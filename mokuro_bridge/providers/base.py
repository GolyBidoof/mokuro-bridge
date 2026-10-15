"""The upload provider contract and the two helpers every backend shares.

A provider is one remote destination backend (MEGA, Google Drive, OneDrive,
and whatever is added next). This module defines what a provider must expose,
one shared folder walk, and the lock that serializes calls per account. Each
provider module instantiates the contract as a module-level ``PROVIDER`` object.
Adding a backend is a copy of one of those modules, one line in the registry in
``providers/__init__.py``, and its account metadata in ``accounts.py``. The
walkthrough lives in ``docs/providers.md``.

Why this shape
--------------
The package used to dispatch on ``instance.provider`` in three places (is it
configured, upload, remote file exists) and to repeat the same folder path walk
in three providers. Those were the seams that made a new destination a
multi-module change. A provider now owns its own behaviour behind a fixed set of
callables, and the package only looks the provider up by id. The callables take
the account name rather than the account instance so a provider stays decoupled
from the account registry, except for the small ``account()`` view that exists
purely to render labels and the extra health fields.

The contract is data, not inheritance: a backend is a frozen ``Provider``
value, so a test can patch one field with ``dataclasses.replace`` without
disturbing the others.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from .. import accounts

# What an upload returns: (success, error message, shareable url). The url is
# best-effort and None when the provider cannot produce one.
UploadResult = tuple[bool, Optional[str], Optional[str]]

# Cap on any sidecar read back; only the series manifest is ever downloaded.
DOWNLOAD_LIMIT_BYTES = 4 * 1024 * 1024

# Cap on one directory listing, so a role with tens of thousands of files
# cannot turn "browse to pick a folder" into a multi-megabyte response.
LIST_LIMIT_ENTRIES = 500


@dataclass(frozen=True)
class ProviderAccount:
    """How one account of a provider presents itself to the rest of the bridge.

    ``label`` is the upload-methods display name, ``extra`` are the provider
    specific fields added to ``UploadMethod.extra`` for ``/health`` (the caller
    adds the generic provider, account, creds_source and tracked keys), and
    ``current_folder`` is the human-readable "current folder" clients show.
    """

    label: str
    extra: dict = field(default_factory=dict)
    current_folder: str = ""


@dataclass(frozen=True)
class Provider:
    """One upload destination backend.

    id:
        The provider id that appears in a method id (``mega:work``).
    label:
        Base display name. ``account()`` may refine it per account.
    configured:
        Whether one account is usable right now, meaning its credentials exist
        and the optional SDK or binary it needs is importable or on PATH.
    creds_source:
        A short description of where that account's secret comes from, for
        ``/health`` (``"oauth"``, ``"keychain"``, ``None``).
    upload:
        Perform one upload. Receives (name, root, remote_dir, local_path,
        on_progress, overwrite) and returns an ``UploadResult``. A provider owns
        its locking and its error policy: MEGA lets a credential error raise,
        while Drive and OneDrive convert a failure into the three-tuple.
    remote_exists:
        Whether (remote_dir, filename) already exists, without creating folders
        or uploading anything.
    download:
        Read one small sidecar back: (name, remote_dir, filename) → bytes, or
        None when absent. Never creates folders. It must raise rather than
        guess, because the caller's alternative is to leave the file alone, and
        it refuses anything above ``DOWNLOAD_LIMIT_BYTES``.
    list_folders:
        Read one directory listing: (name, remote_dir) → entry dicts, each
        ``{"name", "path", "type": "folder"|"file", "size"}``. Read-only, and
        it never creates a folder, so browsing a path that does not exist is
        an empty listing rather than a side effect. Used by the configuration
        dashboard to pick a default upload directory.
    run_setup:
        The interactive wizard cli.py calls; its signature is
        (name, root, label).
    account:
        The presentation view for one account.
    """

    id: str
    label: str
    configured: Callable[[str], bool]
    creds_source: Callable[[str], Optional[str]]
    upload: Callable[[str, str, str, Path, Optional[Callable], str], UploadResult]
    remote_exists: Callable[[str, str, str], bool]
    download: Callable[[str, str, str], Optional[bytes]]
    list_folders: Callable[[str, str], list[dict]]
    run_setup: Callable[..., None]
    account: Callable[[accounts.Instance], ProviderAccount]

    def root(self, name: str = accounts.DEFAULT_NAME) -> str:
        """Remote root for one account of this provider.

        accounts.py stays the source of truth: this resolves the stored account
        and falls back to the provider default, the same value
        ``Instance.root_path`` returns.
        """
        instance = accounts.load_instance(self.id, name)
        if instance is not None:
            return instance.root_path
        return accounts.default_root(self.id)


def safe_provider_url(value: object) -> Optional[str]:
    """Keep only a public provider locator; never a signed query or fragment token."""
    try:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        host = parsed.hostname
        if not host:
            return None
        host = host.casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            return None
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except Exception:
        return None


def safe_provider_error(value: object) -> str:
    """Provider error text with every URL stripped back to its public locator.

    Graph and Drive errors embed the request URL, which carries an access token
    in the query string, so no provider error may reach a client unredacted.
    """
    def _redact(match):
        return safe_provider_url(match.group(0).rstrip(".,);]")) or "[redacted-url]"

    return re.sub(r"https?://[^\s<>\"']+", _redact, str(value or ""))[:2000]


def listing_entry(name: str, path: str, *, is_folder: bool, size: Optional[int] = None) -> dict:
    """One directory-listing row, the shape every provider's `list_folders` returns."""
    entry = {"name": name, "path": path, "type": "folder" if is_folder else "file"}
    if size is not None:
        entry["size"] = size
    return entry


