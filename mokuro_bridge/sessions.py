from __future__ import annotations
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from .config import SESSIONS_DIR, WORK_DIR
from .util import _chmod_fd_private

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
    lock: threading.RLock = field(default_factory=threading.RLock)
    message: str = "Session started"
    finalized: bool = False
    upload: Optional[dict] = None   # live upload progress during finalize
    cover_uploaded: Optional[dict] = None  # {method, url?, path?} once the cover .webp was pushed early
    finalizing: bool = False
    cover_uploading: bool = False
    ingesting: bool = False
    finalize_wait_task: Optional[object] = None
    finalize_upload_task: Optional[object] = None
    finalize_owner_task: Optional[object] = None
    finalize_assembly_task: Optional[object] = None
    finalize_cleanup_task: Optional[object] = None
    finalize_pack_task: Optional[object] = None
    page_reservations: set[str] = field(default_factory=set)
    finalize_lock_handle: Optional[object] = None

_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _session_path(session_id: str) -> Optional[Path]:
    """Return a safe per-session state path, or None for malformed IDs.

    Session IDs are normally 12 lowercase hex characters. Keep a small
    compatibility set for older/local callers, but never interpolate an
    arbitrary path component supplied by an HTTP request.
    """
    value = str(session_id or "")
    if not _SESSION_ID_RE.fullmatch(value):
        return None
    root = SESSIONS_DIR.resolve()
    path = (SESSIONS_DIR / f"{value}.json").resolve()
    if path.parent != root:
        return None
    return path


def _safe_volume_dir(value: object) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        if path.is_symlink():
            return None
        path = path.resolve()
        work_root = WORK_DIR.resolve()
        path.relative_to(work_root)
        if path == work_root:
            raise ValueError("volume path must be a child of WORK_DIR")
        if path.parent != work_root:
            raise ValueError("volume path must be a direct child of WORK_DIR")
        if path.name.startswith(".") or path.name.casefold() in {"_ocr", "output"}:
            raise ValueError("reserved WORK_DIR child cannot be a volume")
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def _persist_session(session: Session) -> bool:
    path = _session_path(session.session_id)
    if path is None:
        return False
    temporary: Optional[Path] = None
    try:
        # Serialize the snapshot and replace under the session lock. A plain
        # write_text can leave truncated JSON if the process exits mid-write or
        # two OCR/upload threads persist the same session concurrently.
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
                # Never persist a provider URL/share token; only the metadata
                # needed to avoid re-uploading an already completed cover.
                "cover_uploaded": (
                    {
                        key: session.cover_uploaded[key]
                        for key in ("method", "file", "path", "remote_path", "size", "sha256")
                        if key in session.cover_uploaded
                    }
                    if isinstance(session.cover_uploaded, dict)
                    else None
                ),
            }
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(SESSIONS_DIR)
            )
            temporary = Path(temporary_name)
            try:
                _chmod_fd_private(fd)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(json.dumps(data, ensure_ascii=False))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if fd != -1:
                    os.close(fd)
            temporary = None
            return True
    except Exception as e:
        print(f"[mokuro-bridge] persist session failed: {e}")
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
        return False

def _delete_persisted_session(session_id: str) -> None:
    path = _session_path(session_id)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

def _refresh_restored_session(session: Session) -> None:
    # lazy import: ocr.py imports .sessions at module level. A missing optional
    # OCR package must not turn a harmless status lookup into a 500.
    try:
        from .ocr import _sync_ocr_cache_state
        _sync_ocr_cache_state(session)
    except Exception as error:
        with session.lock:
            if "persist" in str(error).lower():
                session.message = "OCR state could not be persisted; resume is not durable"
            else:
                session.message = f"OCR cache sync unavailable: {error}"


def _portable_component(value: str) -> bool:
    if not value or value.endswith((".", " ")) or any(ch in value for ch in '<>:"|?*\\'):
        return False
    stem = value.split(".", 1)[0].upper()
    return stem not in {"CON", "PRN", "AUX", "NUL"} and not re.fullmatch(r"COM[1-9]|LPT[1-9]", stem)


def _safe_component(value: object, *, extension: str | None = None) -> bool:
    text = str(value or "")
    if not text or len(text) > 255 or Path(text).name != text:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return False
    if text in {".", ".."} or text.startswith(".") or not _portable_component(text):
        return False
    if extension is not None and Path(text).suffix.lower() not in extension:
        return False
    return True


