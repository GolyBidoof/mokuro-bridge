from __future__ import annotations
import json
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..config import (
    DRIVE_ROOT_NAME,
    WORK_DIR,
    MEGA_LIBRARY_ROOT,
    ONEDRIVE_ROOT_NAME,
    OUTPUT_DIR,
    WEBDAV_BASE_URL,
    WEBDAV_ROOT_NAME,
    _MEGA_UPLOAD_DEFAULT,
    _load_remembered_local_dir,
)
from ..creds import _mega_creds_source
from ..util import series_title_from_volume
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
from .webdav import (
    _run_setup_webdav,
    _webdav_base_url,
    _webdav_configured,
    _webdav_password,
    _webdav_upload_file,
    _webdav_username,
)

_mega_lock = threading.Lock()  # serializes megatools runs (see upload_file)

@dataclass
class UploadMethod:
    id: str            # "local" | "mega" | (future) "drive" | ...
    name: str          # human label
    configured: bool   # is it usable right now?
    default: bool      # is it the default target?
    extra: dict = field(default_factory=dict)  # provider-specific info for /health

# Static list of every method id this build can target. Kept separate from the
# runtime `_UPLOAD_METHODS` snapshot (below) so validation/state helpers can run
# at import time without ordering hazards.
_KNOWN_UPLOAD_METHODS = ("local", "mega", "drive", "onedrive", "webdav")

# The default upload method is "sticky": once a client explicitly asks for a
# method (upload_method=… / upload_to_mega=…), that choice is remembered and
# becomes the default for later requests until another explicit choice replaces
# it. It persists across restarts in this state file (under the work dir).
# Before any explicit choice, the MOKURO_BRIDGE_UPLOAD_DEFAULT env seeds the
# initial default (default "false" → local).
_UPLOAD_METHOD_STATE_FILE = WORK_DIR / "upload_method_default.json"

def _load_remembered_upload_method() -> Optional[str]:
    """The persisted sticky default method, or None when unset/invalid."""
    try:
        data = json.loads(_UPLOAD_METHOD_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    method = str(data.get("method", "")).strip()
    return method if method in _KNOWN_UPLOAD_METHODS else None

def _default_upload_method() -> str:
    """Effective default: the remembered (sticky) method if any, else env."""
    remembered = _load_remembered_upload_method()
    if remembered:
        return remembered
    return "mega" if _MEGA_UPLOAD_DEFAULT else "local"

def _remember_upload_method(method: str) -> None:
    """Persist an explicitly-requested method as the new sticky default.

    Only real, usable targets are remembered (local, or a configured remote) —
    an unconfigured provider is never made the default. Failures are non-fatal:
    the default simply falls back to the env seed on the next start.
    """
    if method not in _KNOWN_UPLOAD_METHODS:
        return
    if method != "local" and not _UPLOAD_METHODS[method].configured:
        return
    try:
        _UPLOAD_METHOD_STATE_FILE.write_text(
            json.dumps({"method": method}), encoding="utf-8"
        )
        _UPLOAD_METHOD_STATE_FILE.chmod(0o600)
    except OSError:
        pass  # non-fatal

def resolve_upload_method(value: Optional[str]) -> str:
    """Map a client-supplied upload target to a concrete method id.

    Accepts: None/"" (current default), "local", "mega", "drive", "onedrive",
    "webdav", and legacy booleans ("true"→mega, "false"→local) for backward
    compatibility with upload_to_mega.
    """
    raw = str(value).strip().lower() if value is not None else ""
    if not raw:
        return _default_upload_method()
    if raw in ("true", "yes", "on", "1"):
        return "mega"
    if raw in ("false", "no", "off", "0"):
        return "local"
    if raw in _KNOWN_UPLOAD_METHODS:
        return raw
    raise ValueError(f"unknown upload method: {value}")

def mega_series_dir(volume_title: str) -> str:
    """Remote MEGA folder for a volume: /Root/mokuro-reader/<series>/."""
    series = series_title_from_volume(volume_title)
    return f"{MEGA_LIBRARY_ROOT}/{series}"

def upload_file(
    method: str,
    local_path: Path,
    remote_dir: str,
    on_progress: Optional[callable],
) -> tuple[bool, Optional[str]]:
    """Upload one file to `method`'s remote dir. Returns (success, error_msg).

    The MEGA branch manages its own megarc (created from _get_mega_creds()
    and deleted in a finally block), ensuring remote dirs exist first. The
    megatools run is serialized under `_mega_lock` exactly like the previous
    inline batch logic. `method == "local"` is handled by the caller, which
    keeps artifacts on disk instead of uploading.
    """
    if method == "mega":
        email, password = _get_mega_creds()
        megarc_path = create_megarc(email, password)
        try:
            with _mega_lock:
                for dir_to_make in (MEGA_LIBRARY_ROOT, remote_dir):
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
                )
        finally:
            megarc_path.unlink(missing_ok=True)
    if method == "drive":
        try:
            service = _drive_service()
            # remote_dir is like "mokuro-reader/<Series>"
            folder_id = _drive_series_folder_id(service, remote_dir)
            return _drive_upload_file(service, folder_id, local_path, on_progress)
        except Exception as e:
            return False, str(e)
    if method == "webdav":
        try:
            username, password = _webdav_username(), _webdav_password()
            if not username or not password:
                return False, "WebDAV credentials not configured. Run `python server.py --setup-upload webdav`."
            return _webdav_upload_file(_webdav_base_url(), username, password, local_path, remote_dir, on_progress)
        except Exception as e:
            return False, str(e)
    if method == "onedrive":
        try:
            token = _onedrive_token()
            return _onedrive_upload_file(token, local_path, remote_dir, on_progress)
        except Exception as e:
            return False, str(e)
    if method == "local":
        raise ValueError("local uploads are handled by the caller")
    raise ValueError(f"unknown upload method: {method}")

