"""
mokuro-bridge — local OCR pipeline for manga captures → reader.mokuro.app.

A store-agnostic local HTTP service: any capture client (browser userscript,
headless scraper, curl, …) POSTs page images of a volume into a *session*;
the bridge OCRs them with mokuro and assembles the three artifacts
reader.mokuro.app consumes — <volume>.cbz, <volume>.mokuro, <volume>.webp —
into a per-series folder. Output can be kept locally or uploaded to MEGA.

Pipeline:
  POST /session/start              → create volume folder + session
  POST /session/{id}/page          → upload page bytes, queue OCR
  POST /session/{id}/page-local    → same-machine path ingest, queue OCR
  GET  /session/{id}/status        → capture/OCR progress
  GET  /sessions                   → list active sessions (multi-title)
  POST /session/{id}/finalize      → wait OCR, write .mokuro, pack, local and/or
                                     MEGA output (NDJSON progress stream)

OCR flushes in chunks (default 8 pages): detect each page, then one batched
recognize_text over all crops. Idle flush after ~1.5s if the chunk isn't full.
Env: OCR_CHUNK_SIZE, OCR_IDLE_FLUSH_S

Everything else is configurable through environment variables — see
MOKURO_BRIDGE_* / MEGA_* below and the README. MEGA upload is optional and
off by default; without it results land under the local output directory.

Requires: Python 3.10+, the stock mokuro package (`pip install mokuro`).
Optional: megatools + MEGA credentials for uploads.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ── Identity ──────────────────────────────────────────────────────────

APP_NAME = "mokuro-bridge"
__version__ = "0.3.0"

# ── OCR engine ────────────────────────────────────────────────────────
# By default the bridge uses the stock mokuro package from PyPI
# (`pip install mokuro`), imported normally from the environment.
#
# Optional override: set MOKURO_REPO to a mokuro checkout (e.g. an optimized
# fork) whose repo root is inserted on sys.path, so its `mokuro` package is
# used instead. The server detects at runtime which OCR API that mokuro
# instance supports and uses the matching code path (stock `mpocr(path)` vs
# fork `detect_and_extract`/`recognize_text`).
def _resolve_mokuro_repo() -> Optional[Path]:
    env_path = os.environ.get("MOKURO_REPO", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    sibling = Path(__file__).resolve().parent / "mokuro"
    if sibling.is_dir() and (sibling / "mokuro").is_dir():
        return sibling
    return None


_MOKURO_REPO = _resolve_mokuro_repo()
if _MOKURO_REPO is not None:
    # Repo root contains the `mokuro` package as <repo>/mokuro/
    sys.path.insert(0, str(_MOKURO_REPO))

# Resolve the mokuro package ONCE, up front. Keeping this module object and
# importing its submodules through it (instead of `import mokuro.X` by name)
# makes the choice deterministic — an editable-install meta-path finder can
# never swap in a different mokuro mid-process.
try:
    import mokuro as _mokuro_pkg
except ImportError:  # pragma: no cover - health reports this
    _mokuro_pkg = None


def _mokuro_submodule(name: str):
    """Import a submodule of the pinned mokuro package."""
    return importlib.import_module(f"{_mokuro_pkg.__name__}.{name}")


app = FastAPI(title=APP_NAME, version=__version__)

# Browser-based clients (userscripts) POST straight from the storefront
# origin. Restrict to an allow-list via CORS_ORIGINS (comma separated);
# the default keeps the original BookWalker viewers working out of the box.
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

# ── Paths / behaviour ─────────────────────────────────────────────────

def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


# Working dir: page images + OCR JSON while a volume is being processed.
WORK_DIR = _env_path("MOKURO_BRIDGE_WORK_DIR", Path.home() / "mokuro-input")
# Local output dir: finished <series>/<volume>.{cbz,mokuro,webp} when MEGA
# upload is disabled (default). Git-ignored when inside the repo checkout.
OUTPUT_DIR = _env_path("MOKURO_BRIDGE_OUTPUT_DIR", Path(__file__).resolve().parent / "output")

WORK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR = WORK_DIR / ".bridge_sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
IMAGE_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png"}

# MEGA (optional destination). Uploads default OFF for a friendly local
# first run; enable globally via MOKURO_BRIDGE_UPLOAD_DEFAULT=true or per
# request with upload_to_mega=true.
MEGA_LIBRARY_ROOT = os.environ.get("MEGA_LIBRARY_ROOT", "/Root/mokuro-reader")
_MEGA_UPLOAD_DEFAULT = str(
    os.environ.get("MOKURO_BRIDGE_UPLOAD_DEFAULT", "false")
).lower() in ("1", "true", "yes", "on")

# Refuse MEGA upload when a volume has fewer pages than this (guards against
# free-viewer errors / premature end uploading junk CBZs). Local mode keeps
# whatever was captured.
MIN_PAGES_FOR_MEGA = max(1, int(os.environ.get("MIN_PAGES_FOR_MEGA", "10")))

# Google Drive (optional destination via google-api-python-client).
# Auth is OAuth2: creds live in DRIVE_CREDS_FILE (0600), created by
# `python server.py --setup-upload drive`. A service-account JSON also works.
DRIVE_ROOT_NAME = os.environ.get("DRIVE_ROOT_NAME", "mokuro-reader")  # folder at My Drive root
DRIVE_CREDS_FILE = _env_path(
    "DRIVE_CREDS_FILE", Path.home() / ".config" / "mokuro-bridge" / "drive_credentials.json"
)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_DRIVE_CLIENT_SECRET_ENV = "DRIVE_CLIENT_SECRET_FILE"

# WebDAV (optional destination; works with Nextcloud, ownCloud, Seafile, ...).
# Base URL is the DAV root the server exposes (e.g. Nextcloud:
# https://host/remote.php/dav/files/<username>). The bridge creates/uses
# <base>/mokuro-reader/<Series>/ under it.
WEBDAV_BASE_URL = os.environ.get("WEBDAV_BASE_URL", "").strip()
WEBDAV_ROOT_NAME = os.environ.get("WEBDAV_ROOT_NAME", "mokuro-reader")
WEBDAV_CREDS_FILE = _env_path(
    "WEBDAV_CREDS_FILE", Path.home() / ".config" / "mokuro-bridge" / "webdav.env"
)

# OneDrive (optional destination via MS Graph, msal device-code auth).
ONEDRIVE_ROOT_NAME = os.environ.get("ONEDRIVE_ROOT_NAME", "mokuro-reader")
ONEDRIVE_CLIENT_ID = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip()
ONEDRIVE_TOKEN_FILE = _env_path(
    "ONEDRIVE_TOKEN_FILE", Path.home() / ".config" / "mokuro-bridge" / "onedrive_token.json"
)
_ONEDRIVE_SCOPES = ["https://graph.microsoft.com/Files.ReadWrite", "offline_access"]
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# ── Upload methods ─────────────────────────────────────────────────────
# Multi-destination ("upload method") architecture: every destination — the
# no-upload `local` method or a remote provider like MEGA — is described by
# an UploadMethod entry. Future providers (drive, onedrive, webdav) register
# here the same way. The registry dict itself is built lazily by
# _build_upload_methods() (defined with the MEGA helpers below) so that
# resolved-at-runtime values (tool availability, credentials) stay current.

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

# Chunked OCR flush: pages are grouped into batches so the queue drains in
# controlled chunks. Override via env.
_OCR_CHUNK_SIZE = max(1, int(os.environ.get("OCR_CHUNK_SIZE", "8")))
_OCR_IDLE_FLUSH_S = float(os.environ.get("OCR_IDLE_FLUSH_S", "1.5"))

# MEGA credentials, resolved in order: env vars → creds file → OS keychain.
MEGA_CREDS_FILE = _env_path(
    "MEGA_CREDS_FILE", Path.home() / ".config" / "mokuro-bridge" / "credentials.env"
)

_generator_lock = threading.Lock()
_assemble_lock = threading.Lock()
_mega_lock = threading.Lock()
_generator = None  # lazy MokuroGenerator

# Global OCR flush queue (shared across sessions / capture clients)
_ocr_lock = threading.Lock()
_ocr_cv = threading.Condition(_ocr_lock)
_ocr_queue: deque[tuple[str, str]] = deque()  # (session_id, filename)
_ocr_processing: set[tuple[str, str]] = set()
_ocr_force_sessions: set[str] = set()  # finalize wants these drained ASAP
_ocr_worker_started = False

# Local path ingest (same-machine clients, e.g. headless scrapers).
_LOCAL_INGEST_ROOTS = [
    Path.home().resolve(),
    Path("/tmp").resolve(),
    Path("/private/tmp").resolve(),
    Path("/var/folders").resolve(),
    Path("/private/var/folders").resolve(),
]


def _truthy(value: str | None) -> bool:
    """Parse a form/env string as boolean; unset/empty → False."""
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _fork_supported() -> bool:
    """Whether the loaded mokuro exposes the fork's batched OCR API.

    A fork adds `detect_and_extract` / `recognize_text` to MangaPageOcr (plus
    an `ocr_batch_size` instance attribute on the generator); stock mokuro
    only offers `mpocr(path)`. We feature-detect so either install works.
    """
    try:
        generator_mod = _mokuro_submodule("mokuro_generator")
        page_ocr_mod = _mokuro_submodule("manga_page_ocr")
    except (ImportError, AttributeError):
        return False
    page_ocr_has_fork_api = (
        hasattr(page_ocr_mod.MangaPageOcr, "detect_and_extract")
        and hasattr(page_ocr_mod.MangaPageOcr, "recognize_text")
    )
    generator_has_batch_size = hasattr(generator_mod.MokuroGenerator(), "ocr_batch_size")
    return page_ocr_has_fork_api and generator_has_batch_size


def _get_generator():
    """Lazy-init the mokuro engine (models stay warm across pages/sessions)."""
    global _generator
    with _generator_lock:
        if _generator is None:
            MokuroGenerator = _mokuro_submodule("mokuro_generator").MokuroGenerator

            _generator = MokuroGenerator()
            _generator.init_models()
        return _generator


# ── Session state ──────────────────────────────────────────────────────


@dataclass
class Session:
    session_id: str
    title: str
    safe_title: str
    vol_dir: Path
    pages_received: set[str] = field(default_factory=set)
    pages_ocr_done: set[str] = field(default_factory=set)
    pages_ocr_failed: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    message: str = "Session started"
    finalized: bool = False


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _persist_session(session: Session) -> None:
    try:
        with session.lock:
            data = {
                "session_id": session.session_id,
                "title": session.title,
                "safe_title": session.safe_title,
                "vol_dir": str(session.vol_dir),
                "pages_received": sorted(session.pages_received),
                "pages_ocr_done": sorted(session.pages_ocr_done),
                "pages_ocr_failed": sorted(session.pages_ocr_failed),
                "message": session.message,
                "finalized": session.finalized,
            }
        (SESSIONS_DIR / f"{session.session_id}.json").write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[mokuro-bridge] persist session failed: {e}")


def _delete_persisted_session(session_id: str) -> None:
    path = SESSIONS_DIR / f"{session_id}.json"
    path.unlink(missing_ok=True)


def ocr_json_path(volume, img_name: str) -> Path:
    """Per-page OCR JSON path in mokuro's cache layout.

    mokuro keeps one JSON per page under <volume_parent>/_ocr/<volume_name>/
    (same name as the image, .json extension). This matches what the stock
    mokuro package writes and reads, so the bridge and mokuro interoperate.
    """
    return (volume.path_ocr_cache / img_name).with_suffix(".json")


def _sync_ocr_cache_state(session: Session) -> None:
    """Mark pages that already have OCR JSON as done (supports resume after crash)."""
    volume_mod = _mokuro_submodule("volume")
    Volume = volume_mod.Volume

    try:
        volume = Volume(session.vol_dir)
    except Exception:
        return

    if not session.vol_dir.is_dir():
        session.vol_dir.mkdir(parents=True, exist_ok=True)

    images = sorted(
        p.name
        for p in session.vol_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )
    image_set = set(images)
    with session.lock:
        # Drop ghost entries after MEGA cleanup / deleted volume dirs.
        session.pages_received &= image_set
        session.pages_ocr_done &= image_set
        session.pages_ocr_failed &= image_set
        for name in images:
            session.pages_received.add(name)
            json_path = ocr_json_path(volume, name)
            if json_path.is_file():
                session.pages_ocr_done.add(name)
                session.pages_ocr_failed.discard(name)
        done = len(session.pages_ocr_done)
        total = len(session.pages_received)
        session.message = f"Resumed — OCR {done}/{total} cached"


def _load_persisted_session(session_id: str) -> Optional[Session]:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    vol_dir = Path(data.get("vol_dir", ""))
    if not vol_dir.is_dir():
        return None
    session = Session(
        session_id=data["session_id"],
        title=data.get("title") or vol_dir.name,
        safe_title=data.get("safe_title") or vol_dir.name,
        vol_dir=vol_dir,
        pages_received=set(data.get("pages_received") or []),
        pages_ocr_done=set(data.get("pages_ocr_done") or []),
        pages_ocr_failed=set(data.get("pages_ocr_failed") or []),
        message=data.get("message") or "Restored session",
        finalized=bool(data.get("finalized")),
    )
    _sync_ocr_cache_state(session)
    print(f"[mokuro-bridge] restored session {session_id} from disk ({session.vol_dir.name})")
    return session


def _get_session(session_id: str) -> Session:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session:
            return session
    session = _load_persisted_session(session_id)
    if session:
        if session.finalized:
            raise HTTPException(status_code=400, detail="Session already finalized")
        with _sessions_lock:
            _sessions[session_id] = session
        return session
    raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")


def _session_or_none(session_id: str) -> Optional[Session]:
    try:
        return _get_session(session_id)
    except HTTPException:
        return None


def _find_session_by_safe_title(safe_title: str) -> Optional[Session]:
    with _sessions_lock:
        for session in _sessions.values():
            if session.safe_title == safe_title:
                return session
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("safe_title") != safe_title:
            continue
        sid = data["session_id"]
        session = _load_persisted_session(sid)
        if session:
            with _sessions_lock:
                _sessions[sid] = session
            return session
    return None


# ── MEGA / helpers ─────────────────────────────────────────────────────


# ── MEGA credentials ──────────────────────────────────────────────────
# Resolution order:
#   1. MEGA_EMAIL / MEGA_PASSWORD env vars
#   2. MEGA_CREDS_FILE (KEY=VALUE, chmod 600)
#   3. the OS credential store, when one is available:
#        macOS  — Keychain (via `security`, or `keyring`)
#        Windows— Credential Manager (via `keyring`)
#        Linux  — Secret Service / gnome-keyring (via `keyring`)
# All optional: MEGA upload is disabled by default and can be skipped entirely.

_KEYRING_SERVICE = "mokuro-bridge"  # namespace used for keyring-based entries


def _keyring():
    """Best-effort import of the optional `keyring` package.

    Returns the module when a usable backend is configured, else None (callers
    then fall back to the credentials file / macOS `security`).
    """
    try:
        import keyring

        keyring.get_keyring()  # raises if no backend is available
        return keyring
    except Exception:
        return None


def _read_creds_file() -> Optional[tuple[str, str]]:
    try:
        text = MEGA_CREDS_FILE.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    email = password = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip().upper() == "MEGA_EMAIL":
            email = value
        elif key.strip().upper() == "MEGA_PASSWORD":
            password = value
    if email and password:
        return email, password
    return None


def _write_creds_file(email: str, password: str) -> None:
    MEGA_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEGA_CREDS_FILE.write_text(
        f"# mokuro-bridge MEGA credentials — keep this file private.\n"
        f"MEGA_EMAIL={email}\nMEGA_PASSWORD={password}\n",
        encoding="utf-8",
    )
    try:
        MEGA_CREDS_FILE.chmod(0o600)
    except OSError:
        pass


def _keychain_mega_creds() -> Optional[tuple[str, str]]:
    """Look up MEGA credentials in the OS credential store.

    Tries, in order: macOS Keychain via `security` (keeps setup-keychain.sh
    entries working), then any `keyring` backend (macOS Keychain, Windows
    Credential Manager, Linux Secret Service).
    """
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-internet-password", "-s", "mega.nz", "-r", "htps", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                password = result.stdout.strip()
                acct_result = subprocess.run(
                    ["security", "find-internet-password", "-s", "mega.nz", "-r", "htps", "-g"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                email_match = re.search(r'"acct"<blob>="([^"]+)"', acct_result.stdout)
                if email_match:
                    return email_match.group(1), password
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # fall through to keyring

    kr = _keyring()
    if kr is not None:
        try:
            email = kr.get_password(_KEYRING_SERVICE, "email")
            if email:
                password = kr.get_password(_KEYRING_SERVICE, email)
                if password:
                    return email, password
        except Exception:
            pass
    return None


def _store_mega_creds_keychain(email: str, password: str) -> None:
    # macOS only: store via the `security` binary (setup-keychain.sh parity).
    subprocess.run(
        [
            "security",
            "add-internet-password",
            "-s", "mega.nz",
            "-r", "htps",
            "-a", email,
            "-w", password,
            "-T", "/usr/bin/security",
            "-U",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )


def _store_mega_creds_os(email: str, password: str) -> str:
    """Store credentials in the OS credential store.

    Returns the backend name. Raises RuntimeError when no usable store is
    available (caller falls back to the credentials file).
    """
    if sys.platform == "darwin":
        try:
            _store_mega_creds_keychain(email, password)
            return "macOS Keychain"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass  # fall through to keyring
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_KEYRING_SERVICE, "email", email)
            kr.set_password(_KEYRING_SERVICE, email, password)
            return "system keyring"
        except Exception as exc:  # pragma: no cover - backend-specific
            raise RuntimeError(f"system keyring store failed: {exc}") from exc
    raise RuntimeError("no OS credential store is available")


def _mega_creds_source() -> Optional[str]:
    """Where creds would come from: 'env', 'file', 'keychain', or None."""
    if os.environ.get("MEGA_EMAIL", "").strip() and os.environ.get(
        "MEGA_PASSWORD", ""
    ).strip():
        return "env"
    if _read_creds_file() is not None:
        return "file"
    if _keychain_mega_creds() is not None:
        return "keychain"
    return None


def _mega_configured() -> bool:
    """Whether the MEGA upload method is usable right now."""
    return bool(shutil.which("megatools")) and _mega_creds_source() is not None


def _get_mega_creds() -> tuple[str, str]:
    env_email = os.environ.get("MEGA_EMAIL", "").strip()
    env_password = os.environ.get("MEGA_PASSWORD", "").strip()
    if env_email and env_password:
        return env_email, env_password
    file_creds = _read_creds_file()
    if file_creds is not None:
        return file_creds
    keychain_creds = _keychain_mega_creds()
    if keychain_creds is not None:
        return keychain_creds
    raise RuntimeError(
        "MEGA credentials not configured. Set MEGA_EMAIL + MEGA_PASSWORD "
        "environment variables, run `python server.py --setup-mega` (stores "
        "them in your OS keychain/credential store or a 0600 file), or write "
        "them to the credentials file."
    )


def _run_setup_mega() -> None:
    """Interactive first-run wizard: ask for MEGA credentials and store them."""
    import getpass

    print("MEGA upload setup for mokuro-bridge")
    print("-" * 40)
    existing = _mega_creds_source()
    if existing:
        answer = input(
            f"MEGA credentials already found ({existing}). Overwrite? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing credentials.")
            return

    if os.environ.get("MEGA_EMAIL", "") and os.environ.get("MEGA_PASSWORD", ""):
        print(
            "MEGA_EMAIL/MEGA_PASSWORD are set in the environment — the wizard "
            "cannot (and should not) override those. Export them instead."
        )
        return

    email = input("MEGA email: ").strip()
    if not email:
        print("No email given — aborting.")
        return
    password = getpass.getpass("MEGA password: ")

    try:
        backend = _store_mega_creds_os(email, password)
    except RuntimeError as exc:
        print(f"{exc}; falling back to a credentials file.")
    else:
        print(f"Stored in {backend}.")
        return
    _write_creds_file(email, password)
    print(f"Stored in {MEGA_CREDS_FILE} (permissions 0600).")
    print(
        "Tip: you can also use environment variables MEGA_EMAIL / MEGA_PASSWORD "
        "instead of storing anything."
    )


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")[:200]


_VOLUME_MARKERS = [
    # （１） (1) （上） etc.
    re.compile(r"\s*[（(]\s*[0-9０-９一二三四五六七八九十百上下中全]+\s*[）)]\s*$"),
    # 1巻 第2巻 １２巻
    re.compile(r"\s*第?\s*[0-9０-９一二三四五六七八九十百]+\s*巻\s*$"),
    # Vol.1 vol 2
    re.compile(r"\s*[Vv]ol\.?\s*[0-9０-９]+\s*$"),
    # trailing volume number after space: "…　6" / "… 3"
    re.compile(r"[\s　]+[0-9０-９]{1,3}\s*$"),
]


def series_title_from_volume(title: str) -> str:
    """
    Derive a shared series folder name from a volume title.

    推しが武道館いってくれたら死ぬ（２）【電子限定特典ペーパー付き】
      → 推しが武道館いってくれたら死ぬ
    メダリスト 1巻 → メダリスト
    「おかえり、パパ」【電子単行本】　6 → 「おかえり、パパ」
    """
    s = (title or "").strip()
    # Edition/bonus tags are volume-specific — drop them for the series folder.
    s = re.sub(r"[【〔\[][^】〕\]]*[】〕\]]", "", s)
    s = re.sub(r"[\s　]+", " ", s).strip()
    for pat in _VOLUME_MARKERS:
        nxt = pat.sub("", s).strip()
        if nxt and nxt != s:
            s = nxt
            break
    s = re.sub(r"[\s　]+", " ", s).strip(" -–—_|")
    return sanitize_filename(s) or sanitize_filename(title) or "manga"


def mega_series_dir(volume_title: str) -> str:
    """Remote MEGA folder for a volume: /Root/mokuro-reader/<series>/."""
    series = series_title_from_volume(volume_title)
    return f"{MEGA_LIBRARY_ROOT}/{series}"


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


def create_megarc(email: str, password: str) -> Path:
    # megatools requires a [Login] section (not [DEFAULT]) with Username=
    megarc = WORK_DIR / f".megarc_{uuid.uuid4().hex[:8]}"
    megarc.write_text(f"[Login]\nUsername = {email}\nPassword = {password}\n")
    megarc.chmod(0o600)
    return megarc


def mega_mkdir(megarc_path: Path, remote_dir: str) -> subprocess.CompletedProcess:
    # mkdir takes remote paths as positional args (no --path)
    return subprocess.run(
        ["megatools", "mkdir", "--config", str(megarc_path), remote_dir],
        capture_output=True,
        text=True,
        timeout=30,
    )


def mega_put(megarc_path: Path, local_path: Path, remote_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "megatools",
            "put",
            "--config",
            str(megarc_path),
            "--path",
            remote_path,
            str(local_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )


# Progress lines from `megatools put` (progress bar disabled when stdout is not
# a TTY — each update becomes a newline-terminated plain line, ~1/sec):
#   My Manga 1巻.cbz: 42.50% - 12.4 MiB of 29.2 MiB (5.2 MiB/s)
#   My Manga 1巻.cbz: 100.00% - done 29.2 MiB (avg. 5.2 MiB/s)
# and the completion line:
#   Uploaded My Manga 1巻.cbz
_MEGATOOLS_PROGRESS_RE = re.compile(
    r"^([^:]+):\s+(\d+(?:\.\d+)?)%\s*-\s*(.*?)(?:\s+\(([^)]+)\))?$"
)
_MEGATOOLS_UPLOADED_RE = re.compile(r"^Uploaded\s+(.+)$")


def _parse_megatools_size(s: str) -> int:
    """Parse a megatools human size ("29.2 MiB", "4.0 KiB", "1024 B") → bytes."""
    s = s.strip()
    m = re.match(r"^([\d.]+)\s*([A-Za-z]*)$", s)
    if not m:
        return 0
    value, unit = float(m.group(1)), m.group(2).upper()
    factors = {
        "": 1,
        "B": 1,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
    }
    return int(value * factors.get(unit, 1))


def _parse_megatools_speed(s: str) -> int:
    """Parse "5.2 MiB/s" → bytes per second (0 when unparseable)."""
    return _parse_megatools_size(s.rstrip("/s"))


def _mega_upload_file(
    megarc_path: Path,
    local_path: Path,
    remote_path: str,
    on_progress: Optional[callable],
) -> tuple[bool, Optional[str]]:
    """Upload one file with `megatools put`, streaming progress.

    Runs megatools with stdout piped (stderr still goes to the process's
    stderr). Progress is throttled by megatools to ~1 update/second and each
    line is flushed by glib, so `on_progress(bytes_done, total_bytes,
    speed_bps)` fires live (the caller derives the percent). Returns
    (success, error_message).
    """
    total_bytes = local_path.stat().st_size
    proc = subprocess.Popen(
        [
            "megatools",
            "put",
            "--config",
            str(megarc_path),
            "--path",
            remote_path,
            str(local_path),
        ],
        stdout=subprocess.PIPE,
        stderr=None,  # inherit → megatools errors land on our stderr
        text=True,
        bufsize=1,  # line-buffered reads
    )
    assert proc.stdout is not None
    success = False
    error_msg: Optional[str] = None
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            m = _MEGATOOLS_UPLOADED_RE.match(line)
            if m:
                success = True
                if on_progress:
                    on_progress(total_bytes, total_bytes, 0)
                continue
            m = _MEGATOOLS_PROGRESS_RE.match(line)
            if m and on_progress:
                rest = m.group(3)
                speed_bps = _parse_megatools_speed(m.group(4) or "")
                # "12.4 MiB of 29.2 MiB" → done/total
                size_match = re.match(r"^(.+?)\s+of\s+(.+)$", rest)
                if size_match:
                    bytes_done = min(_parse_megatools_size(size_match.group(1)), total_bytes)
                    on_progress(bytes_done, total_bytes, speed_bps)
                elif rest.startswith("done "):
                    on_progress(total_bytes, total_bytes, speed_bps)
        proc.wait(timeout=300)
    except Exception as e:
        error_msg = str(e)
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
    if not success and error_msg is None:
        error_msg = f"megatools exited with code {proc.returncode}" if proc.returncode else "no completion line"
    return success, error_msg


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


# ── Google Drive helpers (optional destination) ────────────────────────
# All google imports below are LAZY (inside functions) so server.py imports
# cleanly without the libraries; each function raises a clear RuntimeError
# telling the user how to install them.
_DRIVE_IMPORT_HINT = (
    "Google Drive upload needs the google client libraries. "
    "Install them with: pip install -r requirements-drive.txt"
)
_WEBDAV_IMPORT_HINT = (
    "WebDAV upload needs the requests library. "
    "Install it with: pip install requests"
)
_ONEDRIVE_IMPORT_HINT = (
    "OneDrive upload needs the msal and requests libraries. "
    "Install them with: pip install msal requests"
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
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Google Drive setup needs google_auth_oauthlib. Install it with:\n"
            "  pip install -r requirements-drive.txt"
        )
        return

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


# ── WebDAV helpers (optional destination) ──────────────────────────────
# Uploads go over HTTP(S) with requests (lazy-imported). The base URL is not
# secret; credentials resolve in order: env vars → OS keychain (keyring) →
# the 0600 JSON creds file (which also persists the base URL). The setup
# wizard writes the creds file and, when keyring is available, also stores
# username/password there so the file only carries the URL on those machines.
_WEBDAV_KEYRING_USERNAME = "webdav:username"
_WEBDAV_KEYRING_PASSWORD = "webdav:password"


def _webdav_username() -> Optional[str]:
    """WebDAV username: env WEBDAV_USERNAME → keyring 'webdav:username'."""
    env = os.environ.get("WEBDAV_USERNAME", "").strip()
    if env:
        return env
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_USERNAME)
            if value:
                return value
        except Exception:
            pass
    creds = _read_webdav_creds_file()
    if creds:
        return creds.get("username") or None
    return None


def _webdav_password() -> Optional[str]:
    """WebDAV password: env WEBDAV_PASSWORD → keyring 'webdav:password'."""
    env = os.environ.get("WEBDAV_PASSWORD", "").strip()
    if env:
        return env
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_PASSWORD)
            if value:
                return value
        except Exception:
            pass
    creds = _read_webdav_creds_file()
    if creds:
        return creds.get("password") or None
    return None


def _webdav_configured() -> bool:
    """Whether the WebDAV method is usable right now."""
    return (
        bool(WEBDAV_BASE_URL)
        and bool(_webdav_username())
        and bool(_webdav_password())
        and importlib.util.find_spec("requests") is not None
    )


def _webdav_base_url() -> str:
    """The configured DAV base URL with any trailing slash stripped."""
    return WEBDAV_BASE_URL.rstrip("/")


def _read_webdav_creds_file() -> Optional[dict]:
    """Read WEBDAV_CREDS_FILE (JSON: base_url/username/password) or None."""
    try:
        data = json.loads(WEBDAV_CREDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_webdav_creds_file(url: str, username: str, password: str) -> None:
    """Persist WebDAV URL + credentials as JSON (0600)."""
    WEBDAV_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEBDAV_CREDS_FILE.write_text(
        json.dumps(
            {"base_url": url, "username": username, "password": password},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        WEBDAV_CREDS_FILE.chmod(0o600)
    except OSError:
        pass


def _run_setup_webdav() -> None:
    """Interactive first-run wizard: capture base URL + credentials and store.

    Resolution order at runtime: env vars (WEBDAV_USERNAME / WEBDAV_PASSWORD)
    → OS keychain (keyring) → WEBDAV_CREDS_FILE. The base URL is read from
    WEBDAV_BASE_URL (required) and persisted into the creds file so the setup
    survives restarts; when keyring is available the username/password are
    ALSO stored there and the file only holds the URL.
    """
    try:
        import getpass
    except ImportError:
        getpass = None

    print("WebDAV upload setup for mokuro-bridge")
    print("-" * 40)
    if not WEBDAV_BASE_URL:
        print(
            "No WebDAV base URL configured. Set the WEBDAV_BASE_URL "
            "environment variable (e.g. "
            "https://host/remote.php/dav/files/<username>) and re-run."
        )
        return

    print(f"Base URL: {WEBDAV_BASE_URL}")
    print(
        "Leave a prompt empty to keep the currently configured value "
        "(env / keyring / file)."
    )

    username = os.environ.get("WEBDAV_USERNAME", "").strip() or _webdav_username() or ""
    answer = input(f"Username [{username}]: ").strip()
    if answer:
        username = answer
    if not username:
        print("No username given — aborting.")
        return

    password = os.environ.get("WEBDAV_PASSWORD", "").strip() or _webdav_password() or ""
    if getpass is not None:
        entered = getpass.getpass("Password (empty = keep current): ")
    else:
        entered = input("Password (empty = keep current): ")
    if entered:
        password = entered
    if not password:
        print("No password given — aborting.")
        return

    stored = []
    _write_webdav_creds_file(WEBDAV_BASE_URL, username, password)
    stored.append(str(WEBDAV_CREDS_FILE))
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_USERNAME, username)
            kr.set_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_PASSWORD, password)
            stored.append("system keyring")
        except Exception as exc:  # pragma: no cover - backend-specific
            print(f"note: could not store in the system keyring: {exc}")
    print(
        "Stored WebDAV credentials in: "
        + ", ".join(stored)
        + " (creds file is 0600)."
    )
    print(
        "Tip: you can also use environment variables WEBDAV_USERNAME / "
        "WEBDAV_PASSWORD instead of storing anything."
    )


class _CountingBody:
    """File-like body that counts bytes as requests streams it, throttled.

    `requests` reads via `.read()` and sets Content-Length from `.len`.
    on_progress(bytes_done, total_bytes, speed_bps) fires at most every
    0.25s (or immediately on start/finish), mirroring the caller-side
    throttle in _upload_batch.
    """

    def __init__(self, path, total, on_progress):
        self._f = open(path, "rb")
        self.len = total
        self.done = 0
        self._on_progress = on_progress
        self._last_t = time.monotonic()
        self._last_done = 0
        self._last_emit = 0.0

    def read(self, n=-1):
        chunk = self._f.read(n if n and n > 0 else 1 << 16)
        if chunk:
            self.done += len(chunk)
            now = time.monotonic()
            dt = now - self._last_t
            speed = int((self.done - self._last_done) / dt) if dt > 0 else 0
            self._last_t, self._last_done = now, self.done
            if (
                self.done >= self.len
                or self.done <= 0
                or now - self._last_emit >= 0.25
            ):
                self._last_emit = now
                self._on_progress(self.done, self.len, speed)
        return chunk

    def close(self):
        self._f.close()


def _webdav_mkcol(sess, url: str) -> bool:
    """Best-effort MKCOL; returns True when the collection exists/ok.

    Accepts 201 (created), 301 (moved), 405 (exists, method not allowed on
    an existing collection), 409 (already exists / conflict) as
    "exists/ok"; anything else returns False.
    """
    try:
        resp = sess.request("MKCOL", url, timeout=30)
    except Exception:
        return False
    return resp.status_code in (201, 204, 301, 405, 409)


def _webdav_upload_file(
    base_url: str,
    username: str,
    password: str,
    local_path: Path,
    remote_dir: str,
    on_progress: Optional[callable],
) -> tuple[bool, Optional[str]]:
    """PUT one file over WebDAV under <base_url>/<remote_dir>/<name>.

    Ensures the parent collections exist (idempotent MKCOL per segment),
    then streams the PUT through a counting body so on_progress fires with
    live byte/speed deltas. Returns (success, error_message).
    """
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(_WEBDAV_IMPORT_HINT) from exc
    base = base_url.rstrip("/")
    remote_dir = str(remote_dir or "").strip("/")
    if not base:
        raise RuntimeError(
            "WebDAV base URL not configured. Set WEBDAV_BASE_URL or run "
            "`python server.py --setup-upload webdav`."
        )
    total = local_path.stat().st_size
    remote_url = f"{base}/{remote_dir}/{local_path.name}"

    sess = requests.Session()
    sess.auth = (username, password)  # preemptive Basic auth
    sess.headers["Content-Type"] = "application/octet-stream"

    # Ensure parent collections exist (root, then each series segment).
    accumulated = base
    for seg in remote_dir.split("/"):
        if not seg:
            continue
        accumulated = f"{accumulated}/{seg}"
        if not _webdav_mkcol(sess, accumulated):
            return False, f"WebDAV MKCOL failed for {accumulated}"

    body = None
    try:
        if on_progress is None:
            with open(local_path, "rb") as f:
                resp = sess.put(remote_url, data=f, timeout=None)
        else:
            body = _CountingBody(local_path, total, on_progress)
            resp = sess.put(remote_url, data=body, timeout=None)
        if resp.status_code in (200, 201, 204):
            if on_progress:
                on_progress(total, total, 0)
            return True, None
        text = (resp.text or "").strip()
        return False, f"WebDAV PUT {resp.status_code}: {text[:300] or 'no detail'}"
    except Exception as exc:
        return False, str(exc)
    finally:
        if body is not None:
            body.close()


# ── OneDrive helpers (optional destination) ────────────────────────────
# Auth = msal PublicClientApplication device-code flow; the token cache is
# persisted as JSON in ONEDRIVE_TOKEN_FILE (0600). All msal/requests imports
# are LAZY so server.py imports cleanly without them.


def _onedrive_configured() -> bool:
    """Whether the OneDrive method is usable right now."""
    return (
        importlib.util.find_spec("msal") is not None
        and bool(ONEDRIVE_CLIENT_ID)
        and ONEDRIVE_TOKEN_FILE.is_file()
    )


def _load_token_cache():
    """Load ONEDRIVE_TOKEN_FILE into a SerializableTokenCache (empty if absent)."""
    try:
        from msal import SerializableTokenCache
    except ImportError as exc:
        raise RuntimeError(_ONEDRIVE_IMPORT_HINT) from exc
    cache = SerializableTokenCache()
    try:
        data = ONEDRIVE_TOKEN_FILE.read_text(encoding="utf-8")
    except OSError:
        return cache
    if data.strip():
        try:
            cache.deserialize(data)
        except Exception:
            pass  # corrupt cache — start over
    return cache


def _save_token_cache(cache) -> None:
    """Persist the msal token cache to ONEDRIVE_TOKEN_FILE (0600)."""
    if not cache.has_state_changed:
        return
    ONEDRIVE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    ONEDRIVE_TOKEN_FILE.write_text(cache.serialize(), encoding="utf-8")
    try:
        ONEDRIVE_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _onedrive_msal_app():
    """Build the msal PublicClientApplication with the persisted cache."""
    try:
        from msal import PublicClientApplication
    except ImportError as exc:
        raise RuntimeError(_ONEDRIVE_IMPORT_HINT) from exc
    cache = _load_token_cache()
    app = PublicClientApplication(
        client_id=ONEDRIVE_CLIENT_ID,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    return app, cache


def _onedrive_token() -> str:
    """Silently acquire a valid access token (or raise with a re-setup hint).

    Raises RuntimeError (with a `--setup-upload onedrive` hint) when no
    account/token is cached or the cached refresh token is no longer usable.
    """
    app, cache = _onedrive_msal_app()
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent_with_error(
            _ONEDRIVE_SCOPES, account=accounts[0]
        )
        _save_token_cache(cache)
    if result and "access_token" in result:
        return result["access_token"]
    if accounts and result and "error" in result:
        raise RuntimeError(
            f"OneDrive token refresh failed ({result.get('error')}: "
            f"{result.get('error_description', '')}). Re-run `python "
            "server.py --setup-upload onedrive` to sign in again."
        )
    raise RuntimeError(
        "OneDrive not authorized. Run `python server.py --setup-upload "
        "onedrive` to sign in (device-code flow)."
    )


def _run_setup_onedrive() -> None:
    """Interactive device-code wizard: sign in and persist the token cache."""
    try:
        from msal import PublicClientApplication
    except ImportError:
        print(
            "OneDrive setup needs the msal library. Install it with:\n"
            "  pip install msal requests"
        )
        return

    print("OneDrive upload setup for mokuro-bridge")
    print("-" * 40)
    client_id = ONEDRIVE_CLIENT_ID or input("Azure app client ID: ").strip()
    if not client_id:
        print(
            "No client ID given. In the Azure portal:\n"
            "  1. Create an app registration\n"
            "  2. Add the 'Mobile and desktop applications' platform and "
            "enable public client flows\n"
            "  3. Add the delegated Microsoft Graph permission "
            "'Files.ReadWrite'\n"
            "  4. Copy the Application (client) ID into "
            "ONEDRIVE_CLIENT_ID (env) or enter it above."
        )
        return

    cache = _load_token_cache()
    app = PublicClientApplication(
        client_id=client_id,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    flow = app.initiate_device_flow(scopes=_ONEDRIVE_SCOPES)
    if "user_code" not in flow:
        print(f"error: could not start device flow: {flow.get('error_description', flow)}")
        return
    print(flow.get("message", ""))
    print("Waiting for you to sign in…")
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print(
            "error: sign-in failed: "
            f"{result.get('error')}: {result.get('error_description', '')}"
        )
        return
    _save_token_cache(cache)
    print(
        f"OneDrive token stored in {ONEDRIVE_TOKEN_FILE} (permissions 0600)."
    )


def _onedrive_headers() -> dict:
    """Authorization headers for Graph calls."""
    return {"Authorization": f"Bearer {_onedrive_token()}"}


def _graph_request(sess, method: str, url: str, **kwargs):
    """Graph call wrapper that surfaces non-2xx as RuntimeError."""
    resp = sess.request(method, url, timeout=30, **kwargs)
    if resp.status_code not in (200, 201, 202, 204):
        try:
            detail = resp.json()
        except ValueError:
            detail = (resp.text or "")[:300]
        raise RuntimeError(
            f"Graph {method} {url} → {resp.status_code}: {detail}"
        )
    return resp


def _onedrive_ensure_folder(
    sess, headers: dict, parent_id: str, name: str
) -> str:
    """Find (or create) a folder named `name` under parent_id; return its id.

    GET 200 → exists; GET 404 → create via POST children; create 409 →
    already exists → re-GET to resolve the id. Any other status raises.
    """
    quoted = urllib.parse.quote(name, safe="")
    if parent_id == "root":
        check_url = f"{_GRAPH_BASE}/me/drive/root:/{quoted}:"
        create_url = f"{_GRAPH_BASE}/me/drive/root/children"
    else:
        check_url = f"{_GRAPH_BASE}/me/drive/items/{parent_id}:/{quoted}:"
        create_url = f"{_GRAPH_BASE}/me/drive/items/{parent_id}/children"
    resp = sess.get(check_url, headers=headers, timeout=30)
    if resp.status_code == 200:
        info = resp.json()
        if info.get("folder") is not None or info.get("id"):
            return info["id"]
        raise RuntimeError(f"Graph path exists but is not a folder: {name}")
    if resp.status_code != 404:
        raise RuntimeError(
            f"Graph GET {check_url} → {resp.status_code}: "
            f"{(resp.text or '')[:300]}"
        )
    # Missing → create.
    create_resp = sess.post(
        create_url,
        headers={**headers, "Content-Type": "application/json"},
        json={"name": name, "folder": {}},
        timeout=30,
    )
    if create_resp.status_code in (200, 201):
        return create_resp.json()["id"]
    if create_resp.status_code == 409:
        # Raced with another creator — resolve the existing id.
        retry = sess.get(check_url, headers=headers, timeout=30)
        if retry.status_code == 200:
            return retry.json()["id"]
        raise RuntimeError(
            f"Graph create {name} conflicted (409) and re-GET failed: "
            f"{retry.status_code}: {(retry.text or '')[:300]}"
        )
    raise RuntimeError(
        f"Graph POST {create_url} → {create_resp.status_code}: "
        f"{(create_resp.text or '')[:300]}"
    )


def _onedrive_ensure_remote_dir(
    sess, headers: dict, remote_dir: str
) -> str:
    """Ensure the OneDrive folder chain for remote_dir; return deepest folder id.

    First segment is created under drive root, later segments nest under the
    previously resolved folder id.
    """
    segments = [seg for seg in str(remote_dir or "").strip("/").split("/") if seg]
    if not segments:
        raise ValueError(f"empty OneDrive remote path: {remote_dir!r}")
    parent_id = "root"
    for seg in segments:
        parent_id = _onedrive_ensure_folder(sess, headers, parent_id, seg)
    return parent_id


def _onedrive_upload_file(
    token: str,
    local_path: Path,
    remote_dir: str,
    on_progress: Optional[callable],
) -> tuple[bool, Optional[str]]:
    """Upload one file to OneDrive via a chunked createUploadSession.

    Ensures the parent folder chain exists, then PUTs 10 MiB chunks
    (a multiple of the required 320 KiB fragment size) through the session
    upload URL. Handles 202 continuation, 416 resume, 404 expiry, and
    429/5xx retries. Returns (success, error_message).
    """
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(_ONEDRIVE_IMPORT_HINT) from exc
    total = local_path.stat().st_size
    headers = {"Authorization": f"Bearer {token}"}
    series_folder_id = _onedrive_ensure_remote_dir(
        requests.Session(), headers, remote_dir
    )

    sess = requests.Session()
    quoted_name = urllib.parse.quote(local_path.name, safe="")
    create_url = (
        f"{_GRAPH_BASE}/me/drive/items/{series_folder_id}:/"
        f"{quoted_name}:/createUploadSession"
    )
    try:
        resp = sess.post(
            create_url,
            headers={**headers, "Content-Type": "application/json"},
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            timeout=30,
        )
    except Exception as exc:
        return False, f"OneDrive createUploadSession failed: {exc}"
    if resp.status_code not in (200, 201):
        return (
            False,
            f"OneDrive createUploadSession → {resp.status_code}: "
            f"{(resp.text or '')[:300]}",
        )
    upload_url = resp.json().get("uploadUrl")
    if not upload_url:
        return False, "OneDrive createUploadSession returned no uploadUrl"

    CHUNK = 10 * 1024 * 1024  # multiple of 320 KiB (327680)
    start = 0
    last_time = time.monotonic()
    last_done = 0
    tries = 0
    try:
        with open(local_path, "rb") as f:
            while start < total:
                end = min(start + CHUNK, total) - 1
                f.seek(start)
                data = f.read(end - start + 1)
                put_headers = {
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(data)),
                }  # NO Authorization header on session PUTs (docs: can 401)
                put_resp = sess.put(upload_url, data=data, headers=put_headers, timeout=300)
                status = put_resp.status_code
                if status == 202:
                    next_ranges = put_resp.json().get("nextExpectedRanges") or []
                    if not next_ranges:
                        break  # nothing to continue from → treat as done
                    try:
                        nxt = int(str(next_ranges[0]).split("-")[0])
                    except (ValueError, IndexError):
                        break
                    if nxt <= start:
                        # Some servers finish with a 202 then return on GET;
                        # a non-advancing range means we are done.
                        start = total
                        break
                    start = nxt
                    now = time.monotonic()
                    dt = now - last_time
                    speed = int((start - last_done) / dt) if dt > 0 else 0
                    last_time, last_done = now, start
                    if on_progress and (
                        start >= total or start <= 0 or dt >= 0.25
                    ):
                        on_progress(start, total, speed)
                    tries = 0
                    continue
                if status in (200, 201):
                    if on_progress:
                        on_progress(total, total, 0)
                    return True, None
                if status == 416:
                    # Range not satisfiable — fetch the server's expected range.
                    get_resp = sess.get(upload_url, timeout=30)
                    if get_resp.status_code in (200, 202):
                        body = get_resp.json()
                        ranges = body.get("nextExpectedRanges") or []
                        if ranges:
                            try:
                                start = int(str(ranges[0]).split("-")[0])
                            except (ValueError, IndexError):
                                return False, f"OneDrive upload 416 with unparseable ranges: {ranges}"
                            continue
                    return False, (
                        f"OneDrive upload 416 and no resumable range: "
                        f"{(get_resp.text or put_resp.text or '')[:300]}"
                    )
                if status == 404:
                    return False, "OneDrive upload session expired — recreate & restart the upload"
                if status in (429, 500, 502, 503, 504) and tries < 3:
                    tries += 1
                    retry_after = put_resp.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 1.0 * tries
                    except ValueError:
                        delay = 1.0 * tries
                    time.sleep(min(delay, 10))
                    continue
                return False, (
                    f"OneDrive chunk PUT → {status}: {(put_resp.text or '')[:300]}"
                )
        # Loop ended with start >= total (or a server-side 202 finish).
        if on_progress:
            on_progress(total, total, 0)
        return True, None
    except Exception as exc:
        return False, str(exc)


# Registry snapshot taken once at startup (used by the banner + fallback lookups
# in resolve_upload_method); health() rebuilds it live per request. Built here,
# after the drive helpers above, so the snapshot includes the drive method.
_UPLOAD_METHODS = _build_upload_methods()


def find_mokuro_output(vol_dir: Path) -> Optional[Path]:
    """mokuro writes <title>.mokuro as a sibling of the volume directory."""
    sibling = vol_dir.parent / f"{vol_dir.name}.mokuro"
    if sibling.exists():
        return sibling
    inside = list(vol_dir.glob("*.mokuro"))
    if inside:
        return inside[0]
    parent_matches = [p for p in vol_dir.parent.glob("*.mokuro") if p.stem == vol_dir.name]
    return parent_matches[0] if parent_matches else None


def cleanup_volume_artifacts(vol_dir: Path) -> None:
    sibling_mokuro = vol_dir.parent / f"{vol_dir.name}.mokuro"
    sibling_html = vol_dir.parent / f"{vol_dir.name}.html"
    ocr_cache = vol_dir.parent / "_ocr" / vol_dir.name
    if vol_dir.exists():
        shutil.rmtree(vol_dir, ignore_errors=True)
    sibling_mokuro.unlink(missing_ok=True)
    sibling_html.unlink(missing_ok=True)
    if ocr_cache.exists():
        shutil.rmtree(ocr_cache, ignore_errors=True)


def ndjson(stage: str, message: str, **extra) -> str:
    return json.dumps({"stage": stage, "message": message, **extra}, ensure_ascii=False) + "\n"


def _upload_progress_event(
    remote_name: str,
    bytes_done: int,
    total_bytes: int,
    speed_bps: int,
    remote_path: str,
    method: str,
) -> str:
    """Standardized per-file upload progress event (shared for every remote dir).

    Schema (stable across all upload methods):
      {"stage":"upload_progress","message":"<file>: 42.5%",
       "upload":{"file","bytes","total_bytes","current_bytes","percent","speed_bps","speed_human"},
       "mega_path":"<remote dir>","method":"mega"}
    "mega_path" keeps its historical name for backward compatibility even
    though it now holds `remote_path` for any method.
    """
    percent = 100.0 if total_bytes <= 0 else round(bytes_done * 100.0 / total_bytes, 2)
    speed_human = f"{speed_bps / 1024 / 1024:.2f} MiB/s" if speed_bps >= 1024**2 else f"{speed_bps / 1024:.1f} KiB/s" if speed_bps else "—"
    upload = {
        "file": remote_name,
        "bytes": bytes_done,
        "total_bytes": total_bytes,
        "current_bytes": bytes_done,  # explicit alias: bytes uploaded so far
        "percent": percent,
        "speed_bps": speed_bps,
        "speed_human": speed_human,
        "method": method,
    }
    return ndjson(
        "upload_progress",
        f"{remote_name}: {percent:.1f}%",
        upload=upload,
        current_bytes=bytes_done,  # top-level mirror for easy consumption
        total_bytes=total_bytes,
        percent=percent,
        speed_bps=speed_bps,
        remote_path=remote_path,
        mega_path=remote_path,  # legacy name kept for backward compatibility
        method=method,
    )


def session_snapshot(session: Session) -> dict:
    with session.lock:
        received = len(session.pages_received)
        done = len(session.pages_ocr_done)
        failed = len(session.pages_ocr_failed)
        pending = received - done - failed
        return {
            "session_id": session.session_id,
            "title": session.title,
            "safe_title": session.safe_title,
            "pages_received": received,
            "pages_ocr_done": done,
            "pages_ocr_failed": failed,
            "pages_ocr_pending": max(0, pending),
            "message": session.message,
            "finalized": session.finalized,
        }


def _resolve_ingest_path(raw_path: str) -> Path:
    """Resolve a same-machine path and ensure it sits under an allowed root.

    Local clients (headless scrapers, ocr_folder.py) hand us filesystem paths
    instead of uploading bytes. We only accept paths under the user's home
    directory or system temp locations. The caller validates the type
    (file vs directory) afterwards.
    """
    resolved = Path(raw_path).expanduser().resolve()
    if not any(
        _is_relative_to(resolved, root) for root in _LOCAL_INGEST_ROOTS
    ):
        raise HTTPException(
            status_code=403,
            detail=f"Local ingest path not under allowed roots: {resolved}",
        )
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_local_output_dir(raw_path: str) -> Path:
    """Resolve a client-supplied local_dir and ensure it is allowed.

    Mirrors the ingest guard (_resolve_ingest_path): the path must sit under
    the user's home directory or system temp. The directory is created
    (parents OK) when missing. Raises HTTPException(400) on disallowed paths.
    """
    resolved = Path(raw_path).expanduser().resolve()
    if not any(_is_relative_to(resolved, root) for root in _LOCAL_INGEST_ROOTS):
        raise HTTPException(
            status_code=400,
            detail="local_dir must be under your home directory or system temp",
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _queue_page_ocr(session: Session, safe_name: str) -> dict:
    """Enqueue a page for chunked OCR flush (non-blocking). Skips pages already OCR'd."""
    with session.lock:
        session.pages_received.add(safe_name)
        if safe_name in session.pages_ocr_done:
            received = len(session.pages_received)
            done = len(session.pages_ocr_done)
            session.message = f"Captured {received} · OCR {done}/{received} (cached)"
            snap = session_snapshot(session)
            snap["cached"] = True
            return snap
        received = len(session.pages_received)
        done = len(session.pages_ocr_done)
        session.message = f"Captured {received} · OCR {done}/{received}"

    _ensure_ocr_worker()
    with _ocr_cv:
        # Avoid duplicate queue entries for the same page
        if (session.session_id, safe_name) not in _ocr_queue and (
            session.session_id,
            safe_name,
        ) not in _ocr_processing:
            _ocr_queue.append((session.session_id, safe_name))
        _ocr_cv.notify()

    _persist_session(session)
    return session_snapshot(session)


