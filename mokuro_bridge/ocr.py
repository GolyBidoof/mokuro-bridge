from __future__ import annotations
import contextlib
import hashlib
import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from .util import _chmod_fd_private
from .config import (
    IMAGE_EXTENSIONS,
    _OCR_CHUNK_SIZE,
    _OCR_FAIR_SCHEDULING,
    _OCR_FINALIZE_PRIORITY_RATIO,
    _OCR_IDLE_FLUSH_S,
)
from .sessions import (
    Session,
    _persist_session,
    _safe_component,
    _session_or_none,
    session_snapshot,
)
from . import log as _log

# Dedup set for full-traceback dumps in _mark_page_failed (log each root
# cause once per process so a failing batch doesn't spam 500 tracebacks).
_ocr_error_tracebacks_seen: set = set()
_OCR_MAX_BATCH_RETRIES = 3
_ocr_item_retry_counts: dict[tuple[str, str], int] = {}


class _OcrBatchFailure(RuntimeError):
    """An OCR batch error affecting only the listed queue items."""

    def __init__(self, items: list[tuple[str, str]], cause: BaseException):
        self.items = tuple(items)
        super().__init__(str(cause))


@contextlib.contextmanager
def _quiet_model_load():
    """Silence model-load console noise while mokuro initializes its OCR
    models, so the bridge console stays clean. Suppresses:
      - tqdm bars (redirected to devnull, kept iterable — transformers iterates)
      - transformers logging (LOAD REPORT banner, generation-flags warning)
      - loguru INFO lines from the fork (Initializing text detector, Loading
        OCR model, Using MPS, OCR ready, ...) — WARNING/ERROR still pass
    All restored on exit."""
    import logging
    import os as _os

    devnull = open(_os.devnull, "w")
    tqdm_orig = None
    try:
        import tqdm.auto
        tqdm_orig = tqdm.auto.tqdm

        class _SilentTqdm(tqdm_orig):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("file", devnull)
                super().__init__(*args, **kwargs)

            def clear(self, *a, **k):
                pass

        tqdm.auto.tqdm = _SilentTqdm
    except Exception:
        pass
    tf_logger = None
    tf_logger_prev = None
    try:
        from transformers import logging as tf_logging
        tf_logger = tf_logging.get_logger("transformers")
        tf_logger_prev = tf_logger.level
        tf_logger.setLevel(logging.ERROR)
    except Exception:
        pass
    # loguru (used by the mokuro fork): temporarily replace its handlers with
    # a WARNING-level sink so model-init INFO lines are hidden but real
    # warnings/errors still print. Restored on exit.
    loguru_state = None
    try:
        import loguru
        ids = list(loguru.logger._core.handlers.keys())
        sink = loguru.logger._core.handlers[ids[0]]._sink if ids else None
        loguru_state = (ids, sink)
        loguru.logger.remove()
        loguru.logger.add(sink or sys.stderr, level="WARNING")
    except Exception:
        pass
    # huggingface_hub "unauthenticated requests" warning → silence
    hf_logger = None
    hf_prev = None
    try:
        import logging as _logging
        hf_logger = _logging.getLogger("huggingface_hub")
        hf_prev = hf_logger.level
        hf_logger.setLevel(_logging.ERROR)
    except Exception:
        pass
    try:
        yield
    finally:
        if tqdm_orig is not None:
            try:
                import tqdm.auto
                tqdm.auto.tqdm = tqdm_orig
            except Exception:
                pass
        if tf_logger is not None and tf_logger_prev is not None:
            tf_logger.setLevel(tf_logger_prev)
        if loguru_state is not None:
            try:
                import loguru
                loguru.logger.remove()
                ids, sink = loguru_state
                for _id in ids:
                    loguru.logger.add(sink or sys.stderr, level=0)
            except Exception:
                pass
        if hf_logger is not None and hf_prev is not None:
            try:
                hf_logger.setLevel(hf_prev)
            except Exception:
                pass
        try:
            devnull.close()
        except Exception:
            pass

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
        candidate = Path(env_path).expanduser().resolve()
        return candidate if candidate.is_dir() else None
    sibling = (Path(__file__).resolve().parent.parent / "mokuro").resolve()
    if sibling.is_dir() and (sibling / "mokuro").is_dir() and not sibling.is_symlink():
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

_generator_lock = threading.Lock()
_assemble_lock = threading.Lock()

_generator = None  # lazy MokuroGenerator
_fork_supported_cache: Optional[bool] = None

# Global OCR flush queue (shared across sessions / capture clients)
_ocr_lock = threading.Lock()
_ocr_cv = threading.Condition(_ocr_lock)
_ocr_queue: deque[tuple[str, str]] = deque()  # (session_id, filename)
_ocr_processing: set[tuple[str, str]] = set()
_ocr_force_sessions: set[str] = set()  # finalize wants these drained ASAP
_ocr_worker_started = False
_ocr_worker_thread: Optional[threading.Thread] = None

