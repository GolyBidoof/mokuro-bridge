from __future__ import annotations
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import accounts, bunko
from ..accounts import DEFAULT_NAME
from ..config import (
    WORK_DIR,
    OUTPUT_DIR,
    _MEGA_UPLOAD_DEFAULT,
    _load_remembered_local_dir,
)
from ..util import _chmod_fd_private, series_title_from_volume
from . import drive, mega, onedrive, rclone, webdav
from .base import (
    DOWNLOAD_LIMIT_BYTES,
    LIST_LIMIT_ENTRIES,
    Provider,
    ProviderAccount,
    safe_provider_error,
    safe_provider_url,
    walk_folders,
)

# Names this package exists to re-export. Everything below is imported here so
# cli.py (and api.py) can reach the provider internals from one place, not from
# three sibling modules; without an explicit __all__ those deliberate
# re-exports are indistinguishable from leftovers to a linter, and F401 fires
# on every one of them.
__all__ = [
    "Provider",
    "ProviderAccount",
    "walk_folders",
    "register_provider",
    "setup_runner",
    "_UPLOAD_METHODS",
    "UploadMethod",
    "_build_upload_methods",
    "_default_upload_method",
    "_method_current_folder",
    "_remember_upload_method",
    "_repoint_upload_method",
    "_clear_remembered_upload_method",
    "canonical_method_id",
    "instance_for",
    "known_method_ids",
    "mega_series_dir",
    "method_label",
    "method_provider",
    "method_root",
    "resolve_upload_method",
    "upload_file",
    "remote_file_exists",
    "download_file",
    "list_remote_folders",
    "metadata_is_external",
    "safe_provider_error",
    "safe_provider_url",
    "forget_account",
    "DOWNLOAD_LIMIT_BYTES",
    "LIST_LIMIT_ENTRIES",
]

# The live provider registry. Dispatch below is a lookup in this mapping, so a
# new backend is its own module plus one entry here; accounts.py stays the
# source of truth for which accounts exist and how a method id is spelled. The
# full contract is in docs/providers.md.
_PROVIDERS: dict[str, Provider] = {
    provider.id: provider
    for provider in (
        mega.PROVIDER,
        drive.PROVIDER,
        onedrive.PROVIDER,
        webdav.PROVIDER,
        rclone.PROVIDER,
    )
}


def register_provider(provider: Provider) -> None:
    """Add or replace one provider in the live registry.

    The shipped providers are registered at import. A test or an out of
    tree backend calls this to extend dispatch without touching any of the
    lookup sites in this module.
    """
    _PROVIDERS[provider.id] = provider


def setup_runner(provider_id: str) -> Optional[Callable[..., None]]:
    """The interactive setup wizard for one provider, or None when unknown."""
    provider = _PROVIDERS.get(provider_id)
    return provider.run_setup if provider else None


@dataclass
class UploadMethod:
    id: str            # "local" | "mega" | "mega:work" | "drive" | ...
    name: str
    configured: bool   # is it usable right now?
    default: bool
    extra: dict = field(default_factory=dict)  # provider-specific info for /health

# Every account of every provider is a target; "local" is the only non-account
# one ("local" writes to OUTPUT_DIR and has no credentials). See accounts.py.
_LOCAL_ID = "local"

# The default upload method is "sticky": once a client explicitly asks for a
# method (upload_method=… / upload_to_mega=…), that choice is remembered and
# becomes the default for later requests until another explicit choice replaces
# it. It persists across restarts in this state file (under the work dir).
# Before any explicit choice, the MOKURO_BRIDGE_UPLOAD_DEFAULT env seeds the
# initial default (default "false" → local; may also name an account such as
# "mega:work").
_UPLOAD_METHOD_STATE_FILE = WORK_DIR / "upload_method_default.json"
_upload_state_lock = threading.RLock()

def canonical_method_id(value: str) -> str:
    """Normalise a target to its canonical user-facing id (its alias).

    "local" stays "local"; an alias, "mega", "mega:work" and the redundant
    "mega:default" all resolve to the account's alias (``mega``, ``mega-work``
    for a legacy named account). A named account that does not exist keeps the
    historical spelling so the caller's error message can quote what the client
    asked for. Legacy booleans are *not* handled here — resolve_upload_method
    does that.
    """
    raw = str(value or "").strip().lower()
    if raw == _LOCAL_ID:
        return _LOCAL_ID
    provider, name = accounts.parse_method_id(raw)  # raises ValueError
    for instance in accounts.list_instances():
        if instance.provider == provider and instance.name == name:
            return instance.id
    return accounts.method_id(provider, name)