def _pending_count_for(session_id: str) -> int:
    return sum(1 for sid, _ in _ocr_queue if sid == session_id) + sum(
        1 for sid, _ in _ocr_processing if sid == session_id
    )


def _take_ocr_batch() -> list[tuple[str, str]]:
    """Pick next flush batch. Caller must hold _ocr_cv."""
    if not _ocr_queue:
        return []

    force_cap = max(_OCR_CHUNK_SIZE, 32)

    # Finalize drain: prefer pages for sessions that are waiting on finalize
    if _ocr_force_sessions:
        batch: list[tuple[str, str]] = []
        rest: deque[tuple[str, str]] = deque()
        while _ocr_queue:
            item = _ocr_queue.popleft()
            if item[0] in _ocr_force_sessions and len(batch) < force_cap:
                batch.append(item)
            else:
                rest.append(item)
        _ocr_queue.extend(rest)
        if batch:
            return batch

    if len(_ocr_queue) >= _OCR_CHUNK_SIZE:
        return [_ocr_queue.popleft() for _ in range(_OCR_CHUNK_SIZE)]

    return []


def _take_idle_batch() -> list[tuple[str, str]]:
    """Flush whatever is waiting after idle timeout. Caller must hold _ocr_cv."""
    if not _ocr_queue:
        return []
    n = min(len(_ocr_queue), _OCR_CHUNK_SIZE)
    return [_ocr_queue.popleft() for _ in range(n)]