def parent_path(path: str) -> str:
    """The containing path, or "" at the top. Keeps each provider's own spelling."""
    text = str(path).rstrip("/")
    if "/" not in text.lstrip("/"):
        return ""
    head = text.rsplit("/", 1)[0]
    return head or "/"


def walk_folders(
    remote_dir: str,
    *,
    find: Callable[[str, str], Optional[str]],
    make: Optional[Callable[[str, str], str]] = None,
    start: str = "root",
    require_nonempty: bool = False,
) -> Optional[str]:
    """Walk a "/"-separated remote path one segment at a time.

    ``find(parent_id, segment)`` returns the child folder id, or None when there
    is no such folder. When ``make`` is given, a miss calls ``make(parent_id,
    segment)`` and continues with the id it returns; this is the create walk
    Drive and OneDrive use. With ``make=None`` the walk only reads and returns
    None at the first missing segment, which is the read-only walk
    ``remote_exists`` uses. Empty path segments are dropped, exactly as the
    three copies did.

    ``start`` is the id of the folder the walk begins in ("root" for both Google
    Drive and OneDrive). The return value is the deepest folder id, or None for
    an empty path and for a read-only miss. A create walk over an empty path
    raises ValueError when ``require_nonempty`` is true, matching the old
    behaviour of the two ensure functions.
    """
    segments = [seg for seg in str(remote_dir or "").strip("/").split("/") if seg]
    if not segments:
        if require_nonempty:
            raise ValueError(f"empty remote path: {remote_dir!r}")
        return None
    parent_id = start
    for segment in segments:
        found = find(parent_id, segment)
        if found is None:
            if make is None:
                return None
            found = make(parent_id, segment)
        parent_id = found
    return parent_id


_account_locks_guard = threading.Lock()
_account_locks: dict[tuple[str, str], threading.Lock] = {}


def provider_account_lock(provider: str, name: str) -> threading.Lock:
    """The process-wide lock that serializes one account's provider calls."""
    key = (provider, name)
    with _account_locks_guard:
        return _account_locks.setdefault(key, threading.Lock())