def _method_current_folder(method_id: str) -> str:
    """Human-readable 'current folder' for an upload method (for clients)."""
    if method_id == "local":
        remembered = _load_remembered_local_dir()
        return remembered if remembered else str(OUTPUT_DIR)
    if method_id == "mega":
        return MEGA_LIBRARY_ROOT
    if method_id == "drive":
        return f"{DRIVE_ROOT_NAME} (My Drive root)"
    if method_id == "onedrive":
        return f"{ONEDRIVE_ROOT_NAME} (OneDrive root)"
    if method_id == "webdav":
        base = _webdav_base_url()
        return f"{base}/{WEBDAV_ROOT_NAME}" if base else f"{WEBDAV_ROOT_NAME} (WebDAV root)"
    return ""

def _build_upload_methods() -> dict[str, UploadMethod]:
    """Build the live upload-method registry (fresh per call).

    Computed on demand so availability (credential source, megatools binary)
    reflects the current environment — health calls this per request.
    """
    return {
        "local": UploadMethod(
            id="local",
            name="Local output directory",
            configured=True,
            default=(_default_upload_method() == "local"),
        ),
        "mega": UploadMethod(
            id="mega",
            name="MEGA (megatools)",
            configured=_mega_configured(),
            default=(_default_upload_method() == "mega"),
            extra={
                "creds_source": _mega_creds_source(),
                "library_root": MEGA_LIBRARY_ROOT,
            },
        ),
        "drive": UploadMethod(
            id="drive",
            name="Google Drive",
            configured=_drive_configured(),
            default=(_default_upload_method() == "drive"),
            extra={"creds_source": _drive_creds_source(), "root": DRIVE_ROOT_NAME},
        ),
        "onedrive": UploadMethod(
            id="onedrive",
            name="OneDrive",
            configured=_onedrive_configured(),
            default=(_default_upload_method() == "onedrive"),
            extra={
                "creds_source": "token" if _onedrive_configured() else None,
                "root": ONEDRIVE_ROOT_NAME,
            },
        ),
        "webdav": UploadMethod(
            id="webdav",
            name="WebDAV",
            configured=_webdav_configured(),
            default=(_default_upload_method() == "webdav"),
            extra={
                "creds_source": "keyring/file" if _webdav_configured() else None,
                "base_url": WEBDAV_BASE_URL,
            },
        ),
    }

# Registry snapshot taken once at startup (used by the banner + fallback lookups
# in resolve_upload_method); health() rebuilds it live per request. Built here,
# after the drive helpers above, so the snapshot includes the drive method.
_UPLOAD_METHODS = _build_upload_methods()