def _process_ocr_batch(items: list[tuple[str, str]]) -> None:
    """OCR every page in the batch and write its per-page JSON.

    Two OCR strategies are supported and picked at runtime based on what the
    installed mokuro provides:

    - stock:  `mpocr(img_path)` → complete page result (detect + recognize),
              written to mokuro's per-page cache JSON
              (<volume_parent>/_ocr/<volume>/<name>.json).
    - fork:   if the mokuro instance exposes `detect_and_extract` /
              `recognize_text` (MOKURO_REPO override), detection runs on all
              pages first and recognition is batched in one call — faster on
              GPU builds. Results are saved to the same JSON layout.

    Both paths produce cache JSON that `generate_mokuro_file()` later reads.
    """
    if not items:
        return

    gen = _get_generator()
    gen.init_models()
    mpocr = gen.mpocr
    use_fork_api = _fork_supported()

    print(
        f"[mokuro-bridge] OCR flush {len(items)} page(s)"
        f" (engine: {'fork batch' if use_fork_api else 'stock'}): "
        + ", ".join(f"{sid[:6]}/{fn}" for sid, fn in items[:6])
        + ("…" if len(items) > 6 else "")
    )

    if use_fork_api:
        _ocr_batch_fork(items, gen, mpocr)
    else:
        _ocr_batch_stock(items, gen, mpocr)