def known_method_ids() -> list[str]:
    """Every currently-addressable target id (account aliases, then local)."""
    return accounts.known_aliases()

def instance_for(method: str) -> Optional[accounts.Instance]:
    """The account behind a canonical method id (None for "local")."""
    if method == _LOCAL_ID:
        return None
    provider, name = accounts.parse_method_id(method)
    return accounts.load_instance(provider, name)

def method_root(method: str) -> str:
    """Remote root for a canonical method id ("" for local)."""
    instance = instance_for(method)
    return instance.root_path if instance is not None else ""

def method_provider(method: str) -> Optional[str]:
    """Provider of a canonical method id (None for "local")."""
    instance = instance_for(method)
    return instance.provider if instance is not None else None

def method_label(method: str) -> str:
    """Human label for messages/logs."""
    if method == _LOCAL_ID:
        return "local output directory"
    instance = instance_for(method)
    return instance.display_name if instance is not None else method

def _method_configured(method: str) -> bool:
    """Whether a target is usable right now (credentials + libraries)."""
    if method == _LOCAL_ID:
        return True
    instance = instance_for(method)
    if instance is None:
        return False
    provider = _PROVIDERS.get(instance.provider)
    return bool(provider and provider.configured(instance.name))

def _method_creds_source(method: str) -> Optional[str]:
    """Where that target's credentials come from, for /health."""
    instance = instance_for(method)
    if instance is None:
        return None
    provider = _PROVIDERS.get(instance.provider)
    return provider.creds_source(instance.name) if provider else None

def _env_seeded_method() -> str:
    """The env-seeded default target (MOKURO_BRIDGE_UPLOAD_DEFAULT).

    Accepts the historical booleans (true → mega, false/empty → local) and an
    explicit method id such as "mega:work".
    """
    raw = os.environ.get("MOKURO_BRIDGE_UPLOAD_DEFAULT", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return "mega"
    if raw in ("", "0", "false", "no", "off"):
        return "local"
    try:
        method = canonical_method_id(raw)
    except ValueError:
        pass
    else:
        if method == _LOCAL_ID or any(i.id == method for i in accounts.list_instances()):
            return method
    return "mega" if _MEGA_UPLOAD_DEFAULT else "local"

def _load_remembered_upload_method() -> Optional[str]:
    """The persisted sticky default target, or None when unset/invalid."""
    try:
        data = json.loads(_UPLOAD_METHOD_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = str(data.get("method", "")).strip().lower()
    if not raw:
        return None
    try:
        method = canonical_method_id(raw)
    except ValueError:
        return None
    if method == _LOCAL_ID:
        return method
    # A remembered account that no longer exists falls back to the env seed.
    return method if any(i.id == method for i in accounts.list_instances()) else None

def _default_upload_method() -> str:
    """Effective default: the remembered (sticky) target if any, else env."""
    return _load_remembered_upload_method() or _env_seeded_method()

def _remember_upload_method(method: str) -> None:
    """Persist an explicitly-requested target as the new sticky default.

    Only real, usable targets are remembered (local, or a configured account) —
    an unconfigured provider is never made the default. Failures are non-fatal:
    the default simply falls back to the env seed on the next start.
    """
    if method != _LOCAL_ID:
        try:
            method = canonical_method_id(method)
        except ValueError:
            return
        if not any(i.id == method for i in accounts.list_instances()):
            return
        if not _method_configured(method):
            return
    _write_upload_method_state(method)

def _write_upload_method_state(method: str) -> None:
    """Write the sticky-default state file (non-fatal on any filesystem error)."""
    try:
        with _upload_state_lock:
            _UPLOAD_METHOD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{_UPLOAD_METHOD_STATE_FILE.name}.",
                suffix=".tmp",
                dir=str(_UPLOAD_METHOD_STATE_FILE.parent),
            )
            try:
                _chmod_fd_private(fd)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"method": method}) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, _UPLOAD_METHOD_STATE_FILE)
            finally:
                try:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                except OSError:
                    pass
    except OSError:
        pass  # non-fatal

