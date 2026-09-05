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

Requires: Python 3.10+, mokuro (pip) — a custom mokuro fork/clone can be
enabled with MOKURO_REPO. Optional: megatools + MEGA credentials for uploads.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
__version__ = "0.2.0"

# ── mokuro backend ────────────────────────────────────────────────────
# OCR engine resolution order:
#   1. MOKURO_REPO env → path to a mokuro checkout (e.g. your optimized
#      fork) whose repo root is inserted on sys.path;
#   2. a `./mokuro/` checkout sitting next to this file (dev convenience);
#   3. the installed `mokuro` package (vanilla `pip install mokuro`).
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

# Chunked OCR flush: detect N pages, then one GPU recognize_text over all crops.
# Tuned for the custom mokuro fork (batch_size ~48 crops). Override via env.
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


def _get_generator():
    """Lazy-init custom MokuroGenerator (models stay warm across pages/sessions)."""
    global _generator
    with _generator_lock:
        if _generator is None:
            from mokuro import MokuroGenerator

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


def _sync_ocr_cache_state(session: Session) -> None:
    """Mark pages that already have OCR JSON as done (supports resume after crash)."""
    from mokuro.volume import Volume

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
            json_path = volume.get_ocr_path(name)
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
            "--no-progress",
            str(local_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )


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
    """Detect text on each page, then one batched recognize_text over all crops."""
    if not items:
        return

    from mokuro.utils import dump_json, imread
    from mokuro.volume import Title, Volume

    gen = _get_generator()
    gen.init_models()
    mpocr = gen.mpocr
    ocr_batch_size = getattr(gen, "ocr_batch_size", 48) or 48

    page_results: dict[tuple[str, str], tuple[dict, list, Session]] = {}
    all_crops: list = []
    crop_map: list[tuple[str, str, int]] = []

    print(
        f"[mokuro-bridge] OCR flush {len(items)} page(s): "
        + ", ".join(f"{sid[:6]}/{fn}" for sid, fn in items[:6])
        + ("…" if len(items) > 6 else "")
    )

    for session_id, filename in items:
        session = _session_or_none(session_id)
        if not session:
            continue
        img_path = session.vol_dir / filename
        try:
            img = imread(str(img_path))
            if img is None:
                raise RuntimeError(f"Could not read {img_path}")
            result, crops, meta = mpocr.detect_and_extract(img)
            page_results[(session_id, filename)] = (result, meta, session)
            for j in range(len(crops)):
                all_crops.append(crops[j])
                crop_map.append((session_id, filename, j))
        except Exception as e:
            with session.lock:
                session.pages_ocr_failed.add(filename)
                session.message = f"OCR failed on {filename}: {e}"
            print(f"[mokuro-bridge] OCR detect error on {filename}: {e}")

    if all_crops:
        try:
            texts = mpocr.recognize_text(
                all_crops,
                batch_size=ocr_batch_size,
                num_beams=1,
            )
            for global_idx, text in enumerate(texts):
                sid, fname, local_idx = crop_map[global_idx]
                result, meta, _session = page_results[(sid, fname)]
                blk_idx, line_idx = meta[local_idx]
                result["blocks"][blk_idx]["lines"][line_idx] += text
        except Exception as e:
            print(f"[mokuro-bridge] OCR batch recognize error: {e}")
            for (sid, fname), (_result, _meta, session) in page_results.items():
                with session.lock:
                    session.pages_ocr_failed.add(fname)
                    session.message = f"OCR batch failed: {e}"
            return

    for (sid, fname), (result, _meta, session) in page_results.items():
        try:
            volume = Volume(session.vol_dir)
            volume.title = Title(session.vol_dir.parent)
            json_path = volume.get_ocr_path(fname)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            dump_json(result, json_path)
            with session.lock:
                session.pages_ocr_done.add(fname)
                done = len(session.pages_ocr_done)
                total = len(session.pages_received)
                session.message = f"OCR {done}/{total} pages"
            _persist_session(session)
        except Exception as e:
            with session.lock:
                session.pages_ocr_failed.add(fname)
                session.message = f"OCR save failed on {fname}: {e}"
            print(f"[mokuro-bridge] OCR save error on {fname}: {e}")
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
):
    """
    Wait for OCR queue, assemble .mokuro, pack CBZ + cover, then either keep
    the artifacts locally (default) or upload them to MEGA. NDJSON stream.

    upload_to_mega: "true" → MEGA upload; "false" → local OUTPUT_DIR; unset →
    falls back to MOKURO_BRIDGE_UPLOAD_DEFAULT env (default "false", i.e. local).
    """
    session = _get_session(session_id)
    do_delete = _truthy(delete_after_upload)
    do_mega = (
        _truthy(upload_to_mega) if str(upload_to_mega).strip() else _MEGA_UPLOAD_DEFAULT
    )

    async def generate():
        megarc_path = None
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

            from mokuro.mokuro_generator import MokuroGenerator
            from mokuro.volume import Title, Volume

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

            if do_mega and len(image_files) < MIN_PAGES_FOR_MEGA:
                yield ndjson(
                    "error",
                    f"Refusing MEGA upload: only {len(image_files)} page(s) "
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
                if do_mega
                else OUTPUT_DIR / series_dir_name
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

            if not do_mega:
                yield ndjson(
                    "pack",
                    f"Saved local pack → {staging}",
                    output_dir=str(staging),
                    series=series_dir_name,
                )
                await asyncio.sleep(0)

            mega_remote_dir = mega_series_dir(session.safe_title)
            upload_results = []
            all_success = True

            if do_mega:
                yield ndjson(
                    "upload",
                    f"Uploading to MEGA… ({series_dir_name}/)",
                    series=series_dir_name,
                    mega_path=mega_remote_dir,
                )
                await asyncio.sleep(0)

                try:
                    email, password = _get_mega_creds()
                except RuntimeError as e:
                    yield ndjson("error", str(e))
                    return

                megarc_path = create_megarc(email, password)

                def _mega_upload_batch():
                    results = []
                    with _mega_lock:
                        for remote_dir in (MEGA_LIBRARY_ROOT, mega_remote_dir):
                            mkdir_result = mega_mkdir(megarc_path, remote_dir)
                            if mkdir_result.returncode != 0:
                                err = (mkdir_result.stderr or mkdir_result.stdout or "").strip()
                                if "exist" not in err.lower():
                                    print(f"[mokuro-bridge] mkdir {remote_dir}: {err}")

                        items = [
                            (titled_cbz, f"{file_base}.cbz"),
                            (titled_mokuro, f"{file_base}.mokuro"),
                            (titled_cover, f"{file_base}.webp"),
                        ]
                        for local_path, remote_name in items:
                            result = mega_put(
                                megarc_path,
                                local_path,
                                f"{mega_remote_dir}/{remote_name}",
                            )
                            results.append(
                                {
                                    "file": remote_name,
                                    "size": local_path.stat().st_size,
                                    "success": result.returncode == 0,
                                    "stderr": result.stderr.strip()
                                    if result.returncode != 0
                                    else None,
                                }
                            )
                    return results

                upload_results = await asyncio.to_thread(_mega_upload_batch)
                for r in upload_results:
                    if r["success"]:
                        yield ndjson("upload", f"Uploaded {r['file']}", file=r["file"])
                    else:
                        yield ndjson(
                            "upload",
                            f"Upload failed for {r['file']}: {r.get('stderr') or 'unknown'}",
                            file=r["file"],
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

            if do_mega and not all_success:
                failed = [r for r in upload_results if not r["success"]]
                yield ndjson(
                    "error",
                    "MEGA upload failed: "
                    + "; ".join(f"{r['file']}: {r.get('stderr') or 'unknown'}" for r in failed),
                    status="partial_upload",
                    title=session.safe_title,
                    pages=len(image_files),
                    mega_path=mega_remote_dir,
                    uploads=upload_results,
                )
                return

            done_msg = (
                f"Done! {len(image_files)} pages → MEGA {mega_remote_dir}/"
                f"{file_base}.{{cbz,mokuro,webp}}"
                if do_mega
                else f"Done! {len(image_files)} pages OCR'd → {staging}"
            )
            yield ndjson(
                "done",
                done_msg,
                status="success",
                title=session.safe_title,
                series=series_dir_name,
                pages=len(image_files),
                pages_ocr_done=snap["pages_ocr_done"],
                mega_path=mega_remote_dir if do_mega else None,
                output_dir=str(staging) if not do_mega else None,
                staging=str(staging),
                uploads=upload_results,
                reader_url="https://reader.mokuro.app/" if do_mega else None,
            )
        except Exception as e:
            yield ndjson("error", f"Finalize failed: {e}")
        finally:
            if megarc_path and Path(megarc_path).exists():
                Path(megarc_path).unlink(missing_ok=True)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Health ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    result = {
        "app": APP_NAME,
        "version": __version__,
        "status": "ok",
        "mokuro_installed": False,
        "mokuro_custom_fork": _MOKURO_REPO is not None,
        "mokuro_repo": str(_MOKURO_REPO) if _MOKURO_REPO is not None else None,
        "megatools_installed": bool(shutil.which("megatools")),
        "mega_configured": False,
        "mega_creds_source": None,
        "mega_library_root": MEGA_LIBRARY_ROOT,
        "upload_default": _MEGA_UPLOAD_DEFAULT,
        "work_dir": str(WORK_DIR),
        "output_dir": str(OUTPUT_DIR),
        "cors_origins": CORS_ORIGINS,
        "active_sessions": len(_sessions),
        "ocr_chunk_size": _OCR_CHUNK_SIZE,
        "ocr_idle_flush_s": _OCR_IDLE_FLUSH_S,
        "ocr_queue_depth": len(_ocr_queue),
    }
    try:
        import mokuro as m

        result["mokuro_installed"] = True
        result["mokuro_version"] = getattr(m, "__version__", "?")
        result["mokuro_path"] = getattr(m, "__file__", "?")
    except ImportError:
        pass

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
        "--setup-mega",
        action="store_true",
        help="Interactively store MEGA credentials in the OS keychain / "
        "credential store (macOS Keychain, Windows Credential Manager, Linux "
        "Secret Service) or, failing that, a 0600 credentials file.",
    )
    parser.add_argument("--host", default=os.environ.get("MOKURO_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MOKURO_BRIDGE_PORT", "62642")),
    )
    args = parser.parse_args()

    if args.setup_mega:
        _run_setup_mega()
        return

    import uvicorn

    print("=" * 60)
    print(f"  {APP_NAME} v{__version__} on http://{args.host}:{args.port}")
    print(f"  work dir:   {WORK_DIR}")
    print(f"  output dir: {OUTPUT_DIR} (local mode)")
    print(f"  MEGA root:  {MEGA_LIBRARY_ROOT} (upload default: {_MEGA_UPLOAD_DEFAULT})")
    if _MOKURO_REPO is not None:
        print(f"  custom mokuro repo: {_MOKURO_REPO}")
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