def _ocr_batch_stock(items: list[tuple[str, str]], gen, mpocr) -> None:
    """Stock mokuro: one full page OCR call per page (detect + recognize)."""
    Volume = _mokuro_submodule("volume").Volume

    for session_id, filename in items:
        session = _session_or_none(session_id)
        if not session:
            continue
        img_path = session.vol_dir / filename
        try:
            volume = Volume(session.vol_dir)
            result = mpocr(str(img_path))
            _save_page_result(session, volume, filename, result)
        except Exception as e:
            _mark_page_failed(session, filename, e)


def _ocr_batch_fork(items: list[tuple[str, str]], gen, mpocr) -> None:
    """Fork mokuro: detect all pages, then one batched recognize_text call."""
    utils_mod = _mokuro_submodule("utils")
    imread = utils_mod.imread
    Volume = _mokuro_submodule("volume").Volume

    ocr_batch_size = getattr(gen, "ocr_batch_size", 48) or 48
    page_results: dict[tuple[str, str], dict] = {}
    crop_meta_map: dict[tuple[str, str, int], tuple[int, int]] = {}
    all_crops: list = []
    crop_map: list[tuple[str, str, int]] = []

    for session_id, filename in items:
        session = _session_or_none(session_id)
        if not session:
            continue
        img_path = session.vol_dir / filename
        try:
            img = imread(str(img_path))
            if img is None:
                raise RuntimeError(f"Could not read {img_path}")
            result, crops, metadata = mpocr.detect_and_extract(img)
            page_results[(session_id, filename)] = result
            for j in range(len(crops)):
                all_crops.append(crops[j])
                crop_map.append((session_id, filename, j))
                crop_meta_map[(session_id, filename, j)] = metadata[j]
        except Exception as e:
            session = _session_or_none(session_id)
            if session:
                _mark_page_failed(session, filename, e)

    if not page_results:
        return

    try:
        texts = mpocr.recognize_text(
            all_crops,
            batch_size=ocr_batch_size,
            num_beams=1,
        )
        for global_idx, text in enumerate(texts):
            sid, fname, local_idx = crop_map[global_idx]
            result = page_results.get((sid, fname))
            if result is None:
                continue
            blk_idx, line_idx = crop_meta_map[(sid, fname, local_idx)]
            result["blocks"][blk_idx]["lines"][line_idx] += text
    except Exception as e:
        for (sid, fname) in page_results:
            session = _session_or_none(sid)
            if session:
                _mark_page_failed(session, fname, e)
        return

    for (sid, fname), result in page_results.items():
        session = _session_or_none(sid)
        if not session:
            continue
        volume = Volume(session.vol_dir)
        _save_page_result(session, volume, fname, result)


