"""Named upload accounts ("instances") for the upload providers.

A *method id* names one account of one provider:

    mega            → the default MEGA account (a bare id means "default")
    mega:work       → a second MEGA account named "work"
    drive:main      → the Google Drive account named "main"
    onedrive:uni    → another OneDrive account

Non-secret metadata (display label, remote root, provider hints such as the
MEGA email) lives in one JSON file per account under ACCOUNTS_DIR. Secrets stay
in each provider's own store — the OS keychain for MEGA, a 0600 credential or
token file for Drive/OneDrive. The *default* account deliberately keeps using
the legacy single-account paths (MEGA_CREDS_FILE, DRIVE_CREDS_FILE,
ONEDRIVE_TOKEN_FILE), so existing installs need no migration: with no account
files at all, every provider still has exactly its historical one target.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .util import _chmod_fd_private
from .config import (
    ACCOUNTS_DIR,
    DRIVE_CREDS_FILE,
    DRIVE_ROOT_NAME,
    MEGA_CREDS_FILE,
    MEGA_LIBRARY_ROOT,
    ONEDRIVE_ROOT_NAME,
    ONEDRIVE_TOKEN_FILE,
)

# Providers that can carry more than one account. "local" is a plain directory
# on disk and has no credentials, so it is never an instance.
PROVIDERS = ("mega", "drive", "onedrive")

DEFAULT_NAME = "default"

# Account names become part of a method id, a file name and (for MEGA) nothing
# else, so keep them boring: no separators that any of those would have to
# escape.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_PROVIDER_LABELS = {
    "mega": "MEGA",
    "drive": "Google Drive",
    "onedrive": "OneDrive",
}

_save_lock = threading.RLock()

def default_root(provider: str) -> str:
    """The remote root the default account of `provider` uploads under."""
    if provider == "mega":
        return MEGA_LIBRARY_ROOT
    if provider == "drive":
        return DRIVE_ROOT_NAME
    if provider == "onedrive":
        return ONEDRIVE_ROOT_NAME
    raise ValueError(f"unknown upload provider: {provider}")

def method_id(provider: str, name: str = DEFAULT_NAME) -> str:
    """Canonical method id for (provider, name): bare for the default account."""
    _check_provider(provider)
    _check_name(name)
    return provider if name == DEFAULT_NAME else f"{provider}:{name}"

def parse_method_id(raw: str) -> tuple[str, str]:
    """Split a method id into (provider, account name).

    Accepts "mega", "mega:work", and the redundant-but-harmless "mega:default"
    (normalised to the bare default). Raises ValueError with a usable message
    for anything else, including the "local" target which is not an instance.
    """
    value = str(raw or "").strip().lower()
    provider, sep, name = value.partition(":")
    if provider == "local":
        raise ValueError(
            "'local' is not an account — it writes to a local output directory"
        )
    _check_provider(provider, raw=raw)
    if not sep:
        return provider, DEFAULT_NAME
    if not name:
        raise ValueError(
            f"incomplete upload target {raw!r}: expected '<provider>:<name>', "
            f"e.g. '{provider}:work'"
        )
    _check_name(name, raw=raw)
    return provider, name

def _check_provider(provider: str, raw: Optional[str] = None) -> None:
    if provider not in PROVIDERS:
        shown = raw if raw is not None else provider
        raise ValueError(
            f"unknown upload provider {shown!r} "
            f"(expected one of: {', '.join(PROVIDERS)})"
        )

def _check_name(name: str, raw: Optional[str] = None) -> None:
    if name == DEFAULT_NAME or _NAME_RE.match(name or ""):
        return
    shown = raw if raw is not None else name
    raise ValueError(
        f"invalid account name in {shown!r}: use 1-32 characters from "
        "a-z, 0-9, '_' or '-', starting with a letter or digit"
    )


def _check_root(root: str, raw: Optional[str] = None) -> None:
    """Validate a provider-relative remote root before persisting/using it."""
    value = str(root or "").strip()
    if not value or "\x00" in value or "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("upload root must be a non-empty path without control characters")
    if value.startswith("//"):
        raise ValueError("upload root has an invalid leading slash")
    parts = value[1:].split("/") if value.startswith("/") else value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("upload root must not contain empty, '.' or '..' path segments")
    if value.endswith("/") or value == "/":  # a deliberate leading slash is allowed for MEGA
        raise ValueError("upload root must name a directory, not a filesystem root")

@dataclass(frozen=True)
class Instance:
    """One account of one provider."""

    provider: str
    name: str = DEFAULT_NAME
    label: str = ""
    root: str = ""
    extra: dict = field(default_factory=dict)
    tracked: bool = False  # True when an account file backs this instance
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def id(self) -> str:
        return method_id(self.provider, self.name)

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT_NAME

    @property
    def root_path(self) -> str:
        """Remote root for this account (never empty)."""
        return self.root or default_root(self.provider)

    def display_name_for(self, base: str = "") -> str:
        """Human label, optionally with a provider detail such as "(megatools)".

        The account label always comes last, so a provider note never splits
        the provider from the account it belongs to.
        """
        base = base or _PROVIDER_LABELS.get(self.provider, self.provider)
        if self.label:
            return f"{base} — {self.label}"
        if self.is_default:
            return base
        return f"{base} — {self.name}"

    @property
    def display_name(self) -> str:
        return self.display_name_for()

    @property
    def email(self) -> str:
        """The account's email/handle, when the provider records one."""
        return str(self.extra.get("email", "") or "")