def _validate_cover_metadata(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    method = value.get("method")
    if not isinstance(method, str) or not re.fullmatch(r"(?:local|(?:mega|drive|onedrive)(?::[a-z0-9_-]+)?)", method):
        return None
    file_name = value.get("file")
    if not _safe_component(file_name, extension={".webp"}):
        return None
    path_value = value.get("path")
    if path_value is not None and (not isinstance(path_value, str) or len(path_value) > 4096 or "\x00" in path_value):
        return None
    remote_path = value.get("remote_path")
    if remote_path is not None and (not isinstance(remote_path, str) or len(remote_path) > 4096 or any(ord(ch) < 32 for ch in remote_path)):
        return None
    sha256 = value.get("sha256")
    if sha256 is not None and (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
        return None
    size = value.get("size")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        return None
    return {key: value[key] for key in ("method", "file", "path", "remote_path", "size", "sha256") if key in value}


def _load_persisted_session(session_id: str, *, sync: bool = True) -> Optional[Session]:
    path = _session_path(session_id)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    persisted_id = data.get("session_id")
    if str(persisted_id or "") != str(session_id):
        return None
    vol_dir = _safe_volume_dir(data.get("vol_dir"))
    if vol_dir is None or not vol_dir.is_dir() or vol_dir.is_symlink():
        return None
    safe_title_value = data.get("safe_title")
    if not _safe_component(safe_title_value) or safe_title_value != vol_dir.name:
        return None
    title_value = data.get("title")
    if not isinstance(title_value, str) or len(title_value) > 4096 or any(ord(ch) < 32 or ord(ch) == 127 for ch in title_value):
        title_value = vol_dir.name
    finalized_value = data.get("finalized", False)
    if not isinstance(finalized_value, bool):
        return None

    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}
    def _names(key: str) -> set[str]:
        values = data.get(key) or []
        if not isinstance(values, list):
            return set()
        return {
            value for value in values
            if _safe_component(value, extension=image_extensions)
        }

    message_value = data.get("message")
    if not isinstance(message_value, str) or len(message_value) > 4096:
        message_value = "Restored session"
    session = Session(
        session_id=str(session_id),
        title=title_value,
        safe_title=safe_title_value,
        vol_dir=vol_dir,
        pages_received=_names("pages_received"),
        pages_ocr_done=_names("pages_ocr_done"),
        pages_ocr_failed=_names("pages_ocr_failed"),
        message=message_value,
        finalized=finalized_value,
        cover_uploaded=_validate_cover_metadata(data.get("cover_uploaded")),
    )
    if sync:
        _refresh_restored_session(session)
    print(f"[mokuro-bridge] restored session {session_id} from disk ({session.vol_dir.name})")
    return session

def _get_session(session_id: str) -> Session:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session:
            return session
    session = _load_persisted_session(session_id, sync=False)
    if session:
        if session.finalized:
            raise HTTPException(status_code=400, detail="Session already finalized")
        with _sessions_lock:
            # Another request may have restored the same ID while this one was
            # reading disk. Reuse that object so callers never mutate divergent
            # copies of one session.
            session = _sessions.setdefault(session_id, session)
        _refresh_restored_session(session)
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
            if session.safe_title.casefold() == str(safe_title).casefold():
                return session
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or str(data.get("safe_title") or "").casefold() != str(safe_title).casefold():
            continue
        sid = data.get("session_id")
        if not isinstance(sid, str):
            continue
        session = _load_persisted_session(sid, sync=False)
        if session:
            with _sessions_lock:
                session = _sessions.setdefault(sid, session)
            _refresh_restored_session(session)
            return session
    return None

def session_snapshot(session: Session) -> dict:
    with session.lock:
        received = len(session.pages_received)
        done = len(session.pages_ocr_done)
        failed = len(session.pages_ocr_failed)
        pending = received - done - failed
        reservations = sorted(session.page_reservations)
        return {
            "session_id": session.session_id,
            "title": session.title,
            "safe_title": session.safe_title,
            "pages_received": received,
            "pages_ocr_done": done,
            "pages_ocr_failed": failed,
            "pages_ocr_pending": max(0, pending) + len(reservations),
            "pages_ingesting": reservations,
            "source_ingesting": bool(session.ingesting and not reservations),
            "message": session.message,
            "finalized": session.finalized,
            "upload": session.upload,
            "cover_uploaded": (
                {
                    key: session.cover_uploaded[key]
                    for key in ("method", "file", "path", "remote_path", "size", "sha256")
                    if key in session.cover_uploaded
                }
                if isinstance(session.cover_uploaded, dict)
                else None
            ),
        }