def _save_page_result(session: Session, volume, filename: str, result: dict) -> None:
    """Write one page's OCR JSON into mokuro's cache layout and update state."""
    dump_json = _mokuro_submodule("utils").dump_json

    json_path = ocr_json_path(volume, filename)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(result, json_path)
    with session.lock:
        session.pages_ocr_done.add(filename)
        done = len(session.pages_ocr_done)
        total = len(session.pages_received)
        session.message = f"OCR {done}/{total} pages"
    _persist_session(session)


def _mark_page_failed(session: Session, filename: str, error: Exception) -> None:
    with session.lock:
        session.pages_ocr_failed.add(filename)
        session.message = f"OCR failed on {filename}: {error}"
    print(f"[mokuro-bridge] OCR error on {filename}: {error}")
    _persist_session(session)


def _ocr_worker_loop() -> None:
    try:
        _get_generator()
        print(
            f"[mokuro-bridge] OCR flusher ready (chunk={_OCR_CHUNK_SIZE}, "
            f"idle={_OCR_IDLE_FLUSH_S}s)"
        )
    except Exception as e:
        print(f"[mokuro-bridge] OCR flusher model init error: {e}")

    while True:
        batch: list[tuple[str, str]] = []
        with _ocr_cv:
            while True:
                batch = _take_ocr_batch()
                if batch:
                    break
                if _ocr_queue:
                    # Wait for more pages to fill a chunk, or idle-flush
                    _ocr_cv.wait(timeout=_OCR_IDLE_FLUSH_S)
                    batch = _take_ocr_batch()
                    if batch:
                        break
                    if _ocr_queue:
                        batch = _take_idle_batch()
                        if batch:
                            break
                    continue
                _ocr_cv.wait()

            for item in batch:
                _ocr_processing.add(item)

        try:
            _process_ocr_batch(batch)
        finally:
            with _ocr_cv:
                for item in batch:
                    _ocr_processing.discard(item)
                _ocr_cv.notify_all()


