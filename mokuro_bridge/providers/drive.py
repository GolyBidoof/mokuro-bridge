from __future__ import annotations
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Optional

from ..config import DRIVE_CREDS_FILE, DRIVE_SCOPES, _DRIVE_CLIENT_SECRET_ENV

_DRIVE_IMPORT_HINT = (
    "Google Drive upload needs the google client libraries. "
    "Install them with: pip install -r requirements-drive.txt"
)

def _drive_creds_source() -> Optional[str]:
    """Where Drive creds come from: 'oauth', 'service_account', or None.

    Pure file inspection (NO google imports): reads DRIVE_CREDS_FILE and
    classifies by content — OAuth user creds carry a "refresh_token", a
    service-account key carries "type": "service_account".
    """
    if not DRIVE_CREDS_FILE.is_file():
        return None
    try:
        info = json.loads(DRIVE_CREDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if info.get("refresh_token"):
        return "oauth"
    if info.get("type") == "service_account":
        return "service_account"
    return None

def _drive_configured() -> bool:
    """Whether the Drive method is usable right now (creds + client lib)."""
    return (
        _drive_creds_source() is not None
        and importlib.util.find_spec("googleapiclient") is not None
    )

def _drive_creds():
    """Load OAuth / service-account credentials from DRIVE_CREDS_FILE."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError(_DRIVE_IMPORT_HINT) from exc
    if not DRIVE_CREDS_FILE.is_file():
        raise RuntimeError(
            "Google Drive not configured. Run `python server.py "
            "--setup-upload drive` to authorize, then retry the upload."
        )
    try:
        info = json.loads(DRIVE_CREDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"could not read Drive credentials from {DRIVE_CREDS_FILE}: {exc}"
        ) from exc
    if info.get("type") == "service_account":
        return service_account.Credentials.from_service_account_info(
            info, scopes=DRIVE_SCOPES
        )
    creds = Credentials.from_authorized_user_info(info, scopes=DRIVE_SCOPES)
    if creds.expired:
        try:
            creds.refresh(Request())
        except Exception as exc:  # e.g. revoked/expired refresh token
            raise RuntimeError(
                f"Drive OAuth token refresh failed ({exc}). Re-run `python "
                "server.py --setup-upload drive` to re-authorize."
            ) from exc
    return creds

def _drive_service():
    """Build a Drive API v3 service handle (lazy import)."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(_DRIVE_IMPORT_HINT) from exc
    return build("drive", "v3", credentials=_drive_creds(), cache_discovery=False)

def _drive_find_folder(service, name, parent_id) -> Optional[str]:
    """Look up a Drive folder by name directly under parent_id (or None)."""
    escaped = name.replace("'", "\\'")
    resp = (
        service.files()
        .list(
            q=(
                f"name='{escaped}' and '{parent_id}' in parents "
                "and mimeType='application/vnd.google-apps.folder' "
                "and trashed=false"
            ),
            spaces="drive",
            fields="files(id,name)",
            supportsAllDrives=True,
        )
        .execute()
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None

def _drive_ensure_folder(service, name, parent_id) -> str:
    """Find a Drive folder under parent_id, creating it when missing."""
    existing = _drive_find_folder(service, name, parent_id)
    if existing:
        return existing
    created = (
        service.files()
        .create(
            body={
                "name": name,
                "parents": [parent_id],
                "mimeType": "application/vnd.google-apps.folder",
            },
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return created["id"]

def _drive_series_folder_id(service, drive_path: str) -> str:
    """Ensure the Drive folder chain for a "/"-separated remote path.

    remote_dir arrives as e.g. "mokuro-reader/<Series>": the first segment
    must be the configured root folder (DRIVE_ROOT_NAME, created under
    "root"/My Drive) and each remaining segment is a folder nested under
    the previous one. Handles 1-2+ segments generically. Returns the id of
    the deepest folder.
    """
    segments = [seg for seg in str(drive_path).strip("/").split("/") if seg]
    if not segments:
        raise ValueError(f"empty Google Drive remote path: {drive_path!r}")
    if segments[0] != DRIVE_ROOT_NAME:
        raise ValueError(
            f"Google Drive remote path must start with '{DRIVE_ROOT_NAME}' "
            f"(got {drive_path!r})"
        )
    parent_id = "root"
    for seg in segments:
        parent_id = _drive_ensure_folder(service, seg, parent_id)
    return parent_id

def _drive_upload_file(
    service,
    folder_id: str,
    local_path: Path,
    on_progress: Optional[callable],
) -> tuple[bool, Optional[str]]:
    """Resumable-upload one file into a Drive folder, streaming progress.

    on_progress(bytes_done, total_bytes, speed_bps) fires per 8 MiB chunk
    (the caller throttles upstream NDJSON emission). Returns
    (success, error_message).
    """
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(_DRIVE_IMPORT_HINT) from exc
    total = local_path.stat().st_size
    media = MediaFileUpload(
        str(local_path),
        mimetype="application/octet-stream",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    request = service.files().create(
        body={"name": local_path.name, "parents": [folder_id]},
        media_body=media,
        fields="id,name,size",
        supportsAllDrives=True,
    )
    last_bytes = 0
    last_time = time.monotonic()
    try:
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status is not None and on_progress:
                now = time.monotonic()
                bytes_done = int(status.resumable_progress)
                dt = now - last_time
                speed = int((bytes_done - last_bytes) / dt) if dt > 0 else 0
                last_bytes, last_time = bytes_done, now
                on_progress(bytes_done, total, speed)
        if on_progress:
            on_progress(total, total, 0)
        return True, None
    except Exception as exc:  # HttpError / ResumableUploadError / network…
        return False, str(exc)

def _run_setup_drive() -> None:
    """Interactive first-run wizard: OAuth-authorize and store Drive creds."""
    from ..util import _ensure_python_deps

    if not _ensure_python_deps(
        ["google_auth_oauthlib", "googleapiclient"],
        "requirements-drive.txt",
    ):
        print(
            "Google Drive setup needs the google client libraries. "
            "Install them with:\n  pip install -r requirements-drive.txt\n"
            "then re-run: python server.py --setup-upload drive"
        )
        return

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("Google Drive upload setup for mokuro-bridge")
    print("-" * 40)
    secret_path = os.environ.get(_DRIVE_CLIENT_SECRET_ENV, "").strip()
    if not secret_path:
        secret_path = input(
            "Path to your Google client_secret.json "
            f"(or set {_DRIVE_CLIENT_SECRET_ENV}): "
        ).strip()
    secret_file = Path(secret_path).expanduser() if secret_path else None
    if secret_file is None or not secret_file.is_file():
        print(
            "No client_secret.json found. Download one from the Google Cloud "
            "Console (APIs & Services → Credentials → Create credentials → "
            "OAuth client ID → Desktop app) and re-run."
        )
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), DRIVE_SCOPES)
    creds = None
    last_error = None
    try:
        creds = flow.run_local_server(port=0)
    except Exception as exc:  # e.g. no browser on this machine
        last_error = exc
    if creds is None:
        try:
            creds = flow.run_console()
        except Exception as exc:
            last_error = exc
    if creds is None:
        print(f"error: OAuth authorization failed: {last_error}")
        return

    DRIVE_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DRIVE_CREDS_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(DRIVE_CREDS_FILE, 0o600)
    print(f"Stored Drive credentials in {DRIVE_CREDS_FILE} (permissions 0600).")