# Fair scheduling state.  The queue remains a flat tuple deque for backwards
# compatibility with callers/tests that inspect it; this small cursor tracks
# which session should receive the next slice when several volumes are queued.
_ocr_session_order: deque[str] = deque()
_ocr_schedule_cursor: Optional[str] = None
_ocr_force_turn = False

def _fork_supported() -> bool:
    """Whether the loaded mokuro exposes the fork's batched OCR API.

    Capability detection is cached: constructing a generator merely for a
    health poll used to repeat model/device setup and could take seconds.
    """
    global _fork_supported_cache
    if _fork_supported_cache is not None:
        return _fork_supported_cache
    with _generator_lock:
        if _fork_supported_cache is not None:
            return _fork_supported_cache
        try:
            generator_mod = _mokuro_submodule("mokuro_generator")
            page_ocr_mod = _mokuro_submodule("manga_page_ocr")
            page_ocr_has_fork_api = (
                hasattr(page_ocr_mod.MangaPageOcr, "detect_and_extract")
                and hasattr(page_ocr_mod.MangaPageOcr, "recognize_text")
            )
            generator_has_batch_size = hasattr(generator_mod.MokuroGenerator(), "ocr_batch_size")
            _fork_supported_cache = bool(page_ocr_has_fork_api and generator_has_batch_size)
        except (ImportError, AttributeError):
            _fork_supported_cache = False
        except Exception:
            # A transient model/device/import failure must not permanently
            # force the stock path; the next batch may probe again.
            return False
        return _fork_supported_cache

def _get_generator():
    """Lazy-init the mokuro engine (models stay warm across pages/sessions)."""
    global _generator
    with _generator_lock:
        if _generator is None:
            MokuroGenerator = _mokuro_submodule("mokuro_generator").MokuroGenerator

            candidate = MokuroGenerator()
            with _quiet_model_load():
                candidate.init_models()
            _generator = candidate
        return _generator

def ocr_json_path(volume, img_name: str) -> Path:
    """Per-page OCR JSON path in mokuro's cache layout.

    mokuro keeps one JSON per page under <volume_parent>/_ocr/<volume_name>/
    (same name as the image, .json extension). This matches what the stock
    mokuro package writes and reads, so the bridge and mokuro interoperate.
    """
    return (volume.path_ocr_cache / img_name).with_suffix(".json")

def _safe_ocr_cache_path(
    path: Path, cache_root: Optional[Path] = None, *, allow_missing: bool = False
) -> bool:
    try:
        if path.is_symlink():
            return False
        if not path.is_file() and not (allow_missing and not path.exists()):
            return False
        if cache_root is not None:
            if cache_root.exists() and (cache_root.is_symlink() or not cache_root.is_dir()):
                return False
            root = cache_root.resolve()
            if cache_root.is_symlink() or path.resolve().parent != root:
                return False
        return True
    except (OSError, RuntimeError):
        return False


def _image_fingerprint(path: Path) -> Optional[str]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        finally:
            if fd != -1:
                os.close(fd)
    except OSError:
        return None