def _ensure_ocr_worker() -> None:
    global _ocr_worker_started
    with _ocr_lock:
        if _ocr_worker_started:
            return
        t = threading.Thread(target=_ocr_worker_loop, name="mokuro-ocr-flush", daemon=True)
        t.start()
        _ocr_worker_started = True


def wait_for_session_ocr(session: Session) -> None:
    """Block until every received page for this session is OCR'd (or failed)."""
    sid = session.session_id
    _ensure_ocr_worker()
    with _ocr_cv:
        _ocr_force_sessions.add(sid)
        _ocr_cv.notify()
        while True:
            pending = _pending_count_for(sid)
            with session.lock:
                done = len(session.pages_ocr_done) + len(session.pages_ocr_failed)
                total = len(session.pages_received)
            if pending == 0 and done >= total:
                _ocr_force_sessions.discard(sid)
                return
            _ocr_cv.wait(timeout=0.5)


# ── Session endpoints ──────────────────────────────────────────────────


@app.post("/session/start")
async def session_start(
    title: str = Form("manga"),
    reuse_existing: str = Form("false"),
):
    """Create a new pipelined capture+OCR session."""
    safe_title = sanitize_filename(title) or f"manga_{uuid.uuid4().hex[:8]}"
    do_reuse = _truthy(reuse_existing)

    if do_reuse:
        existing = _find_session_by_safe_title(safe_title)
        if existing:
            existing.vol_dir.mkdir(parents=True, exist_ok=True)
            _sync_ocr_cache_state(existing)
            _persist_session(existing)
            _ensure_ocr_worker()
            snap = session_snapshot(existing)
            snap["reused"] = True
            snap["vol_dir"] = str(existing.vol_dir)
            return JSONResponse(snap)

        # Reuse the on-disk volume even with no live session (keeps OCR cache).
        vol_dir = WORK_DIR / safe_title
        vol_dir.mkdir(parents=True, exist_ok=True)
    else:
        vol_dir = WORK_DIR / safe_title
        if vol_dir.exists():
            vol_dir = WORK_DIR / f"{safe_title}_{uuid.uuid4().hex[:6]}"
            safe_title = vol_dir.name
        vol_dir.mkdir(parents=True, exist_ok=True)

    session_id = uuid.uuid4().hex[:12]
    session = Session(
        session_id=session_id,
        title=title,
        safe_title=safe_title,
        vol_dir=vol_dir,
        message="Ready — waiting for pages",
    )
    if do_reuse:
        _sync_ocr_cache_state(session)
    with _sessions_lock:
        _sessions[session_id] = session
    _persist_session(session)
    _ensure_ocr_worker()

    return JSONResponse(
        {
            "session_id": session_id,
            "title": title,
            "safe_title": safe_title,
            "vol_dir": str(vol_dir),
            "message": session.message,
            "ocr_chunk_size": _OCR_CHUNK_SIZE,
            "reused": do_reuse and any(vol_dir.iterdir()),
            "pages_received": len(session.pages_received),
            "pages_ocr_done": len(session.pages_ocr_done),
        }
    )


