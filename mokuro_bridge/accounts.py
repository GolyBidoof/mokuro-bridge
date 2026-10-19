"""Named upload accounts ("instances") for the upload providers.

An account is addressed two ways:

* an **alias**, the user-facing id: ``mega``, ``mega-1``, ``drive-next``,
  ``onedrive-uni``. Aliases are globally unique, lowercase and selectable from
  scripts. This is the id every listing, status line and error message shows.
* a **storage name**, the internal spelling used for metadata and secret file
  names: ``default`` for a provider's first account, anything else otherwise.
  ``method_id(provider, name)`` renders it in the historical notation
  (``mega`` / ``mega:work``), which is still accepted forever as input.

Non-secret metadata (display label, alias, remote root, provider hints such as
the MEGA email) lives in one JSON file per account under ACCOUNTS_DIR. Secrets
stay in each provider's own store — the OS keychain for MEGA, a 0600 credential
or token file for Drive/OneDrive. The *default* account deliberately keeps using
the legacy single-account paths (MEGA_CREDS_FILE, DRIVE_CREDS_FILE,
ONEDRIVE_TOKEN_FILE), so existing installs need no migration: with no account
files at all, every provider still has exactly its historical one target.

Aliases of accounts created before aliases existed are derived on read instead
of being written back: the first account of a provider is ``<provider>``, a
named account ``mega:work`` becomes ``mega-work``. Its metadata file is never
rewritten by the migration.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field, replace
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
    RCLONE_ROOT_NAME,
    WEBDAV_CREDS_FILE,
    WEBDAV_ROOT_NAME,
)

# Providers that can carry more than one account. "local" is a plain directory
# on disk and has no credentials, so it is never an instance.
PROVIDERS = ("mega", "drive", "onedrive", "webdav", "rclone")

DEFAULT_NAME = "default"

# Account names become part of a method id, a file name and (for MEGA) nothing
# else, so keep them boring: no separators that any of those would have to
# escape. They are internal after the alias change; users type aliases.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The user-facing account id. Stricter than a storage name: no underscore, so an
# alias always survives being pasted into a shell word or a URL query.
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

class AliasCollision(ValueError):
    """The requested alias already names a different account.

    Separate from plain ``ValueError`` so an API caller can answer 409 (the
    request was well-formed, the name is taken) instead of 400.
    """

_PROVIDER_LABELS = {
    "mega": "MEGA",
    "drive": "Google Drive",
    "onedrive": "OneDrive",
    "webdav": "WebDAV",
    "rclone": "rclone",
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
    if provider == "webdav":
        return WEBDAV_ROOT_NAME
    if provider == "rclone":
        return RCLONE_ROOT_NAME
    raise ValueError(f"unknown upload provider: {provider}")

def method_id(provider: str, name: str = DEFAULT_NAME) -> str:
    """The *storage* spelling for (provider, name): bare for the first account.

    Kept for file names and error context; the user-facing id is
    ``Instance.id`` (the alias). Still accepted as input forever.
    """
    _check_provider(provider)
    _check_name(name)
    return provider if name == DEFAULT_NAME else f"{provider}:{name}"

def derived_alias(provider: str, name: str = DEFAULT_NAME) -> str:
    """The alias an account gets when its metadata records none.

    The first account of a provider is the bare provider id; a named account
    keeps its provider prefix (``mega:work`` → ``mega-work``) so two providers
    cannot collide. Underscores become hyphens because an alias may not carry
    one. This is a read-time derivation: nothing is written back.
    """
    _check_provider(provider)
    _check_name(name)
    if name == DEFAULT_NAME:
        return provider
    return f"{provider}-{name.replace('_', '-')}"

def alias_to_name(provider: str, alias: str) -> str:
    """The storage name a newly created account with ``alias`` uses.

    The provider's own bare id is its first account, which must live at the
    historical ``default`` storage paths so legacy credentials keep working.
    Every other alias doubles as the storage name, keeping files distinct.
    """
    _check_provider(provider)
    value = str(alias or "").strip().lower()
    # The provider's own bare id is its first account, which must live at the
    # historical "default" storage paths so legacy credentials keep working.
    return DEFAULT_NAME if value == provider else value

def validate_alias(
    provider: str, alias: str, *, name: Optional[str] = None, allow_reserved: bool = False
) -> str:
    """Normalise and validate one alias; returns the lowercase form.

    ``name`` is the storage name the alias will belong to (None for a brand-new
    account, where the caller resolves it), so the account being edited never
    collides with itself. Raises ``AliasCollision`` when the alias is taken and
    a plain ``ValueError`` for a malformed or reserved one.
    """
    _check_provider(provider)
    value = str(alias or "").strip().lower()
    if not _ALIAS_RE.match(value):
        raise ValueError(
            f"invalid alias {alias!r}: use 1-32 characters from a-z, 0-9 or '-', "
            "starting with a letter or digit (for example mega-1 or drive-next)"
        )
    if value == "local":
        raise ValueError(
            "'local' is reserved for the local output folder and cannot be an account alias"
        )
    if not allow_reserved and value in PROVIDERS and value != provider:
        raise ValueError(f"alias {value!r} is reserved for the {value} provider")
    if value == provider and name not in (None, DEFAULT_NAME):
        raise ValueError(
            f"alias {value!r} is the first {provider} account's name; "
            f"pick another alias (for example {provider}-2) or configure this "
            f"account as the first one"
        )
    for instance in list_instances():
        if instance.provider == provider and instance.name == name:
            continue
        # A reserved name may be claimed when nothing real uses it yet: the
        # implicit per-provider defaults are placeholders, not accounts.
        if allow_reserved and not getattr(instance, "tracked", False):
            continue
        if instance.id == value:
            raise AliasCollision(
                f"alias {value!r} is already used by {instance.display_name}"
            )
    return value

def alias_for_storage(provider: str, name: str) -> Optional[str]:
    """The alias to persist when a CLI wizard saves this account.

    Returns the stored alias when there is one, else the derived alias, or None
    when persisting it would collide with another account (a read-time derived
    id still exists, it is simply not frozen into the file).
    """
    instance = load_instance(provider, name)
    candidate = (
        instance.alias
        if instance is not None and instance.alias
        else derived_alias(provider, name)
    )
    try:
        return validate_alias(provider, candidate, name=name)
    except ValueError:
        return None

def suggest_alias(provider: str) -> str:
    """A free alias to offer for a new account of ``provider``.

    The first account of a provider claims the bare provider id; later ones get
    ``<provider>-2``, ``<provider>-3``, skipping anything already taken.
    """
    _check_provider(provider)
    taken = {instance.id for instance in list_instances() if instance.tracked}
    taken.add("local")
    if provider not in taken:
        return provider
    for number in range(2, 1000):
        candidate = f"{provider}-{number}"
        if candidate not in taken:
            return candidate
    return provider

def find_alias(alias: str) -> Optional["Instance"]:
    """The account whose alias is exactly ``alias``, or None."""
    value = str(alias or "").strip().lower()
    if not value:
        return None
    # A tracked account owns its alias; an implicit per-provider placeholder
    # must never shadow the real destination.
    instances = sorted(list_instances(), key=lambda item: not item.tracked)
    for instance in instances:
        if instance.id == value:
            return instance
    return None

def known_aliases() -> list[str]:
    """Every user-facing account id plus the local destination."""
    return [instance.id for instance in list_instances()] + ["local"]

def parse_method_id(raw: str) -> tuple[str, str]:
    """Resolve a user-supplied target to (provider, storage name).

    Accepts an alias (``mega-work``), a bare provider (``mega``), the historical
    ``provider:name`` spelling (``mega:work``) and the redundant
    ``provider:default``. Raises ValueError with a usable message for anything
    else, including the ``local`` target which is not an account.
    """
    value = str(raw or "").strip().lower()
    if not value:
        raise ValueError(
            "upload target is empty; expected an account alias such as 'mega' "
            f"(known: {', '.join(known_aliases())})"
        )
    if value == "local":
        raise ValueError(
            "'local' is not an account — it writes to a local output directory"
        )
    provider, sep, name = value.partition(":")
    if not sep:
        instance = find_alias(value)
        if instance is not None:
            return instance.provider, instance.name
        if value in PROVIDERS:
            return provider, DEFAULT_NAME
        raise ValueError(
            f"unknown upload target {raw!r} "
            f"(known accounts: {', '.join(known_aliases())})"
        )
    _check_provider(provider, raw=raw)
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
            f"(expected one of: {', '.join(PROVIDERS)}, or an account alias "
            f"such as {derived_alias('mega', 'work')!r})"
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
    alias: str = ""
    label: str = ""
    root: str = ""
    extra: dict = field(default_factory=dict)
    tracked: bool = False  # True when an account file backs this instance
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def id(self) -> str:
        """The user-facing id: the alias, derived for pre-alias accounts."""
        return self.alias or derived_alias(self.provider, self.name)

    @property
    def storage_id(self) -> str:
        """The historical ``provider[:name]`` spelling (stable across renames)."""
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

def accounts_dir() -> Path:
    """The account directory (created lazily by writers, never on read).

    Deliberately does not mkdir: this is called while building the upload
    registry at import time, and a read-only/unwritable HOME must not stop the
    bridge from starting. Writers create the directory themselves.
    """
    return ACCOUNTS_DIR

def clear_alias(provider: str, name: str) -> bool:
    """Drop a persisted alias so the account falls back to its derived id."""
    _check_provider(provider)
    _check_name(name)
    with _save_lock:
        path = meta_path(provider, name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or "alias" not in data:
            return False
        data.pop("alias", None)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            _chmod_fd_private(fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True


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
        "webdav": WEBDAV_CREDS_FILE,
        "rclone": None,
    }[provider]

def _read_meta(provider: str, name: str) -> Optional[dict]:
    """The raw metadata object for one account, or None when absent/unreadable."""
    try:
        data = json.loads(meta_path(provider, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

def load_instance(provider: str, name: str = DEFAULT_NAME) -> Optional[Instance]:
    """The stored instance, or None when a *named* account isn't configured.

    The default account is always returned: with no account file it is the
    implicit legacy target, so `mega` / `drive` / `onedrive` keep meaning what
    they always did.
    """
    _check_provider(provider)
    _check_name(name)
    data = _read_meta(provider, name)
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
    alias = _stored_alias(provider, name, data.get("alias"))
    return Instance(
        provider=provider,
        name=name,
        alias=alias,
        label=str(data.get("label", "") or ""),
        root=stored_root,
        extra=dict(extra) if isinstance(extra, dict) else {},
        tracked=True,
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )

def _stored_alias(provider: str, name: str, raw: object) -> str:
    """A persisted alias when it is still valid, else "" (derive on read).

    A hand-edited or stale alias must never make an account unaddressable, so
    an unusable one silently falls back to the derived alias.
    """
    value = str(raw or "").strip().lower()
    if not value or not _ALIAS_RE.match(value):
        return ""
    if value == "local":
        return ""
    # rclone-backed destinations keep the service name the user chose ("mega",
    # "webdav"), which is also a legacy provider id.
    if value in PROVIDERS and value != provider and provider != "rclone":
        return ""
    if value == provider and name != DEFAULT_NAME:
        return ""
    return value

def save_instance(
    provider: str,
    name: str,
    *,
    label: Optional[str] = None,
    root: Optional[str] = None,
    extra: Optional[dict] = None,
    alias: Optional[str] = None,
    allow_reserved: bool = False,
) -> Instance:
    """Create or update one account's metadata; returns the stored instance.

    Passing None leaves a field as it was, so a re-run of a wizard can update
    just the label without dropping a previously configured root. ``alias``
    renames the user-facing id (validated for collisions); None keeps whatever
    was persisted, and a legacy account with no persisted alias keeps deriving
    one on read rather than being rewritten by an unrelated save.
    """
    _check_provider(provider)
    _check_name(name)
    if root is not None:
        _check_root(root)
    if alias is not None:
        alias = validate_alias(provider, alias, name=name, allow_reserved=allow_reserved)
    with _save_lock:
        previous = load_instance(provider, name)
        # Only an alias that is actually on disk survives; previous.alias is
        # the derived fallback for legacy accounts and must not be frozen in.
        raw_previous = _read_meta(provider, name) or {}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        merged_extra = dict(previous.extra) if previous else {}
        if extra:
            merged_extra.update({k: v for k, v in extra.items() if v not in (None, "")})
        stored_alias = (
            alias
            if alias is not None
            else _stored_alias(provider, name, raw_previous.get("alias"))
        )
        payload = {
            "provider": provider,
            "name": name,
            "alias": stored_alias,
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
    predictably ("mega", then "mega-1", …).

    Aliases are assigned here so the result is always collision-free. A stored
    alias is trusted (writers validate it); a *derived* alias can still collide
    with another account when hand-edited names such as ``work_2`` and
    ``work-2`` coexist, and the later one is suffixed rather than dropped.
    """
    stored: list[Instance] = []
    for path in sorted(accounts_dir().glob("*__*.json")):
        stem = path.name[: -len(".json")]
        provider, sep, name = stem.partition("__")
        if not sep or provider not in PROVIDERS:
            continue  # not an account file (e.g. a *.creds.json sibling)
        if name == DEFAULT_NAME:
            continue  # loaded as the provider's implicit default below
        try:
            instance = load_instance(provider, name)
        except ValueError:
            continue  # hand-edited nonsense name — ignore rather than crash
        if instance is not None:
            stored.append(instance)
    # A stored alias outranks an implicit placeholder: an rclone destination
    # named "mega" must keep that id, so the empty legacy placeholder for the
    # mega provider steps aside instead of forcing a "-2" suffix.
    stored_ids = {instance.id for instance in stored}
    by_id: dict[str, Instance] = {}
    for provider in PROVIDERS:
        implicit = load_instance(provider, DEFAULT_NAME)
        if implicit is not None and implicit.id not in stored_ids:
            implicit = _claim_alias(implicit, by_id)
            by_id[implicit.id] = implicit
    for instance in stored:
        instance = _claim_alias(instance, by_id)
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

def _claim_alias(instance: Instance, taken: dict[str, Instance]) -> Instance:
    """Return ``instance`` with an alias nobody in ``taken`` holds yet."""
    if instance.id not in taken and instance.id != "local":
        return instance
    base = (instance.alias or derived_alias(instance.provider, instance.name))[:28]
    for number in range(2, 1000):
        candidate = f"{base}-{number}"
        if candidate not in taken and candidate != "local":
            return replace(instance, alias=candidate)
    return instance  # unreachable in practice

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
