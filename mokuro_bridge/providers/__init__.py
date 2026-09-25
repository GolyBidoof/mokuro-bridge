from __future__ import annotations
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import accounts
from ..accounts import DEFAULT_NAME
from ..config import (
    WORK_DIR,
    OUTPUT_DIR,
    _MEGA_UPLOAD_DEFAULT,
    _load_remembered_local_dir,
)
from ..creds import _mega_creds_source
from ..util import _chmod_fd_private, series_title_from_volume
from .drive import (
    _drive_configured,
    _drive_creds_source,
    _drive_service,
    _drive_series_folder_id,
    _drive_upload_file,
    _run_setup_drive,
)
from .mega import (
    _get_mega_creds,
    _mega_configured,
    _mega_upload_file,
    _run_setup_mega,
    create_megarc,
    mega_mkdir,
)
from .onedrive import (
    _onedrive_configured,
    _onedrive_token,
    _onedrive_upload_file,
    _run_setup_onedrive,
)

_account_locks_guard = threading.Lock()
_account_locks: dict[tuple[str, str], threading.Lock] = {}


def _provider_account_lock(provider: str, name: str) -> threading.Lock:
    key = (provider, name)
    with _account_locks_guard:
        return _account_locks.setdefault(key, threading.Lock())


def _mega_account_lock(name: str) -> threading.Lock:
    return _provider_account_lock("mega", name)

@dataclass
class UploadMethod:
    id: str            # "local" | "mega" | "mega:work" | "drive" | ...
    name: str          # human label
    configured: bool   # is it usable right now?
    default: bool      # is it the default target?
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
    if instance.provider == "mega":
        return _mega_configured(instance.name)
    if instance.provider == "drive":
        return _drive_configured(instance.name)
    if instance.provider == "onedrive":
        return _onedrive_configured(instance.name)
    return False

def _method_creds_source(method: str) -> Optional[str]:
    """Where that target's credentials come from, for /health."""
    instance = instance_for(method)
    if instance is None:
        return None
    if instance.provider == "mega":
        return _mega_creds_source(instance.name)
    if instance.provider == "drive":
        return _drive_creds_source(instance.name)
    if instance.provider == "onedrive":
        return "token" if _onedrive_configured(instance.name) else None
    return None

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
    instance = accounts.load_instance("mega", name)
    root = instance.root_path if instance is not None else accounts.default_root("mega")
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

    The MEGA branch manages its own megarc (created from _get_mega_creds(name)
    and deleted in a finally block), ensuring remote dirs exist first. The
    megatools run is serialized per account.
    """
    instance = instance_for(method)
    if instance is None:
        raise ValueError(f"unknown upload method: {method}")
    name = instance.name
    root = instance.root_path

    if instance.provider == "mega":
        email, password = _get_mega_creds(name)
        megarc_path = create_megarc(email, password)
        try:
            with _mega_account_lock(name):
                for dir_to_make in (root, remote_dir):
                    mkdir_result = mega_mkdir(megarc_path, dir_to_make)
                    if mkdir_result.returncode != 0:
                        err = (mkdir_result.stderr or mkdir_result.stdout or "").strip()
                        if "exist" not in err.lower():
                            print(f"[mokuro-bridge] mkdir {dir_to_make}: {err}")
                return _mega_upload_file(
                    megarc_path,
                    local_path,
                    f"{remote_dir}/{local_path.name}",
                    on_progress,
                    overwrite,
                )
        finally:
            megarc_path.unlink(missing_ok=True)
    if instance.provider == "drive":
        try:
            with _provider_account_lock("drive", name):
                service = _drive_service(name)
                # remote_dir is like "mokuro-reader/<Series>"
                folder_id = _drive_series_folder_id(service, remote_dir, root_name=root)
                return _drive_upload_file(service, folder_id, local_path, on_progress, overwrite)
        except Exception as e:
            return False, str(e), None
    if instance.provider == "onedrive":
        try:
            with _provider_account_lock("onedrive", name):
                token = _onedrive_token(name)
                return _onedrive_upload_file(token, local_path, remote_dir, on_progress, overwrite)
        except Exception as e:
            return False, str(e), None
    raise ValueError(f"unknown upload provider: {instance.provider}")

def _method_current_folder(method_id: str) -> str:
    """Human-readable 'current folder' for an upload target (for clients)."""
    if method_id == _LOCAL_ID:
        remembered = _load_remembered_local_dir()
        return remembered if remembered else str(OUTPUT_DIR)
    instance = instance_for(method_id)
    if instance is None:
        return ""
    if instance.provider == "mega":
        return instance.root_path
    if instance.provider == "drive":
        return f"{instance.root_path} (a folder in that account's My Drive)"
    if instance.provider == "onedrive":
        return f"{instance.root_path} (a folder in that account's drive)"
    return ""

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
        extra: dict = {
            "provider": instance.provider,
            "account": instance.name,
            "creds_source": _method_creds_source(instance.id),
            "tracked": instance.tracked,
        }
        label = instance.display_name
        if instance.provider == "mega":
            # Keep the historical "MEGA (megatools)" wording for the default
            # account, and put the account name after it for the others.
            label = instance.display_name_for("MEGA (megatools)")
            extra["library_root"] = instance.root_path
            if instance.email:
                extra["email"] = instance.email
        elif instance.provider == "drive":
            extra["root"] = instance.root_path
        elif instance.provider == "onedrive":
            extra["root"] = instance.root_path
        methods[instance.id] = UploadMethod(
            id=instance.id,
            name=label,
            configured=_method_configured(instance.id),
            default=(default_method == instance.id),
            extra=extra,
        )
    return methods

# Registry snapshot taken once at startup (used by the banner + setup-upload
# validation in server.py); health()/upload-methods rebuild it live per request.
_UPLOAD_METHODS = _build_upload_methods()
