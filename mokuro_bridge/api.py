from __future__ import annotations
import asyncio
import json
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import APP_NAME, __version__
from .config import (
    CORS_ORIGINS,
    DRIVE_ROOT_NAME,
    IMAGE_EXTENSIONS,
    MEGA_LIBRARY_ROOT,
    MIN_PAGES_FOR_MEGA,
    ONEDRIVE_ROOT_NAME,
    OUTPUT_DIR,
    WEBDAV_ROOT_NAME,
    WORK_DIR,
    _LOCAL_DIR_STATE_FILE,
    _LOCAL_INGEST_ROOTS,
    _MEGA_UPLOAD_DEFAULT,
    _OCR_CHUNK_SIZE,
    _OCR_IDLE_FLUSH_S,
    _load_remembered_local_dir,
    _remember_local_dir,
)
from .creds import _mega_creds_source
from .ocr import (
    _MOKURO_REPO,
    _assemble_lock,
    _ensure_ocr_worker,
    _fork_supported,
    _mokuro_pkg,
    _mokuro_submodule,
    _ocr_queue,
    _queue_page_ocr,
    _sync_ocr_cache_state,
    cleanup_volume_artifacts,
    find_mokuro_output,
    wait_for_session_ocr,
)
from .providers import (
    _build_upload_methods,
    _default_upload_method,
    _method_current_folder,
    _remember_upload_method,
    resolve_upload_method,
    upload_file,
)
from .sessions import (
    Session,
    _delete_persisted_session,
    _find_session_by_safe_title,
    _get_session,
    _persist_session,
    _sessions,
    _sessions_lock,
    session_snapshot,
)
from .util import sanitize_filename, series_title_from_volume, _truthy

app = FastAPI(title=APP_NAME, version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

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

def _effective_local_output_base(explicit_local_dir: str) -> Optional[Path]:
    """Resolve the local output base for a finalize (call only for "local").

    - explicit local_dir → validated, used, and remembered as the sticky default
    - otherwise a remembered sticky default → validated and used
    - otherwise None (the caller falls back to OUTPUT_DIR)

    An explicit disallowed path raises HTTP 400 (via _resolve_local_output_dir).
    A remembered path that has since become disallowed (e.g. HOME changed) is
    forgotten and ignored rather than erroring. Returns an absolute Path/None.
    """
    if str(explicit_local_dir).strip():
        base = _resolve_local_output_dir(explicit_local_dir)
        _remember_local_dir(str(base))
        return base
    remembered = _load_remembered_local_dir()
    if not remembered:
        return None
    try:
        return _resolve_local_output_dir(remembered)
    except HTTPException:
        # Remembered dir no longer usable — drop it and fall back to OUTPUT_DIR.
        try:
            _LOCAL_DIR_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return None

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
    overwrite: str = Form("fail"),
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
    # Like the upload method, an explicit local_dir is sticky: it becomes the
    # default local output for later local finalizes (persisted).
    local_output_base = (
        _effective_local_output_base(local_dir) if method == "local" else None
    )

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

                        ok, err, url = upload_file(method, local_path, remote_dir, _on_progress, overwrite)
                        duration_s = round(time.monotonic() - start, 2)
                        results.append(
                            {
                                "file": remote_name,
                                "size": local_path.stat().st_size,
                                "success": ok,
                                "stderr": err if not ok else None,
                                "url": url if ok else None,
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
