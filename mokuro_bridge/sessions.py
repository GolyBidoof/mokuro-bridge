from __future__ import annotations
import json
import threading
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException

from .config import SESSIONS_DIR

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
    upload: Optional[dict] = None   # live upload progress during finalize

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
    # lazy import: ocr.py imports .sessions at module level
    from .ocr import _sync_ocr_cache_state

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
            "upload": session.upload,
        }
