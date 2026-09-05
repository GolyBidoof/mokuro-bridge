from __future__ import annotations
import importlib
import os
import shutil
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Optional

from .config import IMAGE_EXTENSIONS, _OCR_CHUNK_SIZE, _OCR_IDLE_FLUSH_S
from .sessions import Session, _persist_session, _session_or_none, session_snapshot
from . import log as _log

# Dedup set for full-traceback dumps in _mark_page_failed (log each root
# cause once per process so a failing batch doesn't spam 500 tracebacks).
_ocr_error_tracebacks_seen: set = set()

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
    sibling = Path(__file__).resolve().parent.parent / "mokuro"
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

_generator_lock = threading.Lock()
_assemble_lock = threading.Lock()

_generator = None  # lazy MokuroGenerator

# Global OCR flush queue (shared across sessions / capture clients)
_ocr_lock = threading.Lock()
_ocr_cv = threading.Condition(_ocr_lock)
_ocr_queue: deque[tuple[str, str]] = deque()  # (session_id, filename)
_ocr_processing: set[tuple[str, str]] = set()
_ocr_force_sessions: set[str] = set()  # finalize wants these drained ASAP
_ocr_worker_started = False

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

    _log.info(
        "ocr",
        f"OCR flush {len(items)} page(s) (engine: {'fork batch' if use_fork_api else 'stock'}): "
        + ", ".join(f"{sid[:6]}/{fn}" for sid, fn in items[:6])
        + ("…" if len(items) > 6 else ""),
    )

    if use_fork_api:
        _ocr_batch_fork(items, gen, mpocr, on_progress_cb=_log.progress)
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

    # Recognize crops in sub-batches of ocr_batch_size. A single giant
    # recognize_text(all_crops) call over hundreds of crops stalls on MPS
    # (one enormous tensor→list decode, no progress) — chunking keeps each
    # call bounded and lets us report progress per sub-batch.
    ocr_batch_size = max(1, int(ocr_batch_size))
    try:
        for sub_start in range(0, len(all_crops), ocr_batch_size):
            sub_crops = all_crops[sub_start : sub_start + ocr_batch_size]
            sub_map = crop_map[sub_start : sub_start + ocr_batch_size]
            _log.progress(
                "ocr",
                f"Recognizing crops {sub_start + 1}–{min(sub_start + len(sub_crops), len(all_crops))}"
                f" of {len(all_crops)}…",
            )
            texts = mpocr.recognize_text(
                sub_crops,
                batch_size=ocr_batch_size,
                num_beams=1,
            )
            for local_idx, text in enumerate(texts):
                sid, fname, crop_idx = sub_map[local_idx]
                result = page_results.get((sid, fname))
                if result is None:
                    continue
                blk_idx, line_idx = crop_meta_map[(sid, fname, crop_idx)]
                result["blocks"][blk_idx]["lines"][line_idx] += text
            if on_progress_cb is not None:
                on_progress_cb(min(sub_start + len(sub_crops), len(all_crops)), len(all_crops))
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
    _log.progress(
        "ocr",
        f"{session.safe_title}: OCR {done}/{total} pages",
    )

def _mark_page_failed(session: Session, filename: str, error: Exception) -> None:
    with session.lock:
        session.pages_ocr_failed.add(filename)
        session.message = f"OCR failed on {filename}: {error}"
    _log.error("ocr", f"{session.safe_title}: page {filename} failed: {error}")
    # Dump the full traceback once per unique error so root causes (e.g. an
    # int leaking into a str-method deep in transformers) are visible in the
    # console instead of only the final message.
    import traceback
    key = f"{type(error).__name__}: {error}"
    if key not in _ocr_error_tracebacks_seen:
        _ocr_error_tracebacks_seen.add(key)
        _log.error("ocr", f"first occurrence of {key!r} — traceback:\n{traceback.format_exc()}")
    _persist_session(session)

def _ocr_worker_loop() -> None:
    try:
        _get_generator()
        _log.info(
            "ocr",
            f"OCR engine ready (chunk={_OCR_CHUNK_SIZE}, idle={_OCR_IDLE_FLUSH_S}s)",
        )
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
            _log.progress("ocr", f"Dispatching OCR batch of {len(batch)} page(s)…")
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