def _repoint_upload_method(old: str, new: str) -> bool:
    """Follow a rename in the sticky-default state file.

    When the remembered default was the account that just got a new alias, the
    state file keeps pointing at it instead of silently falling back to the env
    seed. Returns True when it was rewritten.
    """
    try:
        data = json.loads(_UPLOAD_METHOD_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    raw = str(data.get("method", "")).strip().lower() if isinstance(data, dict) else ""
    if not raw:
        return False
    wanted = str(old or "").strip().lower()
    if raw != wanted:
        # The stored spelling may be the account's legacy id ("drive:work"),
        # which still resolves after the rename.
        try:
            if canonical_method_id(raw) != wanted:
                return False
        except ValueError:
            return False
    _write_upload_method_state(new)
    return True

def _clear_remembered_upload_method(*method_ids: str) -> bool:
    """Drop the sticky default when it names one of ``method_ids``.

    Forgetting the account a default pointed at leaves the file pointing at
    nothing; deleting it lets the env seed (or local) apply again. The raw
    spelling is canonicalised first, so an old ``mega:work`` entry is recognised
    after the account moved to an alias.
    """
    wanted = {str(value or "").strip().lower() for value in method_ids if value}
    if not wanted:
        return False
    try:
        data = json.loads(_UPLOAD_METHOD_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    raw = str(data.get("method", "")).strip().lower() if isinstance(data, dict) else ""
    if not raw:
        return False
    try:
        canonical = canonical_method_id(raw)
    except ValueError:
        canonical = raw
    if canonical not in wanted and raw not in wanted:
        return False
    try:
        with _upload_state_lock:
            _UPLOAD_METHOD_STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return True

def resolve_upload_method(value: Optional[str]) -> str:
    """Map a client-supplied upload target to a concrete method id (an alias).

    Accepts: None/"" (current default), "local", an alias (``mega-1``,
    ``drive-next``), a bare provider (``mega``), the historical ``provider:name``
    spelling (``mega:work``, ``drive:main``), the redundant
    ``<provider>:default``, and legacy booleans ("true"→mega, "false"→local) for
    backward compatibility with upload_to_mega. An unknown account raises with
    the list of known aliases.
    """
    raw = str(value).strip().lower() if value is not None else ""
    if not raw:
        return _default_upload_method()
    if raw in ("true", "yes", "on", "1"):
        return "mega"
    if raw in ("false", "no", "off", "0"):
        return "local"
    method = canonical_method_id(raw)
    if method != _LOCAL_ID and not any(
        i.id == method for i in accounts.list_instances()
    ):
        raise ValueError(
            f"unknown upload account: {value} "
            f"(available: {', '.join(known_method_ids())})"
        )
    return method

def mega_series_dir(volume_title: str, name: str = DEFAULT_NAME) -> str:
    """Remote MEGA folder for a volume: <account root>/<series>/."""
    root = _PROVIDERS["mega"].root(name)
    series = series_title_from_volume(volume_title)
    return f"{root}/{series}"

def upload_file(
    method: str,
    local_path: Path,
    remote_dir: str,
    on_progress: Optional[callable],
    overwrite: str = "fail",
) -> tuple[bool, Optional[str], Optional[str]]:
    """Upload one file to `method`'s remote dir. Returns
    (success, error_msg, url).

    `method` is a canonical target id: "mega", "mega:work", "drive:main", …
    ("local" is handled by the caller). url is a shareable/viewable link when
    the provider can produce one (best-effort; None when unavailable).

    overwrite: "fail" (default) → an existing destination file errors with a
    clear, method-agnostic message; "skip" → existing file is treated as
    success (nothing uploaded); "overwrite" → delete-then-upload so the remote
    copy is replaced. Applies uniformly to every provider.

    The provider owns its account lock, the folders it needs to create and its
    own error policy; this function only resolves the target and looks the
    provider up in the registry.
    """
    instance = instance_for(method)
    if instance is None:
        raise ValueError(f"unknown upload method: {method}")
    provider = _PROVIDERS.get(instance.provider)
    if provider is None:
        raise ValueError(f"unknown upload provider: {instance.provider}")
    return provider.upload(
        instance.name,
        instance.root_path,
        remote_dir,
        local_path,
        on_progress,
        overwrite,
    )

def remote_file_exists(method: str, remote_dir: str, filename: str) -> bool:
    """Check a provider path without creating remote folders or uploading data."""
    method = canonical_method_id(method)
    instance = instance_for(method)
    if instance is None:
        raise ValueError("local destinations are checked by the API layer")
    provider = _PROVIDERS.get(instance.provider)
    if provider is None:
        raise ValueError(f"unknown upload provider: {instance.provider}")
    return provider.remote_exists(instance.name, remote_dir, filename)

def download_file(method: str, remote_dir: str, filename: str) -> Optional[bytes]:
    """Read one sidecar back. None = absent, raise = could not find out."""
    instance = instance_for(method)
    if instance is None:
        raise ValueError("local destinations are read by the API layer")
    provider = _PROVIDERS.get(instance.provider)
    if provider is None:
        raise ValueError(f"unknown upload provider: {instance.provider}")
    return provider.download(instance.name, remote_dir, filename)


# Whether another program compiles series.json for a target changes once per
# setup, not once per volume, so a probe result is cached briefly. The cache is
# process-wide; a new bunko is picked up after the window, and a stopped one
# keeps the skip for at most the same window.
_METADATA_OWNER_CACHE_SECONDS = 120.0
_metadata_owner_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_metadata_owner_lock = threading.Lock()

_EXTERNAL_OWNER_VALUES = {"bunko", "external", "true", "yes", "on", "1"}
_BRIDGE_OWNER_VALUES = {"bridge", "own", "false", "no", "off", "0"}


def metadata_is_external(method: str) -> bool:
    """Whether ``method``'s destination compiles ``series.json`` itself.

    True means the caller must not read, merge or upload the manifest: the
    destination (mokuro-bunko) owns it. Resolution order:

    1. The account's non-secret ``series_owner`` setting, so a bunko 0.7 server
       behind a proxy can be declared without probing.
    2. The provider's own ``metadata_owner`` hook, used by WebDAV to combine
       the cloud marker with the 0.7 ``/api/health`` fingerprint.
    3. The generic ``.mokuro-bunko.json`` marker probe every provider supports,
       because ``download`` and ``root`` already abstract the storage.

    Any probe error is False: writing the manifest is always the safe fallback.
    """
    if method == _LOCAL_ID:
        return False
    try:
        instance = instance_for(method)
    except ValueError:
        return False
    if instance is None:
        return False
    override = str(instance.extra.get("series_owner", "") or "").strip().lower()
    if override in _EXTERNAL_OWNER_VALUES:
        return True
    if override in _BRIDGE_OWNER_VALUES:
        return False
    provider = _PROVIDERS.get(instance.provider)
    if provider is None:
        return False
    try:
        root = provider.root(instance.name)
    except Exception:
        return False
    key = (method, root)
    now = time.monotonic()
    with _metadata_owner_lock:
        cached = _metadata_owner_cache.get(key)
        if cached is not None and now - cached[0] < _METADATA_OWNER_CACHE_SECONDS:
            return cached[1]
    try:
        if provider.metadata_owner is not None:
            value = bool(provider.metadata_owner(instance.name))
        else:
            value = bunko.probe_marker(provider.download, instance.name, root)
    except Exception:
        value = False
    with _metadata_owner_lock:
        _metadata_owner_cache[key] = (now, bool(value))
    return bool(value)


def forget_account(target: str) -> dict:
    """Remove one account: metadata, its secret files, and its keychain item.

    Returns ``{"id", "removed", "notes"}``. One implementation for the CLI's
    --remove-upload and the configuration dashboard, so the two cannot disagree
    about what "remove an account" deletes.
    """
    from ..creds import _delete_mega_creds_keychain

    provider, name = accounts.parse_method_id(target)
    instance = accounts.load_instance(provider, name)
    label = instance.id if instance is not None else target
    email = instance.email if instance is not None else ""
    siblings = accounts.sibling_emails(provider, name) if provider == "mega" else set()
    notes: list[str] = []

    rclone_remote = ""
    if provider == "rclone" and instance is not None:
        rclone_remote = str(instance.extra.get("remote") or "").strip()
    removed = [str(path) for path in accounts.delete_instance(provider, name)]
    if provider == "rclone" and rclone_remote:
        from . import rclone as rclone_provider

        # Only remotes this bridge created; a user's own remote is left alone.
        if rclone_remote.rstrip(":").startswith("mb-") and rclone_provider.delete_remote(rclone_remote):
            removed.append(f"rclone:{rclone_remote}")
    legacy = accounts.legacy_secret_path(provider, name)
    if legacy is not None:
        removed += [str(path) for path in accounts.remove_paths([legacy])]
    if _clear_remembered_upload_method(
        label, accounts.method_id(provider, name)
    ):
        notes.append("it was the default destination; the default was reset")

    if provider == "mega":
        # A default account configured before the account files existed has no
        # recorded email; the keychain itself is the only place to learn it.
        target_email = email
        if not target_email:
            from ..creds import _keychain_mega_account

            target_email = _keychain_mega_account() or ""
        if target_email and target_email in siblings:
            notes.append(f"{target_email} is still used by another MEGA account; its keychain item was kept")
        elif target_email:
            if _delete_mega_creds_keychain(target_email):
                removed.append(f"keychain:mega.nz/{target_email}")
            else:
                notes.append(f"could not remove the keychain item for {target_email}")
        else:
            notes.append("no MEGA email is recorded, so no keychain item could be identified")
    elif provider == "webdav" and name == accounts.DEFAULT_NAME:
        # Only the default account ever used the shared keychain item.
        removed += webdav.forget_default_keychain()
    return {"id": label, "removed": removed, "notes": notes}


def list_remote_folders(method: str, remote_dir: str) -> list[dict]:
    """Read one directory listing. Read-only: it never creates a folder."""
    instance = instance_for(method)
    if instance is None:
        raise ValueError("local destinations are listed by the API layer")
    provider = _PROVIDERS.get(instance.provider)
    if provider is None:
        raise ValueError(f"unknown upload provider: {instance.provider}")
    return provider.list_folders(instance.name, remote_dir)

def _method_current_folder(method_id: str) -> str:
    """Human-readable 'current folder' for an upload target (for clients)."""
    if method_id == _LOCAL_ID:
        remembered = _load_remembered_local_dir()
        return remembered if remembered else str(OUTPUT_DIR)
    instance = instance_for(method_id)
    if instance is None:
        return ""
    provider = _PROVIDERS.get(instance.provider)
    return provider.account(instance).current_folder if provider else ""

def _build_upload_methods() -> dict[str, UploadMethod]:
    """Build the live upload-target registry (fresh per call).

    Computed on demand so availability (credential source, megatools binary,
    account files) reflects the current environment — health calls this per
    request. One entry per configured account, plus "local".
    """
    methods: dict[str, UploadMethod] = {
        _LOCAL_ID: UploadMethod(
            id=_LOCAL_ID,
            name="Local output directory",
            configured=True,
            default=(_default_upload_method() == _LOCAL_ID),
        )
    }
    default_method = _default_upload_method()
    instances = list(accounts.list_instances())
    claimed = {instance.id for instance in instances if instance.tracked}
    for instance in instances:
        # An unconfigured placeholder must not appear next to the account that
        # took its id (an rclone destination aliased "mega", for example).
        if not instance.tracked and instance.id in claimed:
            continue
        provider = _PROVIDERS.get(instance.provider)
        # accounts.py gates the provider list, so the fallback view is only
        # reachable for a hand-edited account file; it matches the old default.
        view = (
            provider.account(instance)
            if provider is not None
            else ProviderAccount(label=instance.display_name)
        )
        extra: dict = {
            "provider": instance.provider,
            "alias": instance.id,
            "account": instance.name,
            "creds_source": _method_creds_source(instance.id),
            "tracked": instance.tracked,
        }
        extra.update(view.extra)
        methods[instance.id] = UploadMethod(
            id=instance.id,
            name=view.label,
            configured=_method_configured(instance.id),
            default=(default_method == instance.id),
            extra=extra,
        )
    return methods

# Registry snapshot taken once at startup (used by the banner + setup-upload
# validation in server.py); health()/upload-methods rebuild it live per request.
_UPLOAD_METHODS = _build_upload_methods()
