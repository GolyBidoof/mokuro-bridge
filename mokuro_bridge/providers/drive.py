from __future__ import annotations
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Optional

from ..config import (
    DRIVE_CREDS_FILE,
    DRIVE_SCOPES,
    _DRIVE_CLIENT_ID_ENV,
    _DRIVE_CLIENT_SECRET_ENV,
)

_DRIVE_IMPORT_HINT = (
    "Google Drive upload needs the google client libraries. "
    "Install them with: pip install -r requirements-drive.txt"
)

# Google's installed-app OAuth endpoints (same values the client secrets file
# would carry). PKCE makes the flow work with just a client ID — the secret is
# optional for installed apps.
_DRIVE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_DRIVE_TOKEN_URI = "https://oauth2.googleapis.com/token"
# Steps the user follows in Google Cloud Console to create their OAuth client.
_DRIVE_CONSOLE_GUIDE = (
    "1. Open Google Cloud Console Credentials:\n"
    "     https://console.cloud.google.com/apis/credentials\n"
    "   (create a project if you don't have one)\n"
    "2. Click  '+ CREATE CREDENTIALS'  →  'OAuth client ID'\n"
    "3. If asked, configure the consent screen first:\n"
    "   'User type: External' → name it anything → Save.\n"
    "4. Application type: 'Desktop app'  →  Create.\n"
    "5. Copy the 'Client ID' (looks like\n"
    "   xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.apps.googleusercontent.com)\n"
    "   and paste it below. The 'Client secret' is NOT needed."
)

def _drive_client_id_from_env() -> str:
    """Client ID preset via DRIVE_CLIENT_ID, or '' when unset."""
    return os.environ.get(_DRIVE_CLIENT_ID_ENV, "").strip()

def _drive_flow_config(client_id: str) -> dict:
    """Build a Google-format client config for the OAuth flow from a client ID.

    PKCE secures the exchange, so no client secret is required for installed
    apps. client_id may be '' only when a full client_secrets.json was given
    via DRIVE_CLIENT_SECRET_FILE (handled by the caller before this is called).
    """
    return {
        "installed": {
            "client_id": client_id,
            "auth_uri": _DRIVE_AUTH_URI,
            "token_uri": _DRIVE_TOKEN_URI,
            "client_secret": "",  # installed-app PKCE flow — not required
            "redirect_uris": ["http://localhost"],
        }
    }

def _load_client_secrets_file() -> Optional[dict]:
    """Load a full client_secrets.json from DRIVE_CLIENT_SECRET_FILE (or None).

    Prints a friendly note when the env var is set but unusable, so a stale
    DRIVE_CLIENT_SECRET_FILE doesn't silently break setup.
    """
    raw = os.environ.get(_DRIVE_CLIENT_SECRET_ENV, "").strip()
    if not raw:
        return None
    secret_file = Path(raw).expanduser()
    if not secret_file.is_file():
        print(
            f"note: {_DRIVE_CLIENT_SECRET_ENV}={secret_file} does not exist — "
            "ignoring it."
        )
        return None
    try:
        return json.loads(secret_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"note: could not read {secret_file}: {exc} — ignoring it.")
        return None

def _client_id_is_plausible(client_id: str) -> bool:
    """Cheap sanity check: Google client IDs end in .apps.googleusercontent.com."""
    return (
        client_id.endswith(".apps.googleusercontent.com")
        and "." in client_id
        and len(client_id) > 20
    )

def _client_id_exists(client_id: str) -> bool:
    """Pre-flight: does Google recognize this OAuth client?

    POSTs a deliberately bogus authorization code to the token endpoint.
    An *unregistered* client is rejected with invalid_client (before the code
    is ever considered); a *registered* client is rejected with invalid_grant
    (the code is just wrong — which is expected, we never really exchange one).
    Network errors raise so the caller can say something useful.
    """
    import requests
    resp = requests.post(
        _DRIVE_TOKEN_URI,
        data={
            "client_id": client_id,
            "code": "mokuro-bridge-preflight-invalid-code",
            "grant_type": "authorization_code",
            "redirect_uri": "http://localhost",
        },
        timeout=20,
    )
    try:
        err = resp.json().get("error", "")
    except ValueError:
        err = ""
    if resp.status_code == 200:
        # Shouldn't happen (bogus code), but treat as "exists" anyway.
        return True
    if err == "invalid_client":
        return False
    # invalid_grant (real client, bad code) and anything else → client exists.
    return True

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