@app.post("/session/resume")
async def session_resume(
    title: str = Form(...),
    source_dir: str = Form(""),
):
    """
    Resume OCR+MEGA for a volume that was partially scraped/OCR'd.

    - Reuses ~/mokuro-input/<title>/ when present
    - Optionally syncs newer/missing images from source_dir (e.g. manga_archives)
    - Skips pages that already have OCR JSON under _ocr/<title>/
    - Queues only the missing pages
    """
    safe_title = sanitize_filename(title) or f"manga_{uuid.uuid4().hex[:8]}"
    vol_dir = WORK_DIR / safe_title
    vol_dir.mkdir(parents=True, exist_ok=True)

    synced = 0
    if source_dir.strip():
        src = _resolve_ingest_path(source_dir.strip())
        if not src.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {src}")
        for img in sorted(src.iterdir()):
            if not img.is_file() or img.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            dest = vol_dir / img.name
            if not dest.exists():
                # Never overwrite existing pages (partial re-scrapes must not clobber good data)
                shutil.copy2(img, dest)
                synced += 1

    existing = _find_session_by_safe_title(safe_title)
    if existing:
        session = existing
        session.finalized = False
    else:
        session_id = uuid.uuid4().hex[:12]
        session = Session(
            session_id=session_id,
            title=title,
            safe_title=safe_title,
            vol_dir=vol_dir,
            message="Resuming…",
        )
        with _sessions_lock:
            _sessions[session_id] = session

    _sync_ocr_cache_state(session)

    queued = 0
    cached = 0
    images = sorted(
        p.name
        for p in vol_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )
    for name in images:
        with session.lock:
            already = name in session.pages_ocr_done
        if already:
            cached += 1
            with session.lock:
                session.pages_received.add(name)
            continue
        _queue_page_ocr(session, name)
        queued += 1

    _persist_session(session)
    _ensure_ocr_worker()
    snap = session_snapshot(session)
    snap.update(
        {
            "vol_dir": str(vol_dir),
            "synced_from_source": synced,
            "queued_for_ocr": queued,
            "ocr_cached": cached,
            "resumed": True,
        }
    )
    print(
        f"[mokuro-bridge] resume {safe_title}: synced={synced} cached={cached} queued={queued}"
    )
    return JSONResponse(snap)


@app.post("/session/{session_id}/page")
async def session_page(
    session_id: str,
    page: UploadFile = File(...),
    filename: str = Form(...),
    page_num: int = Form(0),
):
    """Accept one captured page and queue OCR immediately (non-blocking)."""
    session = _get_session(session_id)
    if session.finalized:
        raise HTTPException(status_code=400, detail="Session already finalized")

    # Sanitize filename to page_001.webp style if needed
    safe_name = Path(filename).name
    if not safe_name or safe_name.startswith("."):
        safe_name = f"page_{int(page_num):03d}.webp"

    dest = session.vol_dir / safe_name
    data = await page.read()
    dest.write_bytes(data)

    snap = _queue_page_ocr(session, safe_name)
    snap["filename"] = safe_name
    snap["page_num"] = page_num
    return JSONResponse(snap)


@app.post("/session/{session_id}/page-local")
async def session_page_local(
    session_id: str,
    path: str = Form(...),
    filename: str = Form(...),
    page_num: int = Form(0),
):
    """Same-machine ingest: copy a local image into the session and queue OCR."""
    session = _get_session(session_id)
    if session.finalized:
        raise HTTPException(status_code=400, detail="Session already finalized")

    src = _resolve_ingest_path(path)
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {src}")
    if src.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {src.suffix}")
    safe_name = Path(filename).name or src.name
    if not safe_name or safe_name.startswith("."):
        safe_name = f"page_{int(page_num):03d}{src.suffix.lower()}"

    # Volume dir can disappear after MEGA cleanup while the session is still live.
    session.vol_dir.mkdir(parents=True, exist_ok=True)
    dest = session.vol_dir / safe_name
    # Never clobber an existing page (re-scrapes / resume must keep good data).
    copied = False
    if not dest.exists():
        await asyncio.to_thread(shutil.copy2, src, dest)
        copied = True
        # File was missing — force re-OCR even if a stale session marked it done.
        with session.lock:
            session.pages_ocr_done.discard(safe_name)
            session.pages_ocr_failed.discard(safe_name)

    snap = _queue_page_ocr(session, safe_name)
    snap["filename"] = safe_name
    snap["page_num"] = page_num
    snap["source"] = str(src)
    snap["copied"] = copied
    return JSONResponse(snap)


@app.get("/sessions")
async def list_sessions():
    """Active pipelined sessions (useful when several capture clients run at once)."""
    with _sessions_lock:
        snaps = [session_snapshot(s) for s in _sessions.values()]
    return JSONResponse({"sessions": snaps, "count": len(snaps)})


@app.get("/session/{session_id}/status")
async def session_status(session_id: str):
    return JSONResponse(session_snapshot(_get_session(session_id)))


@app.post("/session/{session_id}/finalize")
async def session_finalize(
    session_id: str,
    delete_after_upload: str = Form("true"),
    upload_to_mega: str = Form(""),
    upload_method: str = Form(""),
    local_dir: str = Form(""),
):
    """
    Wait for OCR queue, assemble .mokuro, pack CBZ + cover, then either keep
    the artifacts locally (default) or upload them to a remote method (MEGA).
    NDJSON stream.

    upload_method: "local" → OUTPUT_DIR; "mega" → MEGA; unset → falls back to
    upload_to_mega, then to the MOKURO_BRIDGE_UPLOAD_DEFAULT env (default
    "false", i.e. local).
    upload_to_mega: legacy alias — "true" → MEGA; "false" → local; unset →
    env default. New clients should prefer upload_method.
    local_dir: only used when the resolved method is "local" — a custom
    output directory (must be under your home dir or system temp; created if
    missing). Ignored for remote methods.
    """
    session = _get_session(session_id)
    do_delete = _truthy(delete_after_upload)
    raw_target = str(upload_method).strip() or str(upload_to_mega).strip() or None
    method = resolve_upload_method(raw_target)
    # Sticky default: an EXPLICIT method choice becomes the new default for
    # later requests (persisted). Plain finalizes that rely on the default
    # leave it untouched.
    if raw_target is not None:
        _remember_upload_method(method)
    # Custom local output dir (only meaningful for the "local" method; the
    # validation below raises a proper HTTP 400 before the stream starts).
    local_output_base = None
    if method == "local" and local_dir.strip():
        local_output_base = _resolve_local_output_dir(local_dir)

    async def generate():
        try:
            snap = session_snapshot(session)
            yield ndjson(
                "wait_ocr",
                f"Waiting for OCR queue ({snap['pages_ocr_pending']} pending, "
                f"flush every {_OCR_CHUNK_SIZE} or {_OCR_IDLE_FLUSH_S}s idle)…",
                **{k: snap[k] for k in ("pages_received", "pages_ocr_done", "pages_ocr_pending")},
                ocr_chunk_size=_OCR_CHUNK_SIZE,
            )
            await asyncio.sleep(0)

            # Force-flush remaining pages, poll until done
            wait_task = asyncio.create_task(asyncio.to_thread(wait_for_session_ocr, session))
            while not wait_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(wait_task), timeout=0.75)
                except asyncio.TimeoutError:
                    snap = session_snapshot(session)
                    yield ndjson(
                        "wait_ocr",
                        f"OCR {snap['pages_ocr_done']}/{snap['pages_received']}"
                        + (f" ({snap['pages_ocr_pending']} pending)" if snap["pages_ocr_pending"] else ""),
                        pages_ocr_done=snap["pages_ocr_done"],
                        pages_received=snap["pages_received"],
                        pages_ocr_pending=snap["pages_ocr_pending"],
                    )
                    await asyncio.sleep(0)
            await wait_task

            snap = session_snapshot(session)
            yield ndjson(
                "assemble",
                f"Assembling .mokuro from {snap['pages_ocr_done']} OCR pages…",
            )
            await asyncio.sleep(0)

            volume_mod = _mokuro_submodule("volume")
            generator_mod = _mokuro_submodule("mokuro_generator")
            MokuroGenerator = generator_mod.MokuroGenerator
            Title = volume_mod.Title
            Volume = volume_mod.Volume

            def _assemble():
                with _assemble_lock:
                    volume = Volume(session.vol_dir)
                    volume.title = Title(session.vol_dir.parent)
                    volume.title.set_uuid()
                    if not volume.uuid:
                        volume.uuid = str(uuid.uuid4())
                    MokuroGenerator.generate_mokuro_file(volume, True)

            await asyncio.to_thread(_assemble)

            mokuro_file = find_mokuro_output(session.vol_dir)
            if not mokuro_file:
                yield ndjson(
                    "error",
                    f"No .mokuro after assemble (expected {session.vol_dir.parent / (session.vol_dir.name + '.mokuro')})",
                )
                return

            yield ndjson("pack", "Packaging CBZ + cover…")
            await asyncio.sleep(0)

            image_files = sorted(
                p
                for p in session.vol_dir.iterdir()
                if p.suffix.lower() in IMAGE_EXTENSIONS and p.is_file()
            )
            if not image_files:
                yield ndjson("error", "No images in volume directory")
                return

            if method != "local" and len(image_files) < MIN_PAGES_FOR_MEGA:
                yield ndjson(
                    "error",
                    f"Refusing upload: only {len(image_files)} page(s) "
                    f"(minimum {MIN_PAGES_FOR_MEGA}). "
                    "Likely a failed scrape / free-viewer error — local files kept.",
                    status="too_few_pages",
                    pages=len(image_files),
                    min_pages=MIN_PAGES_FOR_MEGA,
                    title=session.safe_title,
                    vol_dir=str(session.vol_dir),
                )
                return

            # Where the final trio lands:
            #   MEGA  → temp staging next to the volume (uploaded, then removed)
            #   local → <OUTPUT_DIR>/<series>/            (kept for the user)
            series_dir_name = series_title_from_volume(session.safe_title)
            file_base = session.safe_title
            staging = (
                session.vol_dir / "_mega_upload"
                if method == "mega"
                else (local_output_base or OUTPUT_DIR) / series_dir_name
            )
            staging.mkdir(parents=True, exist_ok=True)
            # Files keep the full volume title; folder is the shared series name.
            titled_mokuro = staging / f"{file_base}.mokuro"
            titled_cbz = staging / f"{file_base}.cbz"
            titled_cover = staging / f"{file_base}.webp"
            shutil.copy2(mokuro_file, titled_mokuro)

            with zipfile.ZipFile(titled_cbz, "w", zipfile.ZIP_DEFLATED) as zf:
                for img in image_files:
                    zf.write(img, img.name)

            webp_pages = [p for p in image_files if p.suffix.lower() == ".webp"]
            shutil.copy2(webp_pages[0] if webp_pages else image_files[0], titled_cover)

            if method == "local":
                yield ndjson(
                    "pack",
                    f"Saved local pack → {staging}",
                    output_dir=str(staging),
                    series=series_dir_name,
                )
                await asyncio.sleep(0)

            series = series_title_from_volume(session.safe_title)
            if method == "mega":
                remote_dir = f"{MEGA_LIBRARY_ROOT}/{series}"
            elif method == "drive":
                remote_dir = f"{DRIVE_ROOT_NAME}/{series}"
            elif method == "onedrive":
                remote_dir = f"{ONEDRIVE_ROOT_NAME}/{series}"
            elif method == "webdav":
                remote_dir = f"{WEBDAV_ROOT_NAME}/{series}"
            else:
                remote_dir = ""  # local — not used
            method_label = {
                "mega": "MEGA",
                "drive": "Google Drive",
                "onedrive": "OneDrive",
                "webdav": "WebDAV",
            }.get(method, method)
            upload_results = []
            all_success = True

            if method != "local":
                yield ndjson(
                    "upload",
                    f"Uploading to {method_label}… ({series_dir_name}/)",
                    series=series_dir_name,
                    remote_path=remote_dir,
                    mega_path=remote_dir if method == "mega" else None,
                    method=method,
                )
                await asyncio.sleep(0)

                def _upload_batch():
                    progress_events = []
                    results = []
                    items = [
                        (titled_cbz, f"{file_base}.cbz"),
                        (titled_mokuro, f"{file_base}.mokuro"),
                        (titled_cover, f"{file_base}.webp"),
                    ]
                    for local_path, remote_name in items:
                        start = time.monotonic()
                        last_progress = {"bytes": 0, "speed": 0, "emitted": 0.0}

                        def _on_progress(
                            bytes_done, total_bytes, speed_bps, _name=remote_name
                        ):
                            # Throttle upstream NDJSON events to ~4/sec so
                            # megatools' 1/sec lines stay live without flooding.
                            now = time.monotonic()
                            if (
                                bytes_done >= total_bytes
                                or bytes_done <= 0
                                or now - last_progress["emitted"] >= 0.25
                            ):
                                last_progress["bytes"] = bytes_done
                                last_progress["speed"] = speed_bps
                                last_progress["emitted"] = now
                                progress_events.append(
                                    _upload_progress_event(
                                        _name, bytes_done, total_bytes, speed_bps,
                                        remote_dir, method,
                                    )
                                )

                        ok, err = upload_file(method, local_path, remote_dir, _on_progress)
                        duration_s = round(time.monotonic() - start, 2)
                        results.append(
                            {
                                "file": remote_name,
                                "size": local_path.stat().st_size,
                                "success": ok,
                                "stderr": err if not ok else None,
                                "duration_s": duration_s,
                            }
                        )
                    return results, progress_events

                upload_results, progress_events = await asyncio.to_thread(_upload_batch)
                for ev in progress_events:
                    yield ev
                    await asyncio.sleep(0)
                for r in upload_results:
                    if r["success"]:
                        yield ndjson(
                            "upload",
                            f"Uploaded {r['file']}",
                            file=r["file"],
                            method=method,
                        )
                    else:
                        yield ndjson(
                            "upload",
                            f"Upload failed for {r['file']}: {r.get('stderr') or 'unknown'}",
                            file=r["file"],
                            method=method,
                        )
                    await asyncio.sleep(0)

                all_success = all(r["success"] for r in upload_results)

            session.finalized = True

            if do_delete and all_success:
                yield ndjson("cleanup", "Cleaning up working files…")
                await asyncio.sleep(0)
                cleanup_volume_artifacts(session.vol_dir)

            with _sessions_lock:
                _sessions.pop(session_id, None)
            _delete_persisted_session(session_id)

            if method != "local" and not all_success:
                failed = [r for r in upload_results if not r["success"]]
                yield ndjson(
                    "error",
                    f"{method_label} upload failed: "
                    + "; ".join(f"{r['file']}: {r.get('stderr') or 'unknown'}" for r in failed),
                    status="partial_upload",
                    title=session.safe_title,
                    pages=len(image_files),
                    remote_path=remote_dir,
                    mega_path=remote_dir if method == "mega" else None,
                    uploads=upload_results,
                    method=method,
                )
                return

            done_msg = (
                f"Done! {len(image_files)} pages → {method_label} {remote_dir}/"
                f"{file_base}.{{cbz,mokuro,webp}}"
                if method != "local"
                else f"Done! {len(image_files)} pages OCR'd → {staging}"
            )
            # Standardized per-file upload summary for the "done" event (only
            # in remote mode); each entry mirrors the "upload_progress" schema.
            uploads_summary = []
            if method != "local":
                for r in upload_results:
                    total = r.get("size", 0)
                    dur = r.get("duration_s") or 0
                    uploads_summary.append(
                        {
                            "file": r["file"],
                            "bytes": total,
                            "total_bytes": total,
                            "current_bytes": total if r["success"] else 0,
                            "percent": 100.0 if r["success"] else 0.0,
                            "speed_bps": int(total / dur) if dur and r["success"] else 0,
                            "duration_s": dur,
                            "success": r["success"],
                        }
                    )
            yield ndjson(
                "done",
                done_msg,
                status="success",
                title=session.safe_title,
                series=series_dir_name,
                pages=len(image_files),
                pages_ocr_done=snap["pages_ocr_done"],
                remote_path=remote_dir if method != "local" else None,
                mega_path=remote_dir if method == "mega" else None,
                output_dir=str(staging) if method == "local" else None,
                staging=str(staging),
                uploads=uploads_summary if method != "local" else upload_results,
                reader_url="https://reader.mokuro.app/" if method != "local" else None,
                method=method,
            )
        except Exception as e:
            yield ndjson("error", f"Finalize failed: {e}")

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Health / upload-methods ────────────────────────────────────────────


