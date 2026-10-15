from __future__ import annotations
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import accounts
from ..accounts import DEFAULT_NAME
from ..config import (
    WORK_DIR,
    OUTPUT_DIR,
    _MEGA_UPLOAD_DEFAULT,
    _load_remembered_local_dir,
)
from ..util import _chmod_fd_private, series_title_from_volume
from . import drive, mega, onedrive
from .base import DOWNLOAD_LIMIT_BYTES, Provider, ProviderAccount, walk_folders

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
    "DOWNLOAD_LIMIT_BYTES",
]

# The live provider registry. Dispatch below is a lookup in this mapping, so a
# new backend is its own module plus one entry here; accounts.py stays the
# source of truth for which accounts exist and how a method id is spelled. The
# full contract is in docs/providers.md.
_PROVIDERS: dict[str, Provider] = {
    provider.id: provider
    for provider in (mega.PROVIDER, drive.PROVIDER, onedrive.PROVIDER)
}


def register_provider(provider: Provider) -> None:
    """Add or replace one provider in the live registry.

    The three shipped providers are registered at import. A test or an out of
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
    """Normalise a target to its canonical method id.

    "local" stays "local"; "mega" and the redundant "mega:default" become
    "mega"; "mega:work" stays "mega:work". Legacy booleans are *not* handled
    here — resolve_upload_method does that.
    """
    raw = str(value or "").strip().lower()
    if raw == _LOCAL_ID:
        return _LOCAL_ID
    provider, name = accounts.parse_method_id(raw)  # raises ValueError
    return accounts.method_id(provider, name)

def known_method_ids() -> list[str]:
    """Every currently-addressable target id (accounts first, then local)."""
    return [i.id for i in accounts.list_instances()] + [_LOCAL_ID]

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
        if not any(i.id == method for i in accounts.list_instances()):
            return
        if not _method_configured(method):
            return
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

def resolve_upload_method(value: Optional[str]) -> str:
    """Map a client-supplied upload target to a concrete method id.

    Accepts: None/"" (current default), "local", "mega", "mega:work", "drive",
    "drive:main", "onedrive", "onedrive:uni", the redundant
    "<provider>:default", and legacy booleans ("true"→mega, "false"→local) for
    backward compatibility with upload_to_mega. A named account must exist.
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
    for instance in accounts.list_instances():
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