def _drive_find_file_id(service, parent_id: str, name: str) -> Optional[str]:
    """Look up a file (non-folder) by name directly under parent_id."""
    escaped = name.replace("'", "\\'")
    resp = (
        service.files()
        .list(
            q=(
                f"name='{escaped}' and '{parent_id}' in parents "
                "and mimeType!='application/vnd.google-apps.folder' "
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
    overwrite: str = "fail",
) -> tuple[bool, Optional[str]]:
    """Resumable-upload one file into a Drive folder, streaming progress.

    on_progress(bytes_done, total_bytes, speed_bps) fires per 8 MiB chunk
    (the caller throttles upstream NDJSON emission). Returns
    (success, error_message).

    overwrite: "fail" → an existing file with the same name is an error;
    "skip" → existing file counts as success (nothing uploaded);
    "overwrite" → the existing file is deleted first, then re-uploaded.
    """
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(_DRIVE_IMPORT_HINT) from exc
    total = local_path.stat().st_size

    # Existing-file policy.
    existing_id = _drive_find_file_id(service, folder_id, local_path.name)
    if existing_id:
        if overwrite == "skip":
            return True, None, f"https://drive.google.com/file/d/{existing_id}/view"
        if overwrite != "overwrite":
            return (
                False,
                f"destination already exists: {local_path.name} "
                "(send overwrite=overwrite to replace it, or overwrite=skip "
                "to keep the existing copy)",
                None,
            )
        service.files().delete(fileId=existing_id, supportsAllDrives=True).execute()

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
        file_id = response.get("id") if response else None
        url = f"https://drive.google.com/file/d/{file_id}/view" if file_id else None
        return True, None, url
    except Exception as exc:  # HttpError / ResumableUploadError / network…
        return False, str(exc), None

def _run_setup_drive() -> None:
    """Guided first-run wizard: create/paste an OAuth client ID, sign in.

    Google won't let the bridge ship a usable OAuth client — a client only
    works in the project that registered it (anything else fails on Google's
    consent page with 401 invalid_client). So the wizard:
      1. walks you through creating a free "Desktop app" OAuth client in
         Google Cloud Console (with exact links/steps),
      2. lets you paste just the client ID (no client_secret.json needed —
         PKCE secures the flow),
      3. verifies Google actually recognizes the client *before* opening the
         browser, so a typo/mis-click gives a helpful message instead of a raw
         Google error page,
      4. stores the refresh token in DRIVE_CREDS_FILE (0600).
    DRIVE_CLIENT_ID or DRIVE_CLIENT_SECRET_FILE can preset the client instead.
    """
    from ..util import _ensure_python_deps

    if not _ensure_python_deps(
        ["google_auth_oauthlib", "googleapiclient", "requests"],
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
    if DRIVE_CREDS_FILE.is_file():
        answer = input(
            f"Drive credentials already exist at {DRIVE_CREDS_FILE}. "
            "Overwrite (re-authorize)? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing credentials.")
            return

    client_config = _load_client_secrets_file()
    if client_config is not None:
        print("Using the client_secrets.json from DRIVE_CLIENT_SECRET_FILE.")
    else:
        client_id = _drive_client_id_from_env()
        if client_id:
            print("Using the OAuth client ID from DRIVE_CLIENT_ID.")
        else:
            print(_DRIVE_CONSOLE_GUIDE)
            while True:
                try:
                    client_id = input("Paste your OAuth Client ID: ").strip()
                except EOFError:
                    print("\nSetup aborted.")
                    return
                if not client_id:
                    print("No client ID given — aborting.")
                    return
                if not _client_id_is_plausible(client_id):
                    print(
                        "\nThat doesn't look like a Google client ID — it should "
                        "end in '.apps.googleusercontent.com'.\n"
                        "In Google Cloud Console it's shown under Credentials → "
                        "your OAuth 2.0 Client.\n"
                    )
                    continue
                print("\nChecking with Google that this client exists…")
                try:
                    if not _client_id_exists(client_id):
                        print(
                            "\nGoogle doesn't recognize that client ID "
                            "(error: invalid_client).\n"
                            "Most likely causes:\n"
                            "  - the ID was copied wrong (check for spaces or "
                            "truncation)\n"
                            "  - it belongs to a different Google Cloud project\n"
                            "  - the 'OAuth client ID' was created as Web app "
                            "or another type, not 'Desktop app'\n\n"
                            "Fix it in Google Cloud Console and paste the ID "
                            "again."
                        )
                        continue
                except Exception as exc:
                    print(
                        f"\nnote: could not reach Google to verify the client "
                        f"({exc}). Proceeding anyway — if the browser shows "
                        "'invalid_client', re-run and paste the ID again."
                    )
                break
        client_config = _drive_flow_config(client_id)

    flow = InstalledAppFlow.from_client_config(client_config, DRIVE_SCOPES)
    # run_local_server opens the browser, prints "Please visit this URL…",
    # and waits for Google to redirect back to a local server. It fails
    # (e.g. no browser / headless / firewall) only when it can't start the
    # local listener — not for a broken browser (which just leaves you with
    # the printed URL to open yourself).
    try:
        creds = flow.run_local_server(port=0, open_browser=True)
    except Exception as exc:
        print(
            "\nCouldn't start the local sign-in page — this usually means no "
            "browser or a firewall blocking localhost. You can still finish "
            "in a browser on any device:\n"
        )
        auth_url, _ = flow.authorization_url()
        print("  1. Open this URL in a browser and sign in:")
        print(f"     {auth_url}")
        print("  2. When Google redirects you to a localhost page that fails "
              "to load, that's normal —\n"
              "     copy the full address from the address bar and paste it "
              "below.")
        print(f"\n  (fallback failed to start: {exc})\n")
        try:
            paste = input("Paste the localhost redirect URL (or press Enter "
                          "to abort): ").strip()
        except EOFError:
            paste = ""
        if not paste:
            print("Setup aborted.")
            return
        from urllib.parse import urlsplit
        if not urlsplit(paste).scheme:
            paste = "http://localhost/" + paste.lstrip("/")
        try:
            creds = flow.fetch_token(authorization_response=paste)
        except Exception as exc:
            print(f"error: could not finish sign-in: {exc}")
            return

    DRIVE_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DRIVE_CREDS_FILE.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(DRIVE_CREDS_FILE, 0o600)
    print(f"Stored Drive credentials in {DRIVE_CREDS_FILE} (permissions 0600).")