def _method_current_folder(method_id: str) -> str:
    """Human-readable 'current folder' for an upload method (for clients)."""
    if method_id == "local":
        return str(OUTPUT_DIR)
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


@app.get("/upload-methods")
async def upload_methods():
    methods = []
    for m in _build_upload_methods().values():
        entry = {
            "id": m.id,
            "name": m.name,
            "configured": m.configured,
            "default": m.default,
            **m.extra,
        }
        # current folder per method
        entry["current_folder"] = _method_current_folder(m.id)
        methods.append(entry)
    return JSONResponse(
        {
            "upload_method_default": _default_upload_method(),
            "upload_method_selected": None,
            "methods": methods,
        }
    )


@app.get("/health")
async def health():
    result = {
        "app": APP_NAME,
        "version": __version__,
        "status": "ok",
        "mokuro_installed": False,
        "mokuro_custom_fork": _MOKURO_REPO is not None,
        "mokuro_repo": str(_MOKURO_REPO) if _MOKURO_REPO is not None else None,
        "mokuro_fork_api": False,
        "megatools_installed": bool(shutil.which("megatools")),
        "mega_configured": False,
        "mega_creds_source": None,
        "mega_library_root": MEGA_LIBRARY_ROOT,
        "upload_default": _MEGA_UPLOAD_DEFAULT,
        "upload_methods": [
            {"id": m.id, "name": m.name, "configured": m.configured,
             "default": m.default, **m.extra}
            for m in _build_upload_methods().values()
        ],
        "upload_method_default": _default_upload_method(),
        "upload_method_selected": None,
        "work_dir": str(WORK_DIR),
        "output_dir": str(OUTPUT_DIR),
        "cors_origins": CORS_ORIGINS,
        "active_sessions": len(_sessions),
        "ocr_chunk_size": _OCR_CHUNK_SIZE,
        "ocr_idle_flush_s": _OCR_IDLE_FLUSH_S,
        "ocr_queue_depth": len(_ocr_queue),
    }
    if _mokuro_pkg is not None:
        result["mokuro_installed"] = True
        result["mokuro_version"] = getattr(_mokuro_pkg, "__version__", "?")
        result["mokuro_path"] = getattr(_mokuro_pkg, "__file__", "?")
        result["mokuro_fork_api"] = _fork_supported()

    source = _mega_creds_source()
    if source is not None:
        result["mega_configured"] = True
        result["mega_creds_source"] = source

    return JSONResponse(result)


# ── Entrypoint ─────────────────────────────────────────────────────────


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog=APP_NAME, description=__doc__.splitlines()[0])
    parser.add_argument(
        "--setup-upload",
        metavar="METHOD",
        default=None,
        help="Interactively configure/authenticate an upload method "
        "(available: mega, drive, onedrive, webdav). e.g. --setup-upload drive",
    )
    parser.add_argument(
        "--setup-mega",
        action="store_true",
        help="[alias for --setup-upload mega] Interactively store MEGA "
        "credentials in the OS keychain / credential store (macOS Keychain, "
        "Windows Credential Manager, Linux Secret Service) or, failing that, "
        "a 0600 credentials file.",
    )
    parser.add_argument("--host", default=os.environ.get("MOKURO_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MOKURO_BRIDGE_PORT", "62642")),
    )
    args = parser.parse_args()

    setup_method = args.setup_upload or ("mega" if args.setup_mega else None)
    if setup_method:
        if setup_method == "mega":
            _run_setup_mega()
        elif setup_method == "drive":
            _run_setup_drive()
        elif setup_method == "onedrive":
            _run_setup_onedrive()
        elif setup_method == "webdav":
            _run_setup_webdav()
        elif setup_method in _UPLOAD_METHODS:
            print(f"error: upload method '{setup_method}' has no setup wizard yet")
            raise SystemExit(2)
        else:
            print(
                f"error: unknown upload method '{setup_method}' "
                f"(available: {', '.join(_UPLOAD_METHODS)})"
            )
            raise SystemExit(2)
        return

    import uvicorn

    mega_state = "yes" if _UPLOAD_METHODS["mega"].configured else "no"
    print("=" * 60)
    print(f"  {APP_NAME} v{__version__} on http://{args.host}:{args.port}")
    print(f"  work dir:   {WORK_DIR}")
    print(f"  output dir: {OUTPUT_DIR} (local mode)")
    print(f"  upload methods: {_default_upload_method()} (default), mega (configured: {mega_state})")
    print(f"  MEGA root:  {MEGA_LIBRARY_ROOT} (upload default: {_MEGA_UPLOAD_DEFAULT})")
    print(f"  drive root: {DRIVE_ROOT_NAME} (configured: {'yes' if _UPLOAD_METHODS['drive'].configured else 'no'})")
    onedrive_state = "yes" if _UPLOAD_METHODS["onedrive"].configured else "no"
    print(f"  onedrive root: {ONEDRIVE_ROOT_NAME} (configured: {onedrive_state})")
    webdav_state = "yes" if _UPLOAD_METHODS["webdav"].configured else "no"
    webdav_detail = (
        f" (base URL: {WEBDAV_BASE_URL})" if WEBDAV_BASE_URL else " (no base URL)"
    )
    print(f"  webdav root: {WEBDAV_ROOT_NAME} (configured: {webdav_state}){webdav_detail}")
    if _MOKURO_REPO is not None:
        print(f"  custom mokuro repo: {_MOKURO_REPO}")
    if _mokuro_pkg is not None:
        print(f"  mokuro: {getattr(_mokuro_pkg, '__file__', '?')} (fork API: {_fork_supported()})")
    else:
        print("  mokuro: NOT INSTALLED — run: pip install mokuro")
    print("=" * 60)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        log_level="info",
        # Reload wipes in-memory sessions mid-scrape. Opt in with UVICORN_RELOAD=1.
        reload=os.environ.get("UVICORN_RELOAD", "0").lower() in ("1", "true", "yes"),
        reload_excludes=["mokuro/*", "**/mokuro/**", "**/__pycache__/**"],
    )


if __name__ == "__main__":
    _main()