# ── storage ───────────────────────────────────────────────────────────────

def accounts_dir() -> Path:
    """The account directory (created lazily by writers, never on read).

    Deliberately does not mkdir: this is called while building the upload
    registry at import time, and a read-only/unwritable HOME must not stop the
    bridge from starting. Writers create the directory themselves.
    """
    return ACCOUNTS_DIR

def meta_path(provider: str, name: str) -> Path:
    """Metadata file for one account (whether or not it exists yet)."""
    return accounts_dir() / f"{provider}__{name}.json"

def secret_path(provider: str, name: str, kind: str) -> Path:
    """A secret file belonging to one account, e.g. kind="creds.json"."""
    return accounts_dir() / f"{provider}__{name}.{kind}"

def legacy_secret_path(provider: str, name: str) -> Optional[Path]:
    """The historical single-account path, used by the default account."""
    if name != DEFAULT_NAME:
        return None
    return {
        "mega": MEGA_CREDS_FILE,
        "drive": DRIVE_CREDS_FILE,
        "onedrive": ONEDRIVE_TOKEN_FILE,
    }[provider]

def load_instance(provider: str, name: str = DEFAULT_NAME) -> Optional[Instance]:
    """The stored instance, or None when a *named* account isn't configured.

    The default account is always returned: with no account file it is the
    implicit legacy target, so `mega` / `drive` / `onedrive` keep meaning what
    they always did.
    """
    _check_provider(provider)
    _check_name(name)
    try:
        data = json.loads(meta_path(provider, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = None
    if data is None:
        if name != DEFAULT_NAME:
            return None
        return Instance(provider=provider, name=name)
    extra = data.get("extra")
    stored_root = str(data.get("root", "") or "")
    if stored_root:
        try:
            _check_root(stored_root)
        except ValueError:
            return None
    return Instance(
        provider=provider,
        name=name,
        label=str(data.get("label", "") or ""),
        root=stored_root,
        extra=dict(extra) if isinstance(extra, dict) else {},
        tracked=True,
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )

def save_instance(
    provider: str,
    name: str,
    *,
    label: Optional[str] = None,
    root: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Instance:
    """Create or update one account's metadata; returns the stored instance.

    Passing None leaves a field as it was, so a re-run of a wizard can update
    just the label without dropping a previously configured root.
    """
    _check_provider(provider)
    _check_name(name)
    if root is not None:
        _check_root(root)
    with _save_lock:
        previous = load_instance(provider, name)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        merged_extra = dict(previous.extra) if previous else {}
        if extra:
            merged_extra.update({k: v for k, v in extra.items() if v not in (None, "")})
        payload = {
            "provider": provider,
            "name": name,
            "label": previous.label if label is None and previous else (label or ""),
            "root": previous.root if root is None and previous else (root or ""),
            "extra": merged_extra,
            "created_at": (previous.created_at if previous and previous.created_at else now),
            "updated_at": now,
        }
        path = meta_path(provider, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            _chmod_fd_private(fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass
    return load_instance(provider, name)  # type: ignore[return-value]

def delete_instance(provider: str, name: str) -> list[Path]:
    """Remove one account's metadata (and its account-local secrets).

    Legacy credential files are *not* touched here — callers that mean to
    remove the default account's stored credentials pass the legacy path
    explicitly to `remove_paths`.
    """
    _check_provider(provider)
    _check_name(name)
    with _save_lock:
        removed: list[Path] = []
        base = accounts_dir()
        for path in (meta_path(provider, name),):
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed.append(path)
        # Account-local secret files (drive__work.creds.json, mega__work.env, …).
        for path in sorted(base.glob(f"{provider}__{name}.*")):
            if path.name == meta_path(provider, name).name:
                continue
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed.append(path)
        return removed

def remove_paths(paths: list[Path]) -> list[Path]:
    """Delete each existing file in `paths`; returns what was actually removed."""
    removed: list[Path] = []
    with _save_lock:
        for path in paths:
            candidate = Path(path) if path else None
            if candidate and candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()
                removed.append(candidate)
    return removed

def list_instances() -> list[Instance]:
    """Every known account: the implicit default per provider, plus stored ones.

    Ordered default-first per provider so API listings and CLI output read
    predictably ("mega", then "mega:work", …).
    """
    by_id: dict[str, Instance] = {}
    for provider in PROVIDERS:
        implicit = load_instance(provider, DEFAULT_NAME)
        if implicit is not None:
            by_id[implicit.id] = implicit
    for path in sorted(accounts_dir().glob("*__*.json")):
        stem = path.name[: -len(".json")]
        provider, sep, name = stem.partition("__")
        if not sep or provider not in PROVIDERS:
            continue  # not an account file (e.g. a *.creds.json sibling)
        try:
            instance = load_instance(provider, name)
        except ValueError:
            continue  # hand-edited nonsense name — ignore rather than crash
        if instance is not None:
            by_id[instance.id] = instance
    ordered: list[Instance] = []
    for provider in PROVIDERS:
        ordered.extend(
            sorted(
                (i for i in by_id.values() if i.provider == provider),
                key=lambda i: (not i.is_default, i.name),
            )
        )
    return ordered

def instances_for(provider: str) -> list[Instance]:
    """Every known account of one provider (default first)."""
    return [i for i in list_instances() if i.provider == provider]

def sibling_emails(provider: str, name: str) -> set[str]:
    """Emails that *other* accounts of this provider still rely on.

    Used before deleting an account's OS-store entry: two instances can record
    the same email (e.g. the same account configured twice under different
    names), and the shared keychain item must outlive whichever one is removed
    first.
    """
    return {
        i.email
        for i in instances_for(provider)
        if i.email and i.name != name
    }

def next_instance_name(provider: str, preferred: str = "") -> str:
    """A free account name to offer as the default answer in a wizard.

    Only *saved* accounts reserve a name: the implicit default that exists
    before anything is configured must not push the first wizard run onto
    "account2".
    """
    taken = {i.name for i in instances_for(provider) if i.tracked}
    candidate = preferred.strip().lower() if preferred else ""
    if candidate:
        try:
            _check_name(candidate)
        except ValueError:
            candidate = ""
    if candidate and candidate not in taken:
        return candidate
    if DEFAULT_NAME not in taken:
        return DEFAULT_NAME
    for n in range(2, 100):
        name = f"account{n}"
        if name not in taken:
            return name
    return "account"

def ask_account_name(name: Optional[str], provider: str) -> str:
    """The account name to configure: the flag value, else prompt for one.

    Falls back to the suggested free name when stdin is not interactive, so
    `--setup-upload mega` in a script never blocks on the prompt.
    """
    if name:
        return str(name).strip().lower()
    suggested = next_instance_name(provider)
    try:
        answer = input(
            f"{provider} account name [{suggested}] "
            "(use a new name to add another account): "
        ).strip().lower()
    except EOFError:
        answer = ""
    return answer or suggested