def _is_link_like(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return bool(path.is_symlink() or (is_junction is not None and is_junction()))
    except OSError:
        return True


def _valid_ocr_cache(path: Path, image: Optional[Path] = None) -> bool:
    if _is_link_like(path):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    # The generator indexes each cache entry as a mapping before adding
    # `img_path`; accepting a list here would mark a page done and then fail
    # during final assembly. Keep the validator and generator shape aligned.
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        return False
    if image is not None:
        expected = _image_fingerprint(image)
        sidecar_path = path.with_name(path.name + ".sha256")
        if _is_link_like(sidecar_path):
            return False
        try:
            recorded = sidecar_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        return bool(expected) and recorded == expected
    return True


def _sync_ocr_cache_state(session: Session) -> None:
    """Reconcile a persisted session and requeue pages lacking OCR JSON.

    Restoring only the ``done`` set leaves missing pages invisible to the
    worker, so ``wait_for_session_ocr`` can wait forever. Keep the filesystem
    cache as the source of truth and enqueue the remainder in fair-session
    order before the caller starts waiting.
    """
    with session.lock:
        original_state = (
            set(session.pages_received),
            set(session.pages_ocr_done),
            set(session.pages_ocr_failed),
            session.message,
        )
    queued_added: list[tuple[str, str]] = []
    try:
        volume_mod = _mokuro_submodule("volume")
        volume = volume_mod.Volume(session.vol_dir)
    except Exception:
        # Status/restore remains useful when the optional OCR package is not
        # installed; the stock cache path can still be used for reconciliation.
        volume = None

    cache_root = None
    if volume is not None:
        try:
            cache_root = volume.path_ocr_cache
        except Exception:
            cache_root = None
    if cache_root is None:
        cache_root = session.vol_dir.parent / "_ocr" / session.vol_dir.name
    if session.vol_dir.is_symlink():
        return
    if not session.vol_dir.is_dir():
        try:
            session.vol_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
    try:
        images = sorted(
            p.name
            for p in session.vol_dir.iterdir()
            if not p.is_symlink() and p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            and _safe_component(p.name, extension=IMAGE_EXTENSIONS)
        )
    except OSError:
        return
    stem_owners: dict[str, str] = {}
    collisions: set[str] = set()
    for name in images:
        key = Path(name).stem.casefold()
        if key in stem_owners:
            collisions.add(name)
            collisions.add(stem_owners[key])
        else:
            stem_owners[key] = name
    if collisions:
        images = [name for name in images if name not in collisions]
    image_set = set(images)
    # Validate/hash cache files before taking the scheduler lock. State changes
    # below are committed atomically with finalization admission and queue
    # publication, so a concurrent finalizer cannot observe a half-synced set.
    valid_done: set[str] = set()
    for name in images:
        if volume is not None:
            json_path = ocr_json_path(volume, name)
        else:
            json_path = session.vol_dir.parent / "_ocr" / session.vol_dir.name / (Path(name).stem + ".json")
        if _safe_ocr_cache_path(json_path, cache_root) and _valid_ocr_cache(json_path, session.vol_dir / name):
            valid_done.add(name)

    with _ocr_cv:
        with session.lock:
            if session.finalized or session.finalizing:
                return
            if collisions:
                session.pages_received -= collisions
                session.pages_ocr_done -= collisions
                session.pages_ocr_failed -= collisions
                session.message = "Rejected colliding image filenames"
            # Drop ghost entries after MEGA cleanup / deleted volume dirs.
            session.pages_received &= image_set
            session.pages_ocr_done &= image_set
            session.pages_ocr_failed &= image_set
            missing: list[str] = []
            for name in images:
                session.pages_received.add(name)
                if name in valid_done:
                    session.pages_ocr_done.add(name)
                    session.pages_ocr_failed.discard(name)
                elif name in session.pages_ocr_done:
                    # A persisted success is only valid while its cache file still
                    # exists; otherwise requeue it instead of producing a gap.
                    session.pages_ocr_done.discard(name)
                    missing.append(name)
                elif name not in session.pages_ocr_failed:
                    missing.append(name)
            done = len(session.pages_ocr_done)
            total = len(session.pages_received)
            session.message = f"Resumed — OCR {done}/{total} cached"
            for name in missing:
                item = (session.session_id, name)
                if item not in _ocr_queue and item not in _ocr_processing:
                    _ocr_queue.append(item)
                    queued_added.append(item)
                    if session.session_id not in _ocr_session_order:
                        _ocr_session_order.append(session.session_id)
            # Keep the queue update and durable snapshot in the same
            # critical section as finalization's admission check.
            if not _persist_session(session):
                for item in queued_added:
                    try:
                        _ocr_queue.remove(item)
                    except ValueError:
                        pass
                if not any(item[0] == session.session_id for item in _ocr_queue):
                    try:
                        _ocr_session_order.remove(session.session_id)
                    except ValueError:
                        pass
                (
                    session.pages_received,
                    session.pages_ocr_done,
                    session.pages_ocr_failed,
                    session.message,
                ) = original_state
                raise RuntimeError("could not persist resumed OCR state")
            _ocr_cv.notify_all()

def _queue_page_ocr(
    session: Session, safe_name: str, *, _ocr_cv_held: bool = False
) -> dict:
    """Queue a page atomically with the caller's OCR critical section when held."""
    cached = False
    was_received = False
    with session.lock:
        if session.finalized or session.finalizing:
            raise RuntimeError("session is finalizing or finalized")
        was_received = safe_name in session.pages_received
        was_done = safe_name in session.pages_ocr_done
        was_failed = safe_name in session.pages_ocr_failed
        old_message = session.message
        session.pages_received.add(safe_name)
        if safe_name in session.pages_ocr_done:
            received = len(session.pages_received)
            done = len(session.pages_ocr_done)
            session.pages_ocr_failed.discard(safe_name)
            session.message = f"Captured {received} · OCR {done}/{received} (cached)"
            cached = True
        else:
            session.pages_ocr_failed.discard(safe_name)
            received = len(session.pages_received)
            done = len(session.pages_ocr_done)
            session.message = f"Captured {received} · OCR {done}/{received}"

    if not _ocr_cv_held:
        _ensure_ocr_worker()

    def _enqueue_locked():
        item = (session.session_id, safe_name)
        queued_now = item not in _ocr_queue and item not in _ocr_processing
        _ocr_item_retry_counts.pop(item, None)
        if queued_now:
            _ocr_queue.append(item)
            if session.session_id not in _ocr_session_order:
                _ocr_session_order.append(session.session_id)
        _ocr_cv.notify()
        return queued_now

    def _restore_state():
        with session.lock:
            if was_done:
                session.pages_ocr_done.add(safe_name)
            else:
                session.pages_ocr_done.discard(safe_name)
            if was_failed:
                session.pages_ocr_failed.add(safe_name)
            else:
                session.pages_ocr_failed.discard(safe_name)
            if was_received:
                session.pages_received.add(safe_name)
            else:
                session.pages_received.discard(safe_name)
            session.message = old_message

    if cached:
        if not _ocr_cv_held:
            with _ocr_cv:
                if not _persist_session(session):
                    _restore_state()
                    raise RuntimeError("could not persist OCR page state")
        elif not _persist_session(session):
            _restore_state()
            raise RuntimeError("could not persist OCR page state")
        snap = session_snapshot(session)
        snap["cached"] = True
        return snap

    if not _ocr_cv_held:
        with _ocr_cv:
            queued_now = _enqueue_locked()
            if not _persist_session(session):
                if queued_now:
                    try:
                        _ocr_queue.remove((session.session_id, safe_name))
                    except ValueError:
                        pass
                _restore_state()
                raise RuntimeError("could not persist OCR queue state")
    else:
        queued_now = _enqueue_locked()
        if not _persist_session(session):
            if queued_now:
                try:
                    _ocr_queue.remove((session.session_id, safe_name))
                except ValueError:
                    pass
            _restore_state()
            raise RuntimeError("could not persist OCR queue state")
    return session_snapshot(session)

def _pending_count_for(session_id: str) -> int:
    return sum(1 for sid, _ in _ocr_queue if sid == session_id) + sum(
        1 for sid, _ in _ocr_processing if sid == session_id
    )


def _active_ocr_sessions_locked() -> list[str]:
    """Return queued session ids in the next fair-turn order.

    ``_ocr_queue`` deliberately stays a flat deque so existing integrations can
    inspect or clear it.  This companion cursor supplies the missing fairness:
    sessions are rotated after each selected batch, rather than always starting
    with whichever volume happened to enqueue first.  The caller must hold
    ``_ocr_cv``.
    """
    global _ocr_schedule_cursor
    counts: dict[str, int] = {}
    for session_id, _filename in _ocr_queue:
        counts[session_id] = counts.get(session_id, 0) + 1
    if not counts:
        _ocr_session_order.clear()
        _ocr_schedule_cursor = None
        return []

    # Drop sessions that have no queued pages, then register sessions that may
    # have been inserted by a test or an older worker path directly in the deque.
    for session_id in list(_ocr_session_order):
        if session_id not in counts:
            try:
                _ocr_session_order.remove(session_id)
            except ValueError:
                pass
    for session_id in counts:
        if session_id not in _ocr_session_order:
            _ocr_session_order.append(session_id)

    if _ocr_schedule_cursor not in _ocr_session_order:
        _ocr_schedule_cursor = None
    if _ocr_schedule_cursor is not None:
        try:
            cursor_index = _ocr_session_order.index(_ocr_schedule_cursor)
        except ValueError:
            _ocr_schedule_cursor = None
        else:
            # Start immediately after the last session selected previously.
            _ocr_session_order.rotate(-(cursor_index + 1))
    return list(_ocr_session_order)


def _take_fair_items(
    session_order: list[str], limit: int
) -> list[tuple[str, str]]:
    """Take up to ``limit`` items, cycling through the given session ids.

    The queue is compacted after selection, but relative page order within each
    session is preserved.  Caller must hold ``_ocr_cv``.
    """
    global _ocr_schedule_cursor
    if not _ocr_queue or limit <= 0 or not session_order:
        return []

    by_session: dict[str, deque[tuple[str, str]]] = {}
    for item in _ocr_queue:
        by_session.setdefault(item[0], deque()).append(item)

    selected: list[tuple[str, str]] = []
    turn = [sid for sid in session_order if by_session.get(sid)]
    while turn and len(selected) < limit:
        next_turn: list[str] = []
        for session_id in turn:
            if len(selected) >= limit:
                break
            page_queue = by_session[session_id]
            if not page_queue:
                continue
            selected.append(page_queue.popleft())
            if page_queue:
                next_turn.append(session_id)
        if not next_turn:
            break
        turn = next_turn

    if selected:
        remaining = deque(
            item
            for session_id in by_session
            for item in by_session[session_id]
        )
        _ocr_queue.clear()
        _ocr_queue.extend(remaining)
        _ocr_schedule_cursor = selected[-1][0]
    return selected


def _take_fair_batch_locked(force_batch: bool) -> list[tuple[str, str]]:
    """Pick a fair mixed-session batch. Caller must hold ``_ocr_cv``.

    A finalize session gets a bounded share of a mixed batch.  The remaining
    slots are deliberately reserved for sessions that are not waiting on
    finalize, so a large forced volume cannot monopolize the sole worker.  When
    only one session is queued, retain the historical larger finalize batch cap.
    """
    order = _active_ocr_sessions_locked()
    if not order:
        return []

    forced = [sid for sid in order if sid in _ocr_force_sessions]
    ordinary = [sid for sid in order if sid not in _ocr_force_sessions]
    active_count = len(order)
    if not forced:
        # Ordinary idle/queue flushes retain the configured chunk size even
        # when only one session is active.
        return _take_fair_items(order, min(_OCR_CHUNK_SIZE, len(_ocr_queue)))
    # Keep the old 32-page force flush for a lone finalize volume, but use the
    # configured normal batch size once another volume can make progress.
    batch_limit = (
        max(_OCR_CHUNK_SIZE, 32)
        if force_batch and active_count == 1
        else _OCR_CHUNK_SIZE
    )

    global _ocr_force_turn
    if ordinary and batch_limit > 1:
        force_limit = int(batch_limit * _OCR_FINALIZE_PRIORITY_RATIO)
        force_limit = max(1, force_limit)
        force_limit = min(force_limit, batch_limit - 1)
    elif ordinary:
        # A one-page batch cannot be split proportionally. Alternate turns so
        # a continuously fed finalize queue cannot starve ordinary volumes.
        force_limit = 1 if _ocr_force_turn else 0
        _ocr_force_turn = not _ocr_force_turn
    else:
        force_limit = batch_limit

    selected = _take_fair_items(forced, force_limit)
    remaining = batch_limit - len(selected)
    if ordinary and remaining:
        selected.extend(_take_fair_items(ordinary, remaining))
    return selected


def _take_ocr_batch_legacy() -> list[tuple[str, str]]:
    """Historical FIFO/finalize-priority selector (opt-out compatibility mode)."""
    if not _ocr_queue:
        return []

    force_cap = max(_OCR_CHUNK_SIZE, 32)

    # Finalize drain: prefer pages for sessions that are waiting on finalize.
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


def _take_ocr_batch() -> list[tuple[str, str]]:
    """Pick next flush batch. Caller must hold ``_ocr_cv``."""
    if not _OCR_FAIR_SCHEDULING:
        return _take_ocr_batch_legacy()
    if not _ocr_queue:
        return []
    if _ocr_force_sessions:
        return _take_fair_batch_locked(force_batch=True)
    if len(_ocr_queue) >= _OCR_CHUNK_SIZE:
        return _take_fair_batch_locked(force_batch=False)
    return []


def _take_idle_batch() -> list[tuple[str, str]]:
    """Flush whatever is waiting after idle timeout. Caller must hold ``_ocr_cv``."""
    if not _ocr_queue:
        return []
    if _OCR_FAIR_SCHEDULING:
        return _take_fair_batch_locked(force_batch=bool(_ocr_force_sessions))
    n = min(len(_ocr_queue), _OCR_CHUNK_SIZE)
    return [_ocr_queue.popleft() for _ in range(n)]

def ocr_queue_metrics() -> dict:
    """Return a read-only, count-only snapshot of the shared OCR queue.

    The snapshot intentionally contains session ids and numeric counts only: it
    is safe for a monitoring client to poll without exposing page names, volume
    paths, or credentials.  Queue and processing state are captured under the
    same condition lock used by the worker so the totals are coherent.
    """
    with _ocr_cv:
        pending_by: dict[str, int] = {}
        for session_id, _filename in _ocr_queue:
            pending_by[session_id] = pending_by.get(session_id, 0) + 1
        processing_by: dict[str, int] = {}
        for session_id, _filename in _ocr_processing:
            processing_by[session_id] = processing_by.get(session_id, 0) + 1
        active_batch_sessions = sorted(processing_by, key=str)
        worker_started = bool(_ocr_worker_started)
        worker_alive = bool(
            _ocr_worker_thread is not None and _ocr_worker_thread.is_alive()
        )
        worker_state = (
            "stopped"
            if not worker_started or not worker_alive
            else "processing"
            if active_batch_sessions
            else "idle"
        )
        queue_depth = len(_ocr_queue)
        processing_depth = len(_ocr_processing)
        model_loaded = _generator is not None

    # Include live sessions with no queued pages as zero-count rows, while
    # keeping the lazy import here to avoid the sessions <-> ocr import cycle.
    try:
        from .sessions import _sessions, _sessions_lock
        with _sessions_lock:
            live_sessions = list(_sessions.items())
    except Exception:
        live_sessions = []
    ingesting_by: dict[str, int] = {}
    source_ingesting: set[str] = set()
    for session_id, session in live_sessions:
        lock = getattr(session, "lock", None)
        if lock is None:
            continue
        with lock:
            reservations = getattr(session, "page_reservations", ())
            ingesting_by[session_id] = len(reservations)
            if getattr(session, "ingesting", False):
                source_ingesting.add(session_id)
    live_session_ids = [session_id for session_id, _session in live_sessions]

    session_ids = set(pending_by) | set(processing_by) | set(live_session_ids)
    per_session = []
    for session_id in sorted(session_ids, key=str):
        row = {
            "session_id": session_id,
            "pending": pending_by.get(session_id, 0) + ingesting_by.get(session_id, 0),
            "processing": processing_by.get(session_id, 0),
        }
        if ingesting_by.get(session_id, 0):
            row["ingesting"] = ingesting_by[session_id]
        if session_id in source_ingesting:
            row["source_ingesting"] = True
        per_session.append(row)
    return {
        "scheduler": "round_robin" if _OCR_FAIR_SCHEDULING else "fifo",
        "queue_depth": queue_depth,
        "processing_depth": processing_depth,
        "total_depth": queue_depth + processing_depth + sum(ingesting_by.values()),
        "per_session": per_session,
        "active_batch_sessions": active_batch_sessions,
        "active_sessions": len(live_session_ids),
        "source_ingesting_sessions": len(source_ingesting),
        "worker": {
            "started": worker_started,
            "alive": worker_alive,
            "state": worker_state,
            "model_loaded": model_loaded,
        },
    }


def _process_ocr_batch(items: list[tuple[str, str]]) -> None:
    """Serialize model use with mokuro artifact assembly."""
    if not items:
        return
    with _assemble_lock:
        _process_ocr_batch_unlocked(items)


def _process_ocr_batch_unlocked(items: list[tuple[str, str]]) -> None:
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

    gen = _get_generator()  # lazily inits models (quietly) and keeps them warm
    mpocr = gen.mpocr
    use_fork_api = _fork_supported()

    if use_fork_api:
        _ocr_batch_fork(
            items,
            gen,
            mpocr,
            on_progress_cb=lambda done, total: _log.progress(
                "ocr",
                f"OCR {done}/{total} crops",
            ),
        )
    else:
        _ocr_batch_stock(items, gen, mpocr)

def _valid_ocr_result_shape(value: object) -> bool:
    if isinstance(value, list):
        return all(isinstance(item, dict) for item in value)
    return isinstance(value, dict) and isinstance(value.get("blocks"), list)


def _ocr_batch_stock(items: list[tuple[str, str]], gen, mpocr) -> None:
    """Stock mokuro: one full page OCR call per page (detect + recognize)."""
    Volume = _mokuro_submodule("volume").Volume
    failures: list[tuple[str, str]] = []
    last_error: BaseException | None = None
    for session_id, filename in items:
        session = _session_or_none(session_id)
        if not session:
            continue
        if session.ingesting:
            failures.append((session_id, filename))
            last_error = RuntimeError("session is ingesting")
            continue
        img_path = session.vol_dir / filename
        try:
            volume = Volume(session.vol_dir)
            result = _json_safe(mpocr(str(img_path)))
            if not _valid_ocr_result_shape(result):
                raise ValueError("OCR engine returned an invalid page result")
            _save_page_result(session, volume, filename, result)
        except Exception as error:
            failures.append((session_id, filename))
            last_error = error
    if failures:
        raise _OcrBatchFailure(failures, last_error or RuntimeError("OCR page failed"))

def _ocr_batch_fork(
    items: list[tuple[str, str]],
    gen,
    mpocr,
    on_progress_cb=None,
) -> None:
    """Fork mokuro: detect all pages, then batched recognize_text calls."""
    utils_mod = _mokuro_submodule("utils")
    imread = utils_mod.imread
    Volume = _mokuro_submodule("volume").Volume

    ocr_batch_size = getattr(gen, "ocr_batch_size", 48) or 48
    page_results: dict[tuple[str, str], dict] = {}
    crop_meta_map: dict[tuple[str, str, int], tuple[int, int]] = {}
    all_crops: list = []
    crop_map: list[tuple[str, str, int]] = []
    failed: list[tuple[str, str]] = []
    last_error: BaseException | None = None

    for session_id, filename in items:
        session = _session_or_none(session_id)
        if not session:
            continue
        if session.ingesting:
            failed.append((session_id, filename))
            last_error = RuntimeError("session is ingesting")
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
        except Exception as error:
            failed.append((session_id, filename))
            last_error = error

    if not page_results:
        if failed:
            raise _OcrBatchFailure(failed, last_error or RuntimeError("OCR detection failed"))
        return

    # Recognize crops in sub-batches of ocr_batch_size. A single giant
    # recognize_text(all_crops) call over hundreds of crops stalls on MPS
    # (one enormous tensor→list decode, no progress) — chunking keeps each
    # call bounded and lets us report progress per sub-batch.
    ocr_batch_size = max(1, int(ocr_batch_size))
    recognition_failed = False
    try:
        for sub_start in range(0, len(all_crops), ocr_batch_size):
            sub_crops = all_crops[sub_start : sub_start + ocr_batch_size]
            sub_map = crop_map[sub_start : sub_start + ocr_batch_size]
            texts = mpocr.recognize_text(
                sub_crops,
                batch_size=ocr_batch_size,
                num_beams=getattr(gen, "num_beams", 4) or 4,
            )
            if not isinstance(texts, (list, tuple)) or len(texts) != len(sub_crops):
                actual = len(texts) if isinstance(texts, (list, tuple)) else type(texts).__name__
                raise RuntimeError(
                    f"OCR engine returned {actual} text result(s) for {len(sub_crops)} crop(s)"
                )
            for local_idx, text in enumerate(texts):
                sid, fname, crop_idx = sub_map[local_idx]
                result = page_results.get((sid, fname))
                if result is None:
                    continue
                blk_idx, line_idx = crop_meta_map[(sid, fname, crop_idx)]
                result["blocks"][blk_idx]["lines"][line_idx] += text
            if on_progress_cb is not None:
                done = min(sub_start + len(sub_crops), len(all_crops))
                on_progress_cb(done, len(all_crops))
    except Exception as error:
        recognition_failed = True
        last_error = error
        failed.extend(page_results.keys())

    if not recognition_failed:
        for item, result in list(page_results.items()):
            sid, fname = item
            session = _session_or_none(sid)
            if not session:
                continue
            try:
                volume = Volume(session.vol_dir)
                safe_result = _json_safe(result)
                if not _valid_ocr_result_shape(safe_result):
                    raise ValueError("OCR engine returned an invalid page result")
                _save_page_result(session, volume, fname, safe_result)
            except Exception as error:
                failed.append(item)
                last_error = error
    if failed:
        raise _OcrBatchFailure(failed, last_error or RuntimeError("OCR page failed"))

def _json_safe(value):
    """Convert NumPy-like OCR values into strict JSON primitives."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _json_safe(tolist())
        except Exception:
            pass
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except Exception:
            pass
    return str(value)


def _save_page_result(session: Session, volume, filename: str, result: dict) -> None:
    """Write one page's OCR JSON into a no-follow cache path."""
    dump_json = _mokuro_submodule("utils").dump_json
    root = Path(volume.path_ocr_cache)
    work_root = session.vol_dir.parent.resolve()
    try:
        if root.is_symlink() or root.parent.is_symlink() or root.resolve().parent.parent != work_root:
            raise RuntimeError("unsafe OCR cache directory")
        root.parent.mkdir(parents=True, exist_ok=True)
        if root.parent.is_symlink() or root.exists() and not root.is_dir():
            raise RuntimeError("unsafe OCR cache directory")
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError("could not create OCR cache directory") from error
    image_path = session.vol_dir / filename
    fingerprint = _image_fingerprint(image_path)
    if not fingerprint:
        raise RuntimeError("could not fingerprint source image")
    json_path = ocr_json_path(volume, filename)
    if json_path.is_symlink():
        json_path.unlink(missing_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{json_path.name}.", dir=str(root))
    temporary = Path(temporary_name)
    try:
        _chmod_fd_private(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(result, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, json_path)
        temporary = None
    finally:
        if fd != -1:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    sidecar = json_path.with_name(json_path.name + ".sha256")
    if not _safe_ocr_cache_path(sidecar, root, allow_missing=True):
        raise RuntimeError("unsafe OCR fingerprint path")
    sidecar_fd, sidecar_name = tempfile.mkstemp(prefix=f".{sidecar.name}.", dir=str(root))
    sidecar_tmp = Path(sidecar_name)
    try:
        _chmod_fd_private(sidecar_fd)
        with os.fdopen(sidecar_fd, "w", encoding="ascii") as sidecar_handle:
            sidecar_fd = -1
            sidecar_handle.write(fingerprint)
            sidecar_handle.flush()
            os.fsync(sidecar_handle.fileno())
        os.replace(sidecar_tmp, sidecar)
        sidecar_tmp = None
    finally:
        if sidecar_fd != -1:
            os.close(sidecar_fd)
        if sidecar_tmp is not None:
            sidecar_tmp.unlink(missing_ok=True)
    with _ocr_cv:
        with session.lock:
            session.pages_ocr_done.add(filename)
            session.pages_ocr_failed.discard(filename)
            done = len(session.pages_ocr_done)
            total = len(session.pages_received)
            session.message = f"OCR {done}/{total} pages"
            if not _persist_session(session):
                session.pages_ocr_done.discard(filename)
                # Keep the item retryable when only the state write failed;
                # the cache itself is valid and a later retry can persist it.
                session.pages_ocr_failed.discard(filename)
                raise RuntimeError("could not persist completed OCR state")
        _ocr_item_retry_counts.pop((session.session_id, filename), None)
        _ocr_cv.notify_all()
    if done >= total:
        _log.info("ocr", f"{session.safe_title}: OCR complete ({done} pages)")

def _mark_page_failed(session: Session, filename: str, error: Exception) -> None:
    with _ocr_cv:
        with session.lock:
            session.pages_ocr_failed.add(filename)
            session.pages_ocr_done.discard(filename)
            session.message = f"OCR failed on {filename}: {error}"
            if not _persist_session(session):
                session.pages_ocr_failed.discard(filename)
                raise RuntimeError("could not persist failed OCR state")
        _log.error("ocr", f"{session.safe_title}: page {filename} failed: {error}")
        # Dump the full traceback once per unique error so root causes (e.g. an
        # int leaking into a str-method deep in transformers) are visible in the
        # console instead of only the final message.
        import traceback
        key = f"{type(error).__name__}: {error}"
        if key not in _ocr_error_tracebacks_seen:
            _ocr_error_tracebacks_seen.add(key)
            _log.error("ocr", f"first occurrence of {key!r} — traceback:\n{traceback.format_exc()}")
        _ocr_item_retry_counts.pop((session.session_id, filename), None)
        _ocr_cv.notify_all()

def _ocr_worker_loop() -> None:
    try:
        _get_generator()
    except Exception as e:
        _log.error("ocr", f"OCR engine model init error: {e}")

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
            with _ocr_cv:
                for item in batch:
                    _ocr_item_retry_counts.pop(item, None)
        except Exception as e:
            # A page-local detection/recognition failure is retried only for
            # the affected items; successful pages in the same batch remain
            # durable. Generic batch/import failures affect the whole batch.
            retry_items = list(getattr(e, "items", batch))
            retry_set = set(retry_items)
            with _ocr_cv:
                for completed_item in batch:
                    if completed_item not in retry_set:
                        _ocr_item_retry_counts.pop(completed_item, None)
            # Resolve sessions before taking _ocr_cv: _session_or_none() may
            # lazily restore state and that path also needs the same lock.
            sessions = {sid: _session_or_none(sid) for sid, _ in retry_items}
            _log.error("ocr", f"OCR batch failed; retrying {len(retry_items)} page(s): {e}")
            with _ocr_cv:
                for item in retry_items:
                    _ocr_processing.discard(item)
                    session = sessions.get(item[0])
                    if session is None:
                        _ocr_item_retry_counts.pop(item, None)
                        continue
                    with session.lock:
                        unfinished = (
                            item[1] not in session.pages_ocr_done
                            and item[1] not in session.pages_ocr_failed
                        )
                    if not unfinished:
                        _ocr_item_retry_counts.pop(item, None)
                        continue
                    attempts = _ocr_item_retry_counts.get(item, 0) + 1
                    _ocr_item_retry_counts[item] = attempts
                    if attempts >= _OCR_MAX_BATCH_RETRIES:
                        with session.lock:
                            session.pages_ocr_failed.add(item[1])
                            session.pages_ocr_done.discard(item[1])
                            session.message = f"OCR failed on {item[1]} after {attempts} attempts"
                            if not _persist_session(session):
                                session.pages_ocr_failed.discard(item[1])
                                if item not in _ocr_queue:
                                    _ocr_queue.append(item)
                                continue
                        _ocr_item_retry_counts.pop(item, None)
                        continue
                    if item not in _ocr_queue:
                        _ocr_queue.append(item)
                        if item[0] not in _ocr_session_order:
                            _ocr_session_order.append(item[0])
                _ocr_cv.notify_all()
            time.sleep(1)
        finally:
            with _ocr_cv:
                for item in batch:
                    _ocr_processing.discard(item)
                _ocr_cv.notify_all()

def _ensure_ocr_worker() -> None:
    global _ocr_worker_started, _ocr_worker_thread
    with _ocr_lock:
        if _ocr_worker_started and _ocr_worker_thread is not None and _ocr_worker_thread.is_alive():
            return
        if _ocr_worker_started:
            _ocr_worker_started = False
            _ocr_worker_thread = None
        t = threading.Thread(target=_ocr_worker_loop, name="mokuro-ocr-flush", daemon=True)
        _ocr_worker_thread = t
        try:
            t.start()
        except Exception:
            _ocr_worker_thread = None
            _ocr_worker_started = False
            raise
        _ocr_worker_started = True

def wait_for_session_ocr(session: Session) -> None:
    """Block until every received page for this session is OCR'd (or failed)."""
    sid = session.session_id
    _ensure_ocr_worker()
    try:
        with _ocr_cv:
            _ocr_force_sessions.add(sid)
            _ocr_cv.notify()
            while True:
                pending = _pending_count_for(sid)
                with session.lock:
                    done = len(session.pages_ocr_done) + len(session.pages_ocr_failed)
                    total = len(session.pages_received)
                if pending == 0 and done >= total:
                    return
                _ocr_cv.wait(timeout=0.5)
    finally:
        with _ocr_cv:
            _ocr_force_sessions.discard(sid)
            _ocr_cv.notify_all()

def find_mokuro_output(vol_dir: Path) -> Optional[Path]:
    """mokuro writes <title>.mokuro as a sibling of the volume directory."""
    sibling = vol_dir.parent / f"{vol_dir.name}.mokuro"
    if sibling.is_file() and not sibling.is_symlink():
        return sibling
    inside = [p for p in vol_dir.glob("*.mokuro") if p.is_file() and not p.is_symlink()]
    if inside:
        return inside[0]
    parent_matches = [
        p for p in vol_dir.parent.glob("*.mokuro")
        if p.is_file() and not p.is_symlink() and p.stem == vol_dir.name
    ]
    return parent_matches[0] if parent_matches else None

def cleanup_volume_artifacts(vol_dir: Path) -> None:
    sibling_mokuro = vol_dir.parent / f"{vol_dir.name}.mokuro"
    sibling_html = vol_dir.parent / f"{vol_dir.name}.html"
    ocr_cache = vol_dir.parent / "_ocr" / vol_dir.name
    if vol_dir.is_symlink():
        raise RuntimeError("refusing to clean a symlinked work volume")
    if vol_dir.exists():
        if not vol_dir.is_dir():
            raise RuntimeError("work-volume path is not a directory")
        shutil.rmtree(vol_dir)
        if vol_dir.exists() or vol_dir.is_symlink():
            raise RuntimeError("work-volume cleanup did not complete")
    for path in (sibling_mokuro, sibling_html):
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.exists():
            path.unlink()
    if ocr_cache.is_symlink():
        ocr_cache.unlink(missing_ok=True)
    elif ocr_cache.exists():
        if not ocr_cache.is_dir():
            raise RuntimeError("OCR-cache path is not a directory")
        shutil.rmtree(ocr_cache)
        if ocr_cache.exists() or ocr_cache.is_symlink():
            raise RuntimeError("OCR-cache cleanup did not complete")
