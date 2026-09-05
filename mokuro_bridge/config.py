from __future__ import annotations
import json
import os
from pathlib import Path

from .util import _env_path

_CORS_DEFAULT_ORIGINS = (
    "https://viewer.bookwalker.jp,"
    "https://viewer-trial.bookwalker.jp,"
    "https://viewer-ptrial.bookwalker.jp,"
    "https://viewer-subscription.bookwalker.jp"
)
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", _CORS_DEFAULT_ORIGINS).split(",")
    if o.strip()
]

# Working dir: page images + OCR JSON while a volume is being processed.
WORK_DIR = _env_path("MOKURO_BRIDGE_WORK_DIR", Path.home() / "mokuro-input")
# Local output dir: finished <series>/<volume>.{cbz,mokuro,webp} when MEGA
# upload is disabled (default). Git-ignored when inside the repo checkout.
OUTPUT_DIR = _env_path("MOKURO_BRIDGE_OUTPUT_DIR", Path(__file__).resolve().parent.parent / "output")

WORK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR = WORK_DIR / ".bridge_sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
IMAGE_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png"}

# request with upload_to_mega=true.
MEGA_LIBRARY_ROOT = os.environ.get("MEGA_LIBRARY_ROOT", "/Root/mokuro-reader")
_MEGA_UPLOAD_DEFAULT = str(
    os.environ.get("MOKURO_BRIDGE_UPLOAD_DEFAULT", "false")
).lower() in ("1", "true", "yes", "on")

# Optional guard against failed scrapes: refuse remote upload when a volume
# has fewer than this many pages (free-viewer errors can capture a handful of
# junk pages). Default 1 = effectively off — a legit short volume (e.g. a
# 9-page sampler) must still upload. Raise it if you want a stricter floor.
MIN_PAGES_FOR_MEGA = max(1, int(os.environ.get("MIN_PAGES_FOR_MEGA", "1")))

# Google Drive (optional destination via google-api-python-client).
# Auth is OAuth2: creds live in DRIVE_CREDS_FILE (0600), created by
# `python server.py --setup-upload drive`. A service-account JSON also works.
DRIVE_ROOT_NAME = os.environ.get("DRIVE_ROOT_NAME", "mokuro-reader")  # folder at My Drive root
DRIVE_CREDS_FILE = _env_path(
    "DRIVE_CREDS_FILE", Path.home() / ".config" / "mokuro-bridge" / "drive_credentials.json"
)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# The setup wizard needs a Google Cloud OAuth client ID. It can't ship one:
# Google only lets a client run in the project that registered it (otherwise
# the consent page fails with 401 invalid_client). So the wizard walks the
# user through creating a free "Desktop app" OAuth client and pasting its ID.
# DRIVE_CLIENT_ID / DRIVE_CLIENT_SECRET_FILE let advanced users preset it.
_DRIVE_CLIENT_SECRET_ENV = "DRIVE_CLIENT_SECRET_FILE"
_DRIVE_CLIENT_ID_ENV = "DRIVE_CLIENT_ID"

# OneDrive (optional destination via MS Graph, msal device-code auth).
# NOTE: msal reserves 'offline_access' — it's added automatically, so it must
# NOT appear in the scopes list (msal raises ValueError otherwise).
ONEDRIVE_ROOT_NAME = os.environ.get("ONEDRIVE_ROOT_NAME", "mokuro-reader")
ONEDRIVE_CLIENT_ID = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip()
ONEDRIVE_TOKEN_FILE = _env_path(
    "ONEDRIVE_TOKEN_FILE", Path.home() / ".config" / "mokuro-bridge" / "onedrive_token.json"
)
_ONEDRIVE_SCOPES = ["https://graph.microsoft.com/Files.ReadWrite"]
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Chunked OCR flush: pages are grouped into batches so the queue drains in
# controlled chunks. Override via env.
_OCR_CHUNK_SIZE = max(1, int(os.environ.get("OCR_CHUNK_SIZE", "8")))
_OCR_IDLE_FLUSH_S = float(os.environ.get("OCR_IDLE_FLUSH_S", "1.5"))

# MEGA credentials, resolved in order: env vars → creds file → OS keychain.
MEGA_CREDS_FILE = _env_path(
    "MEGA_CREDS_FILE", Path.home() / ".config" / "mokuro-bridge" / "credentials.env"
)

# The local output directory is "sticky" the same way: an explicit `local_dir`
# on a local finalize becomes the remembered default for later local finalizes.
# It persists in this state file (under the work dir); OUTPUT_DIR is the
# fallback when nothing has been remembered yet.
_LOCAL_DIR_STATE_FILE = WORK_DIR / "local_dir_default.json"

def _load_remembered_local_dir() -> Optional[str]:
    """The persisted sticky local output dir, or None when unset/invalid."""
    try:
        data = json.loads(_LOCAL_DIR_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = str(data.get("local_dir", "")).strip()
    return raw or None

def _remember_local_dir(path_str: str) -> None:
    """Persist an explicit local_dir as the sticky default (non-fatal)."""
    if not path_str:
        return
    try:
        _LOCAL_DIR_STATE_FILE.write_text(
            json.dumps({"local_dir": path_str}), encoding="utf-8"
        )
        _LOCAL_DIR_STATE_FILE.chmod(0o600)
    except OSError:
        pass  # non-fatal

# Local path ingest (same-machine clients, e.g. headless scrapers).
_LOCAL_INGEST_ROOTS = [
    Path.home().resolve(),
    Path("/tmp").resolve(),
    Path("/private/tmp").resolve(),
    Path("/var/folders").resolve(),
    Path("/private/var/folders").resolve(),
]
