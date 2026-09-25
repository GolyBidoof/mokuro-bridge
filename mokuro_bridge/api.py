from __future__ import annotations
import asyncio
import contextlib
import errno
import fnmatch
import hashlib
import io
import json
import os
import queue as _queue
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import weakref
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, Response

from . import APP_NAME, __version__
from . import fetchproxy as _fetchproxy
from . import log as _log
from .config import (
    CORS_ORIGINS,
    IMAGE_EXTENSIONS,
    MEGA_LIBRARY_ROOT,
    MIN_PAGES_FOR_MEGA,
    OUTPUT_DIR,
    WORK_DIR,
    _LOCAL_DIR_STATE_FILE,
    _LOCAL_INGEST_ROOTS,
    _MEGA_UPLOAD_DEFAULT,
    _OCR_CHUNK_SIZE,
    _OCR_IDLE_FLUSH_S,
    SESSIONS_DIR,
    _load_remembered_local_dir,
    _remember_local_dir,
)
from .creds import _mega_creds_source
from . import ocr as _ocr
from .ocr import (
    _MOKURO_REPO,
    _ensure_ocr_worker,
    _fork_supported,
    _mokuro_pkg,
    _mokuro_submodule,
    _ocr_queue,
    _ocr_cv,
    _ocr_processing,
    _queue_page_ocr,
    ocr_queue_metrics,
    _sync_ocr_cache_state,
    _valid_ocr_cache,
    _safe_ocr_cache_path,
    ocr_json_path,
    cleanup_volume_artifacts,
    find_mokuro_output,
    wait_for_session_ocr,
)
from .providers import (
    _build_upload_methods,
    _default_upload_method,
    _method_current_folder,
    _remember_upload_method,
    method_label,
    method_provider,
    method_root,
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
from .util import _chmod_fd_private, sanitize_filename, series_title_from_volume, _truthy

def _cors_origin_regex() -> Optional[str]:
    patterns = [str(pattern) for pattern in CORS_ORIGINS if any(ch in str(pattern) for ch in "*?")]
    if not patterns:
        return None
    return "^(?:" + "|".join(fnmatch.translate(pattern) for pattern in patterns) + ")$"


app = FastAPI(title=APP_NAME, version=__version__)

# ── Activity tracker ───────────────────────────────────────────────────
# A lightweight, thread-safe record of what the bridge is doing right now.
# health() reads it to report `busy` + `busy_stage` so a polling client can
# tell "idle" from "OCR running" from "uploading" without parsing sessions.
import threading as _threading

_activity_lock = _threading.Lock()
_activity = {"stage": "idle", "detail": "", "owners": {}}
_session_create_lock = _threading.RLock()
_assemble_locks_guard = _threading.Lock()
_assemble_locks: dict[str, _threading.Lock] = {}
_title_locks_guard = _threading.Lock()
_title_locks: "weakref.WeakKeyDictionary[object, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()
_local_finalize_guard = _threading.Lock()
_local_finalize_leases: set[str] = set()
_local_creation_guard = _threading.Lock()
_local_creation_leases: set[str] = set()
_upload_slots_guard = _threading.Lock()
_upload_slots: "weakref.WeakKeyDictionary[object, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_UPLOAD_MEMORY_CONCURRENCY = 2
_SOURCE_MEMORY_SEMAPHORE = threading.BoundedSemaphore(2)


def _title_async_lock(safe_title: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _title_locks_guard:
        locks = _title_locks.setdefault(loop, {})
        return locks.setdefault(str(safe_title).casefold(), asyncio.Lock())


def _is_link_like(path: Path) -> bool:
    """Treat symlinks and Windows reparse-point junctions as unsafe paths."""
    try:
        is_junction = getattr(path, "is_junction", None)
        return bool(path.is_symlink() or (is_junction is not None and is_junction()))
    except OSError:
        return True


def _open_lock_file_nofollow(path: Path):
    if _is_link_like(path):
        raise OSError("lock path is a reparse point")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("lock path is not a regular file")
        _chmod_fd_private(fd)
        return os.fdopen(fd, "a+")
    except BaseException:
        os.close(fd)
        raise


def _acquire_finalize_lock(session_id: str):
    """Acquire a non-blocking cross-process lease for one finalization."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(session_id or "")):
        return None
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = SESSIONS_DIR / f".{session_id}.finalize.lock"
        handle = _open_lock_file_nofollow(path)
    except OSError:
        return None
    if handle is not None:
        result = _try_platform_lock(handle)
        if result is True:
            return handle
        handle.close()
        if result is False:
            return None
    # Neither POSIX nor Windows advisory locking is available; retain the
    # same lease semantics in-process rather than admitting a duplicate.
    key = f"local:{session_id}"
    with _local_finalize_guard:
        if key in _local_finalize_leases:
            return None
        _local_finalize_leases.add(key)
    return key


def _release_finalize_lock(handle) -> None:
    if handle is None:
        return
    if isinstance(handle, str) and handle.startswith("local:"):
        with _local_finalize_guard:
            _local_finalize_leases.discard(handle)
        return
    try:
        _unlock_platform(handle)
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _assemble_session_lock(session_id: str) -> _threading.Lock:
    with _assemble_locks_guard:
        return _assemble_locks.setdefault(session_id, _threading.Lock())


def _drop_assemble_session_lock(session_id: str) -> None:
    with _assemble_locks_guard:
        _assemble_locks.pop(session_id, None)


def _set_activity(stage: str, detail: str = "", owner: str = "") -> None:
    with _activity_lock:
        key = owner or "_global"
        _activity["owners"][key] = (stage, detail)
        if stage != "idle":
            _activity["stage"] = stage
            _activity["detail"] = detail


def _clear_activity(owner: str = "") -> None:
    with _activity_lock:
        _activity["owners"].pop(owner or "_global", None)
        remaining = list(_activity["owners"].values())
        if not remaining:
            _activity["stage"] = "idle"
            _activity["detail"] = ""
            return
        stage, detail = next(
            (entry for entry in remaining if entry[0] == "uploading"),
            remaining[0],
        )
        _activity["stage"] = stage
        _activity["detail"] = detail


def _try_platform_lock(handle) -> Optional[bool]:
    """Try a non-blocking cross-process lock once; ``None`` means unsupported."""
    try:
        import fcntl
    except ImportError:
        try:
            import msvcrt
        except ImportError:
            return None
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (OSError, ValueError) as error:
            if isinstance(error, OSError) and error.errno in (
                errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
            ):
                return False
            return None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError) as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        return None


def _unlock_platform(handle) -> None:
    try:
        import fcntl
    except ImportError:
        try:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError, ValueError):
            pass
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass


@contextlib.asynccontextmanager
async def _volume_creation_lock(safe_title: str):
    """Serialize same-title creation without blocking the event loop."""
    canonical_title = str(safe_title).casefold()
    lock_path = WORK_DIR / f".{canonical_title}.create.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = _open_lock_file_nofollow(lock_path)
    local_key = f"local:{canonical_title}"
    local_acquired = False
    try:
        result = await asyncio.to_thread(_try_platform_lock, handle)
        if result is None:
            with _local_creation_guard:
                if local_key in _local_creation_leases:
                    raise RuntimeError("same-title session is already being created")
                _local_creation_leases.add(local_key)
                local_acquired = True
        else:
            while not result:
                await asyncio.sleep(0.05)
                result = await asyncio.to_thread(_try_platform_lock, handle)
        yield
    finally:
        if local_acquired:
            with _local_creation_guard:
                _local_creation_leases.discard(local_key)
        else:
            await asyncio.to_thread(_unlock_platform, handle)
        try:
            handle.close()
        except Exception:
            pass


def _current_activity() -> tuple[str, str]:
    with _activity_lock:
        return _activity["stage"], _activity["detail"]


def _set_upload_state(session, *, active, method=None, file=None, percent=None,
                      current_bytes=None, total_bytes=None, speed_bps=None,
                      speed_human=None, remote_path=None, url=None, error=None):
    """Publish live per-session upload state for GET /session/{id}/status.

    Called from the upload thread (already throttled to ~4 frames/sec by
    _on_progress) and at per-file start/end. Writes under session.lock so the
    snapshot sees a consistent dict.
    """
    with session.lock:
        session.upload = {
            "active": active,
            "method": method,
            "file": file,
            "percent": percent,
            "current_bytes": current_bytes,
            "total_bytes": total_bytes,
            "speed_bps": speed_bps,
            "speed_human": speed_human,
            "remote_path": remote_path,
            "url": url,
            "error": error,
        }


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

def _origin_allowed(origin: str) -> bool:
    return bool(origin) and any(fnmatch.fnmatchcase(origin, str(pattern)) for pattern in CORS_ORIGINS)


@app.middleware("http")
async def _allow_private_network_requests(request, call_next):
    """Answer Chromium's private-network preflight for allowed viewers.

    The outer middleware handles this preflight directly because older
    Starlette CORS versions reject the PNA request header before routing.
    """
    if request.method == "OPTIONS" and request.headers.get("access-control-request-private-network", "").lower() == "true":
        origin = request.headers.get("origin", "")
        if not _origin_allowed(origin):
            return Response(status_code=403)
        requested_method = request.headers.get("access-control-request-method", "").upper()
        if requested_method not in {"POST", "GET", "OPTIONS"}:
            return Response(status_code=405)
        requested_headers = request.headers.get("access-control-request-headers", "")
        if requested_headers:
            # The configured policy allows all headers, but still reject header
            # names containing control characters rather than echoing them.
            if any(not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name.strip())
                   for name in requested_headers.split(",")):
                return Response(status_code=400)
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": requested_headers or "*",
                "Access-Control-Allow-Private-Network": "true",
                "Vary": "Origin, Access-Control-Request-Private-Network, Access-Control-Request-Method, Access-Control-Request-Headers",
            },
        )
    response = await call_next(request)
    origin = request.headers.get("origin", "")
    if _origin_allowed(origin) and request.headers.get("access-control-request-private-network", "").lower() == "true":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        vary = response.headers.get("Vary", "")
        vary_names = [part.strip() for part in vary.split(",") if part.strip()]
        existing_names = {part.casefold() for part in vary_names}
        for name in ("Origin", "Access-Control-Request-Private-Network"):
            if name.casefold() not in existing_names:
                vary_names.append(name)
                existing_names.add(name.casefold())
        response.headers["Vary"] = ", ".join(vary_names)
    return response

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

def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left_path = os.path.normcase(str(left.resolve())).casefold()
        right_path = os.path.normcase(str(right.resolve())).casefold()
    except (OSError, RuntimeError):
        left_path = os.path.normcase(str(left.absolute())).casefold()
        right_path = os.path.normcase(str(right.absolute())).casefold()
    if left_path == right_path:
        return True
    return left_path.startswith(right_path + os.sep) or right_path.startswith(left_path + os.sep)


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

try:
    from natsort import natsorted as _natsorted
except ImportError:  # pragma: no cover - optional dependency
    _natsorted = None


def _natural_path_key(path: Path) -> tuple:
    parts = re.split(r"(\d+)", path.name.lower())
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in parts)


def _has_stem_collision(vol_dir: Path, destination: Path) -> bool:
    try:
        return any(
            p.name != destination.name
            and p.is_file() and p.stem.casefold() == destination.stem.casefold()
            for p in vol_dir.iterdir()
            if p.suffix.lower() in IMAGE_EXTENSIONS
        )
    except OSError:
        return False


async def _run_thread_owned(func, *args):
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_task_even_if_cancelled(task)
        raise


async def _drain_task_even_if_cancelled(task):
    if task is None:
        return
    async def _wait():
        try:
            await task
        except BaseException:
            pass
    drain = asyncio.create_task(_wait())
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            continue


def _safe_provider_url(value: object) -> Optional[str]:
    """Keep only a public provider locator; never return signed query/fragment tokens."""
    try:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        host = parsed.hostname
        if not host:
            return None
        host = host.casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            return None
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except Exception:
        return None


def _safe_provider_error(value: object) -> str:
    text = str(value or "")
    def _redact(match):
        safe = _safe_provider_url(match.group(0).rstrip(".,);]"))
        return safe or "[redacted-url]"
    return re.sub(r"https?://[^\s<>\"']+", _redact, text)[:2000]


def _file_digest(path: Path) -> Optional[str]:
    fd = -1
    try:
        if path.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if fd != -1:
            os.close(fd)


def _normalize_cover_bytes(data: bytes) -> bytes:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            converted = image.convert("RGBA") if image.mode in {"RGBA", "LA", "P"} else image.convert("RGB")
            converted.save(output, format="WEBP")
            return output.getvalue()
    except Exception as error:
        raise HTTPException(status_code=400, detail="Cover could not be converted to WebP") from error


def _looks_like_image(data: bytes) -> bool:
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > 100 * 1024 * 1024:
        return False
    head = bytes(data[:32])
    signatures = (
        b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM",
        b"II*\x00", b"MM\x00*",
    )
    if any(head.startswith(sig) for sig in signatures):
        return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return (
        len(head) >= 12
        and head[4:8] == b"ftyp"
        and head[8:12] in {b"avif", b"avis"}
    )


def _write_no_follow(destination: Path, data: bytes) -> None:
    # Kept as a compatibility alias; all writes are staged and replaced so a
    # failed write cannot truncate a previously valid artifact.
    _write_atomic_no_follow(destination, data)


def _write_atomic_no_follow(destination: Path, data: bytes) -> None:
    if destination.is_symlink():
        raise HTTPException(status_code=400, detail="Destination filename must not be a symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    temporary = Path(temporary_name)
    try:
        _chmod_fd_private(fd)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.is_symlink():
            raise HTTPException(status_code=400, detail="Destination filename must not be a symlink")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if fd != -1:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_source_limited(source: Path, limit: int = 100 * 1024 * 1024) -> bytes:
    with _open_source_nofollow(source) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="Source image is too large")
    return data


def _open_source_nofollow(source: Path):
    if _is_link_like(source):
        raise HTTPException(status_code=400, detail="Could not open source safely")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Could not open source safely") from error
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HTTPException(status_code=400, detail="Source is not a regular file")
    return os.fdopen(fd, "rb")


def _copy_no_follow(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise HTTPException(status_code=400, detail="Destination filename must not be a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = None
    created = False
    try:
        with _open_source_nofollow(source) as src:
            fd = os.open(destination, flags, 0o600)
            created = True
            with os.fdopen(fd, "wb") as dst:
                fd = None
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail="Destination already exists") from error
    except (OSError, ValueError) as error:
        if created:
            try: destination.unlink(missing_ok=True)
            except Exception: pass
        raise HTTPException(status_code=400, detail="Could not copy destination safely") from error
    finally:
        if fd is not None:
            os.close(fd)


def _copy_replace_no_follow(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise HTTPException(status_code=400, detail="Output filename must not be a symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    temporary = Path(temporary_name)
    try:
        _chmod_fd_private(fd)
        with _open_source_nofollow(source) as src, os.fdopen(fd, "wb") as dst:
            fd = -1
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if destination.is_symlink():
            raise HTTPException(status_code=400, detail="Output filename must not be a symlink")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if fd != -1:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _zip_replace_no_follow(destination: Path, files: list[Path]) -> None:
    if destination.is_symlink():
        raise HTTPException(status_code=400, detail="Output filename must not be a symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    temporary = Path(temporary_name)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zf:
            for image in files:
                if image.is_symlink() or not image.is_file():
                    raise HTTPException(status_code=400, detail="Source image is not a regular file")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    source_fd = os.open(image, flags)
                except (OSError, ValueError) as error:
                    raise HTTPException(status_code=400, detail="Could not open source image safely") from error
                try:
                    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                        raise HTTPException(status_code=400, detail="Source image is not a regular file")
                    with os.fdopen(source_fd, "rb") as source_handle, zf.open(image.name, "w") as target_handle:
                        source_fd = -1
                        shutil.copyfileobj(source_handle, target_handle)
                finally:
                    if source_fd != -1:
                        os.close(source_fd)
        os.chmod(temporary, 0o600)
        if destination.is_symlink():
            raise HTTPException(status_code=400, detail="Output filename must not be a symlink")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cover_replace(source: Path, destination: Path) -> None:
    if source.suffix.lower() == ".webp":
        try:
            from PIL import Image
            with Image.open(source) as image:
                image.verify()
        except Exception as error:
            raise HTTPException(status_code=400, detail="Cover WebP is corrupt") from error
        _copy_replace_no_follow(source, destination)
        return
    try:
        from PIL import Image
        with Image.open(source) as image:
            fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
            temporary = Path(temporary_name)
            os.close(fd)
            try:
                converted = image.convert("RGBA") if image.mode in {"RGBA", "LA", "P"} else image.convert("RGB")
                converted.save(temporary, format="WEBP")
                os.chmod(temporary, 0o600)
                if destination.is_symlink():
                    raise HTTPException(status_code=400, detail="Output filename must not be a symlink")
                os.replace(temporary, destination)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=400, detail="Could not convert cover to WebP") from error


def _natural_sorted_paths(paths: list[Path]) -> list[Path]:
    if _natsorted is not None:
        return list(_natsorted(paths, key=lambda p: p.name))
    return sorted(paths, key=_natural_path_key)


def _upload_memory_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _upload_slots_guard:
        return _upload_slots.setdefault(
            loop, asyncio.Semaphore(_UPLOAD_MEMORY_CONCURRENCY)
        )


@contextlib.asynccontextmanager
async def _upload_memory_slot():
    # Bound the worst-case in-memory page/cover buffers without tying a
    # semaphore to a closed event loop.
    async with _upload_memory_semaphore():
        yield


async def _read_upload_limited(upload: UploadFile, limit: int = 100 * 1024 * 1024) -> bytes | bytearray:
    # A single growable buffer avoids retaining a list of chunks and a second
    # full-size join allocation for large page/cover uploads. Callers hold the
    # upload-memory slot through validation and the durable write.
    buffer = bytearray()
    while True:
        chunk = await upload.read(min(1024 * 1024, limit - len(buffer) + 1))
        if not chunk:
            break
        if len(buffer) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Uploaded file is too large")
        buffer.extend(chunk)
    return buffer


def _validated_page_name(filename: str, fallback: str) -> str:
    raw = str(filename or "").strip()
    if len(raw) > 255:
        raise HTTPException(status_code=400, detail="filename is too long")
    if raw:
        safe_name = Path(raw).name
        if safe_name != raw or not safe_name or safe_name.startswith("."):
            raise HTTPException(status_code=400, detail="filename must be a plain image filename")
        if Path(safe_name).suffix.lower() not in IMAGE_EXTENSIONS or not _portable_component(safe_name):
            raise HTTPException(status_code=400, detail="filename must use an image extension")
        return safe_name
    safe_fallback = Path(fallback).name
    if not safe_fallback or Path(safe_fallback).suffix.lower() not in IMAGE_EXTENSIONS or not _portable_component(safe_fallback):
        raise HTTPException(status_code=400, detail="a valid fallback image filename is required")
    return safe_fallback


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

def _ensure_safe_work_volume(path: Path) -> Path:
    root = WORK_DIR.resolve()
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail="Volume path could not be resolved") from error
    if path.is_symlink() or resolved.parent != root or resolved == root:
        raise HTTPException(status_code=400, detail="Volume path must be a direct, non-symlink child of WORK_DIR")
    resolved.mkdir(parents=True, exist_ok=True)
    try:
        resolved.chmod(0o700)
    except OSError:
        pass
    return resolved


def _portable_component(value: str) -> bool:
    if not value or len(value) > 255 or value.endswith((".", " ")):
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if any(ch in value for ch in '<>:"|?*\\'):
        return False
    stem = value.split(".", 1)[0].upper()
    return stem not in {"CON", "PRN", "AUX", "NUL"} and not re.fullmatch(r"COM[1-9]|LPT[1-9]", stem)


def _validated_series(value: str) -> str:
    if not _portable_component(value):
        raise HTTPException(status_code=400, detail="Derived series name is unsafe")
    return value


def _validate_volume_title(safe_title: str) -> str:
    reserved = {"_ocr", "output", "local_dir_default.json", "upload_method_default.json"}
    if (
        not safe_title
        or len(safe_title) > 180
        or safe_title.startswith(".")
        or safe_title.casefold() in {value.casefold() for value in reserved}
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in safe_title)
        or not _portable_component(safe_title)
    ):
        raise HTTPException(status_code=400, detail="Volume title is reserved or invalid")
    return safe_title


@app.post("/session/start")
async def session_start(
    title: str = Form("manga"),
    reuse_existing: str = Form("false"),
):
    return await _session_start_impl(title, reuse_existing)


async def _session_start_impl(title: str, reuse_existing: str):
    safe_title = _validate_volume_title(
        sanitize_filename(title) or f"manga_{uuid.uuid4().hex[:8]}"
    )
    async with _title_async_lock(safe_title):
        return await _session_start_body(title, reuse_existing, safe_title)


async def _session_start_body(title: str, reuse_existing: str, safe_title: str):
    """Create a new pipelined capture+OCR session."""
    do_reuse = _truthy(reuse_existing)

    if do_reuse:
        existing = await asyncio.to_thread(_find_session_by_safe_title, safe_title)
        if existing:
            with existing.lock:
                if existing.finalized or existing.finalizing or existing.cover_uploading or existing.ingesting or existing.page_reservations:
                    raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
                original_reuse_state = (
                    set(existing.pages_received),
                    set(existing.pages_ocr_done),
                    set(existing.pages_ocr_failed),
                    existing.message,
                )
                existing.pages_ocr_failed.clear()
                existing.ingesting = True
            try:
                _ensure_safe_work_volume(existing.vol_dir)
                existing.vol_dir.mkdir(parents=True, exist_ok=True)
                sync_task = asyncio.create_task(asyncio.to_thread(_sync_ocr_cache_state, existing))
                try:
                    await asyncio.shield(sync_task)
                except asyncio.CancelledError:
                    await _drain_task_even_if_cancelled(sync_task)
                    raise
                if not _persist_session(existing):
                    with _ocr_cv:
                        for index in range(len(_ocr_queue) - 1, -1, -1):
                            if _ocr_queue[index][0] == existing.session_id:
                                _ocr_queue.pop(index)
                    raise RuntimeError("could not persist reused session")
                _ensure_ocr_worker()
                snap = session_snapshot(existing)
                snap["reused"] = True
                snap["vol_dir"] = str(existing.vol_dir)
                return JSONResponse(snap)
            except BaseException:
                with _ocr_cv:
                    for index in range(len(_ocr_queue) - 1, -1, -1):
                        if _ocr_queue[index][0] == existing.session_id:
                            _ocr_queue.pop(index)
                    if not any(item[0] == existing.session_id for item in _ocr_queue):
                        try:
                            _ocr._ocr_session_order.remove(existing.session_id)
                        except ValueError:
                            pass
                with existing.lock:
                    (
                        existing.pages_received,
                        existing.pages_ocr_done,
                        existing.pages_ocr_failed,
                        existing.message,
                    ) = original_reuse_state
                raise
            finally:
                with existing.lock:
                    existing.ingesting = False

        with _session_create_lock:
            async with _volume_creation_lock(safe_title):
                # A different process may have restored/registered the same
                # title after the optimistic lookup. Never create a duplicate
                # live session against one volume.
                raced = await asyncio.to_thread(_find_session_by_safe_title, safe_title)
                if raced:
                    raise HTTPException(status_code=409, detail="A session for this title was created concurrently; retry reuse")
                # Reuse the on-disk volume even with no live session (keeps OCR cache).
                vol_dir = _ensure_safe_work_volume(WORK_DIR / safe_title)
                vol_dir.mkdir(parents=True, exist_ok=True)
    else:
        with _session_create_lock:
            async with _volume_creation_lock(safe_title):
                vol_dir = _ensure_safe_work_volume(WORK_DIR / safe_title)
                if vol_dir.exists():
                    vol_dir = _ensure_safe_work_volume(WORK_DIR / f"{safe_title}_{uuid.uuid4().hex[:6]}")
                    safe_title = vol_dir.name
                vol_dir.mkdir(parents=True, exist_ok=True)

    session_id = uuid.uuid4().hex[:12]
    session = Session(
        session_id=session_id,
        title=title,
        safe_title=safe_title,
        vol_dir=vol_dir,
        message="Ready — waiting for pages",
        ingesting=do_reuse,
    )
    # Register before syncing: the sync path may enqueue OCR immediately, and
    # the worker must be able to resolve the session from the live map.
    with _sessions_lock:
        _sessions[session_id] = session
    try:
        if do_reuse:
            sync_task = asyncio.create_task(asyncio.to_thread(_sync_ocr_cache_state, session))
            try:
                await asyncio.shield(sync_task)
            except asyncio.CancelledError:
                await _drain_task_even_if_cancelled(sync_task)
                raise
        if not _persist_session(session):
            raise RuntimeError("could not persist new session")
        _ensure_ocr_worker()
    except BaseException:
        with _ocr_cv:
            for index in range(len(_ocr_queue) - 1, -1, -1):
                if _ocr_queue[index][0] == session_id:
                    _ocr_queue.pop(index)
            if not any(item[0] == session_id for item in _ocr_queue):
                try:
                    _ocr._ocr_session_order.remove(session_id)
                except ValueError:
                    pass
        with _sessions_lock:
            if _sessions.get(session_id) is session:
                _sessions.pop(session_id, None)
        _delete_persisted_session(session_id)
        raise
    finally:
        with session.lock:
            session.ingesting = False

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
    return await _session_resume_impl(title, source_dir)


async def _session_resume_impl(title: str, source_dir: str):
    safe_title = _validate_volume_title(
        sanitize_filename(title) or f"manga_{uuid.uuid4().hex[:8]}"
    )
    async with _title_async_lock(safe_title):
        return await _session_resume_body(title, source_dir, safe_title)


async def _session_resume_body(title: str, source_dir: str, safe_title: str):
    """
    Resume OCR+MEGA for a volume that was partially scraped/OCR'd.

    - Reuses ~/mokuro-input/<title>/ when present
    - Optionally syncs newer/missing images from source_dir (e.g. manga_archives)
    - Skips pages that already have OCR JSON under _ocr/<title>/
    - Queues only the missing pages
    """
    created_here = False
    existing = await asyncio.to_thread(_find_session_by_safe_title, safe_title)
    if existing:
        with existing.lock:
            if existing.finalized or existing.finalizing or existing.cover_uploading or existing.ingesting or existing.page_reservations:
                raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
            existing.ingesting = True
        session = existing
        # An explicit resume is the operator's retry boundary; clear prior
        # terminal OCR failures so a repaired model/dependency can requeue them.
        with session.lock:
            session.pages_ocr_failed.clear()
        vol_dir = existing.vol_dir
    else:
        # Hold the cross-process file lock only for the short volume/session
        # admission step; source hashing/copying happens after it is released.
        with _session_create_lock:
            async with _volume_creation_lock(safe_title):
                raced = await asyncio.to_thread(_find_session_by_safe_title, safe_title)
                if raced:
                    raise HTTPException(status_code=409, detail="A session for this title was created concurrently; retry reuse")
                vol_dir = _ensure_safe_work_volume(WORK_DIR / safe_title)
                session_id = uuid.uuid4().hex[:12]
                session = Session(
                    session_id=session_id,
                    title=title,
                    safe_title=safe_title,
                    vol_dir=vol_dir,
                    message="Resuming…",
                    ingesting=True,
                )
                with _sessions_lock:
                    _sessions[session_id] = session
                created_here = True

    try:
        _ensure_safe_work_volume(vol_dir)
        vol_dir.mkdir(parents=True, exist_ok=True)
        synced = 0
        if source_dir.strip():
            src = _resolve_ingest_path(source_dir.strip())
            if not src.is_dir():
                raise HTTPException(status_code=400, detail=f"Not a directory: {src}")
            def _sync_one_image(img: Path) -> bool:
                with _SOURCE_MEMORY_SEMAPHORE:
                    return _sync_one_image_unlocked(img)

            def _sync_one_image_unlocked(img: Path) -> bool:
                nonlocal synced
                if img.is_symlink():
                    raise HTTPException(status_code=400, detail=f"Symlinked source is not allowed: {img.name}")
                if not img.is_file() or img.suffix.lower() not in IMAGE_EXTENSIONS:
                    return False
                safe_name = _validated_page_name(img.name, img.name)
                dest = vol_dir / safe_name
                if dest.is_symlink() or (dest.exists() and not dest.is_file()):
                    raise HTTPException(status_code=400, detail=f"Invalid destination page: {safe_name}")
                if _has_stem_collision(vol_dir, dest):
                    raise HTTPException(status_code=409, detail=f"Image filename stem already exists with another extension: {safe_name}")
                reserved = False
                try:
                    # Admission is short; the potentially large digest/copy runs
                    # without the global OCR condition held.
                    with _ocr_cv:
                        if (session.session_id, safe_name) in _ocr_processing:
                            raise HTTPException(status_code=409, detail=f"Page is currently being OCR'd: {safe_name}")
                        with session.lock:
                            if safe_name in session.page_reservations:
                                raise HTTPException(status_code=409, detail=f"Page is already being ingested: {safe_name}")
                            session.page_reservations.add(safe_name)
                            reserved = True
                            failed_page = safe_name in session.pages_ocr_failed
                    source_digest = _file_digest(img)
                    if source_digest is None:
                        raise HTTPException(status_code=400, detail=f"Could not read source image: {safe_name}")
                    existing_digest = _file_digest(dest) if dest.exists() else None
                    if dest.exists() and existing_digest is None:
                        raise HTTPException(status_code=400, detail=f"Could not read existing page: {safe_name}")
                    if dest.exists() and not failed_page and source_digest == existing_digest:
                        return False
                    if not _looks_like_image(_read_source_limited(img)):
                        raise HTTPException(status_code=400, detail=f"Source is not a recognized image: {safe_name}")
                    if dest.exists():
                        _copy_replace_no_follow(img, dest)
                    else:
                        _copy_no_follow(img, dest)
                    with _ocr_cv:
                        with session.lock:
                            try:
                                cache_volume = _mokuro_submodule("volume").Volume(vol_dir)
                                cache_path = ocr_json_path(cache_volume, safe_name)
                                if not _safe_ocr_cache_path(cache_path, cache_volume.path_ocr_cache, allow_missing=True):
                                    raise HTTPException(status_code=400, detail="OCR cache path is unsafe")
                                cache_path.unlink(missing_ok=True)
                            except HTTPException:
                                raise
                            except Exception:
                                pass
                            session.pages_ocr_done.discard(safe_name)
                            session.pages_ocr_failed.discard(safe_name)
                    return True
                finally:
                    if reserved:
                        with _ocr_cv:
                            with session.lock:
                                session.page_reservations.discard(safe_name)

            def _sync_source_directory() -> None:
                nonlocal synced
                for img in sorted(src.iterdir()):
                    if _sync_one_image(img):
                        synced += 1

            source_task = asyncio.create_task(asyncio.to_thread(_sync_source_directory))
            try:
                await asyncio.shield(source_task)
            except asyncio.CancelledError:
                await _drain_task_even_if_cancelled(source_task)
                raise

        sync_task = asyncio.create_task(asyncio.to_thread(_sync_ocr_cache_state, session))
        try:
            await asyncio.shield(sync_task)
        except asyncio.CancelledError:
            await _drain_task_even_if_cancelled(sync_task)
            raise

        def _queue_missing_images() -> tuple[int, int]:
            queued_count = 0
            cached_count = 0
            images = sorted(
                p.name
                for p in vol_dir.iterdir()
                if not p.is_symlink() and p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
                and _safe_component(p.name, extension=IMAGE_EXTENSIONS)
            )
            for name in images:
                with session.lock:
                    already = name in session.pages_ocr_done
                if already:
                    cached_count += 1
                    with session.lock:
                        session.pages_received.add(name)
                    continue
                _queue_page_ocr(session, name)
                queued_count += 1
            return queued_count, cached_count

        queue_task = asyncio.create_task(asyncio.to_thread(_queue_missing_images))
        try:
            queued, cached = await asyncio.shield(queue_task)
        except asyncio.CancelledError:
            await _drain_task_even_if_cancelled(queue_task)
            raise

        if not _persist_session(session):
            raise RuntimeError("could not persist resumed session")
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
    except BaseException:
        if created_here:
            with _ocr_cv:
                for index in range(len(_ocr_queue) - 1, -1, -1):
                    if _ocr_queue[index][0] == session.session_id:
                        _ocr_queue.pop(index)
                if not any(item[0] == session.session_id for item in _ocr_queue):
                    try:
                        _ocr._ocr_session_order.remove(session.session_id)
                    except ValueError:
                        pass
            with _sessions_lock:
                if _sessions.get(session.session_id) is session:
                    _sessions.pop(session.session_id, None)
            _delete_persisted_session(session.session_id)
        raise
    finally:
        with session.lock:
            session.ingesting = False

@app.post("/session/{session_id}/page")
async def session_page(
    session_id: str,
    page: UploadFile = File(...),
    filename: str = Form(...),
    page_num: int = Form(0),
):
    """Accept one captured page and queue OCR immediately (non-blocking)."""
    session = await asyncio.to_thread(_get_session, session_id)
    if session.finalized or session.finalizing or session.cover_uploading or session.ingesting:
        raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")

    # Accept only image destinations so every accepted page participates in
    # OCR, CBZ packaging, and cover selection consistently.
    safe_name = _validated_page_name(filename, f"page_{int(page_num):03d}.webp")

    _ensure_safe_work_volume(session.vol_dir)
    dest = session.vol_dir / safe_name
    if dest.is_symlink():
        raise HTTPException(status_code=400, detail="Destination filename must not be a symlink")
    # Reserve the page, then perform the potentially large atomic write in a
    # worker thread. Finalization and OCR selection only see the short commit
    # section after the bytes are durably staged.
    _ensure_ocr_worker()
    reserved = False
    try:
        with _ocr_cv:
            if (session.session_id, safe_name) in _ocr_processing:
                raise HTTPException(status_code=409, detail=f"Page is currently being OCR'd: {safe_name}")
            with session.lock:
                if session.finalized or session.finalizing or session.cover_uploading or session.ingesting:
                    raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
                if safe_name in session.page_reservations:
                    raise HTTPException(status_code=409, detail=f"Page is currently being ingested: {safe_name}")
                if _has_stem_collision(session.vol_dir, dest):
                    raise HTTPException(status_code=409, detail="Image filename stem already exists with another extension")
                if safe_name in session.pages_ocr_done:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Page already has completed OCR: {safe_name}",
                    )
                if dest.exists() and safe_name not in session.pages_ocr_failed and safe_name in session.pages_received:
                    raise HTTPException(status_code=409, detail=f"Page is already queued or present: {safe_name}")
                session.page_reservations.add(safe_name)
                reserved = True
        async with _upload_memory_slot():
            data = await _read_upload_limited(page)
            if not _looks_like_image(data):
                raise HTTPException(status_code=400, detail="Uploaded page is not a recognized image")
            await _run_thread_owned(_write_atomic_no_follow, dest, data)
        with _ocr_cv:
            with session.lock:
                if session.finalized or session.finalizing:
                    raise HTTPException(status_code=409, detail="Session is finalizing or already finalized")
                try:
                    cache_volume = _mokuro_submodule("volume").Volume(session.vol_dir)
                    cache_path = ocr_json_path(cache_volume, safe_name)
                    if not _safe_ocr_cache_path(cache_path, cache_volume.path_ocr_cache, allow_missing=True):
                        raise HTTPException(status_code=400, detail="OCR cache path is unsafe")
                    cache_path.unlink(missing_ok=True)
                except HTTPException:
                    raise
                except Exception:
                    pass
                session.pages_ocr_done.discard(safe_name)
                session.pages_ocr_failed.discard(safe_name)
                snap = _queue_page_ocr(session, safe_name, _ocr_cv_held=True)
    finally:
        if reserved:
            with _ocr_cv:
                with session.lock:
                    session.page_reservations.discard(safe_name)
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
    session = await asyncio.to_thread(_get_session, session_id)
    if session.finalized or session.finalizing or session.cover_uploading or session.ingesting:
        raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")

    raw_source = Path(path).expanduser()
    if _is_link_like(raw_source):
        raise HTTPException(status_code=400, detail="Symlinked source is not allowed")
    src = _resolve_ingest_path(path)
    if not src.is_file() or src.is_symlink():
        raise HTTPException(status_code=400, detail=f"Not a file: {src}")
    if src.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {src.suffix}")
    safe_name = _validated_page_name(filename, f"page_{int(page_num):03d}{src.suffix.lower()}")

    async with _upload_memory_slot():
        if not _looks_like_image(await asyncio.to_thread(_read_source_limited, src)):
            raise HTTPException(status_code=400, detail="Source file is not a recognized image")
    # Volume dir can disappear after MEGA cleanup while the session is still live.
    _ensure_safe_work_volume(session.vol_dir)
    dest = session.vol_dir / safe_name
    if dest.is_symlink():
        raise HTTPException(status_code=400, detail="Destination filename must not be a symlink")
    # Reserve the filename under the scheduler lock, then copy outside it. A
    # 100 MiB local-page copy must not stall OCR/finalization for other volumes.
    _ensure_ocr_worker()
    reserved = False
    copied = False
    try:
        with _ocr_cv:
            if (session.session_id, safe_name) in _ocr_processing:
                raise HTTPException(status_code=409, detail=f"Page is currently being OCR'd: {safe_name}")
            with session.lock:
                if session.finalized or session.finalizing or session.cover_uploading or session.ingesting:
                    raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
                if safe_name in session.page_reservations:
                    raise HTTPException(status_code=409, detail=f"Page is already being ingested: {safe_name}")
                _ensure_safe_work_volume(session.vol_dir)
                session.vol_dir.mkdir(parents=True, exist_ok=True)
                if _has_stem_collision(session.vol_dir, dest):
                    raise HTTPException(status_code=409, detail="Image filename stem already exists with another extension")
                if dest.exists() and safe_name in session.pages_received and safe_name not in session.pages_ocr_failed:
                    raise HTTPException(status_code=409, detail=f"Page is already queued or present: {safe_name}")
                session.page_reservations.add(safe_name)
                reserved = True
        if dest.is_symlink():
            raise HTTPException(status_code=400, detail="Destination filename must not be a symlink")
        if dest.exists():
            await _run_thread_owned(_copy_replace_no_follow, src, dest)
        else:
            await _run_thread_owned(_copy_no_follow, src, dest)
        copied = True
        with _ocr_cv:
            with session.lock:
                if session.finalized or session.finalizing:
                    raise HTTPException(status_code=409, detail="Session is finalizing or already finalized")
                try:
                    cache_volume = _mokuro_submodule("volume").Volume(session.vol_dir)
                    cache_path = ocr_json_path(cache_volume, safe_name)
                    if not _safe_ocr_cache_path(cache_path, cache_volume.path_ocr_cache, allow_missing=True):
                        raise HTTPException(status_code=400, detail="OCR cache path is unsafe")
                    cache_path.unlink(missing_ok=True)
                except HTTPException:
                    raise
                except Exception:
                    pass
                session.pages_ocr_done.discard(safe_name)
                session.pages_ocr_failed.discard(safe_name)
                snap = _queue_page_ocr(session, safe_name, _ocr_cv_held=True)
    finally:
        if reserved:
            with _ocr_cv:
                with session.lock:
                    session.page_reservations.discard(safe_name)
    snap["filename"] = safe_name
    snap["page_num"] = page_num
    snap["source"] = str(src)
    snap["copied"] = copied
    return JSONResponse(snap)

@app.post("/session/{session_id}/cover")
async def session_cover(
    session_id: str,
    cover: UploadFile = File(...),
    upload_method: str = Form(""),
    local_dir: str = Form(""),
):
    """Early cover upload: store/upload <title>.webp (the cover — a copy of
    the volume's first page) *before* OCR finishes, so the destination
    receives something immediately and the client can show upload progress
    early. Records session.cover_uploaded so a later finalize skips
    re-uploading the cover to the same destination. Returns JSON (not NDJSON):
    {ok, method, file, remote_path|path, url|None, size}.
    """
    session = await asyncio.to_thread(_get_session, session_id)
    if session.finalized or session.finalizing or session.cover_uploading or session.ingesting:
        raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
    try:
        method = await asyncio.to_thread(resolve_upload_method, str(upload_method).strip() or None)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    with session.lock:
        if session.finalized or session.finalizing or session.cover_uploading or session.ingesting:
            raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
        session.cover_uploading = True
    upload_slot = _upload_memory_semaphore()
    try:
        await upload_slot.acquire()
    except BaseException:
        with session.lock:
            session.cover_uploading = False
        raise
    slot_held = True

    def _release_cover_slot() -> None:
        nonlocal slot_held
        if slot_held:
            slot_held = False
            upload_slot.release()

    try:
        data = await _read_upload_limited(cover)
        if not _looks_like_image(data):
            raise HTTPException(status_code=400, detail="Cover is not a recognized image")
        data = await asyncio.to_thread(_normalize_cover_bytes, data)
    except BaseException:
        _release_cover_slot()
        with session.lock:
            session.cover_uploading = False
        raise
    try:
        file_base = session.safe_title
        remote_name = f"{file_base}.webp"
        series = _validated_series(series_title_from_volume(session.safe_title))
        size = len(data)
    except BaseException:
        _release_cover_slot()
        with session.lock:
            session.cover_uploading = False
        raise

    if method == "local":
        try:
            base = await asyncio.to_thread(_effective_local_output_base, local_dir) or OUTPUT_DIR
            dest_dir = base / series
        except BaseException:
            _release_cover_slot()
            with session.lock:
                session.cover_uploading = False
            raise
        try:
            with session.lock:
                if session.finalized or session.finalizing or session.ingesting:
                    raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
            _set_activity("uploading", f"{session.safe_title} cover", session_id)
        except BaseException:
            _release_cover_slot()
            with session.lock:
                session.cover_uploading = False
            raise
        try:
            if dest_dir.is_symlink():
                raise HTTPException(status_code=400, detail="Cover output directory is unsafe")
            dest_dir = dest_dir.resolve()
            if (
                _paths_overlap(dest_dir, session.vol_dir)
                or _paths_overlap(dest_dir, WORK_DIR)
            ):
                raise HTTPException(status_code=400, detail="Cover output directory must not overlap working state")
            if dest_dir.is_symlink() or (dest_dir.exists() and not dest_dir.is_dir()):
                raise HTTPException(status_code=400, detail="Cover output directory is unsafe")
            dest_dir.mkdir(parents=True, exist_ok=True)
            _ensure_safe_work_volume(session.vol_dir)
            dest = dest_dir / remote_name
            if dest.is_symlink():
                raise HTTPException(status_code=400, detail="Cover destination must not be a symlink")
            _ensure_safe_work_volume(session.vol_dir)
            await _run_thread_owned(_write_atomic_no_follow, dest, data)
            digest = await asyncio.to_thread(_file_digest, dest)
            if digest is None:
                raise HTTPException(status_code=500, detail="Could not verify written cover")
            rec = {"method": "local", "file": remote_name, "path": str(dest), "remote_path": None, "size": size, "sha256": digest}
            with session.lock:
                session.cover_uploaded = rec
            if not _persist_session(session):
                with session.lock:
                    session.cover_uploaded = None
                raise HTTPException(status_code=500, detail="Could not persist cover metadata")
            return JSONResponse({"ok": True, **rec})
        finally:
            _release_cover_slot()
            _clear_activity(session_id)
            with session.lock:
                session.cover_uploading = False

    # Remote upload: reuse upload_file so the URL/session-state plumbing is
    # identical to finalize. The temp file uses a .uploadpart suffix so it can
    # never be mistaken for a page by OCR or finalize's image scan.
    remote_dir = ""
    tmp: Optional[Path] = None

    try:
        with session.lock:
            if session.finalized or session.finalizing or session.ingesting:
                raise HTTPException(status_code=400, detail="Session is finalizing or already finalized")
        _set_activity("uploading", f"{session.safe_title} cover", session_id)
    except BaseException:
        _release_cover_slot()
        with session.lock:
            session.cover_uploading = False
        raise
    try:
        remote_dir = f"{method_root(method)}/{series}"
        tmp = session.vol_dir / f"{remote_name}.uploadpart"
        _ensure_safe_work_volume(session.vol_dir)
        session.vol_dir.mkdir(parents=True, exist_ok=True)
        _ensure_safe_work_volume(session.vol_dir)
        await _run_thread_owned(_write_atomic_no_follow, tmp, data)

        def _prog(bytes_done, total_bytes, speed_bps):
            pct = round(bytes_done * 100.0 / total_bytes, 2) if total_bytes else 100.0
            speed_human = (
                f"{speed_bps / 1048576:.2f} MiB/s" if speed_bps >= 1048576
                else f"{speed_bps / 1024:.1f} KiB/s" if speed_bps else "—"
            )
            _set_upload_state(
                session, active=True, method=method, file=remote_name,
                current_bytes=bytes_done, total_bytes=total_bytes, percent=pct,
                speed_bps=speed_bps, speed_human=speed_human, remote_path=remote_dir,
            )

        upload_task = asyncio.create_task(asyncio.to_thread(
            upload_file, method, tmp, remote_dir, _prog, "overwrite"
        ))
        with session.lock:
            session.finalize_upload_task = upload_task
        ok, err, url = await asyncio.shield(upload_task)
        if not ok:
            raise HTTPException(status_code=502, detail=f"cover upload failed: {_safe_provider_error(err or 'unknown')}")
        digest = await asyncio.to_thread(_file_digest, tmp)
        if digest is None:
            raise HTTPException(status_code=500, detail="Could not verify uploaded cover")
        rec = {
            "method": method, "file": remote_name,
            "remote_path": remote_dir, "url": _safe_provider_url(url), "size": size, "sha256": digest,
        }
        with session.lock:
            session.cover_uploaded = rec
        if not _persist_session(session):
            with session.lock:
                session.cover_uploaded = None
            raise HTTPException(status_code=500, detail="Could not persist cover metadata")
        return JSONResponse({"ok": True, **rec})
    finally:
        _release_cover_slot()
        with session.lock:
            cover_task = session.finalize_upload_task
            session.finalize_upload_task = None
        await _drain_task_even_if_cancelled(cover_task)
        _set_upload_state(session, active=False, method=method, file=remote_name)
        _clear_activity(session_id)
        with session.lock:
            session.cover_uploading = False
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass

@app.get("/sessions")
async def list_sessions():
    """Active pipelined sessions (useful when several capture clients run at once)."""
    def _snapshot_all():
        with _sessions_lock:
            return [session_snapshot(s) for s in _sessions.values()]
    snaps = await asyncio.to_thread(_snapshot_all)
    return JSONResponse({"sessions": snaps, "count": len(snaps)})

@app.get("/session/{session_id}/status")
async def session_status(session_id: str):
    # Restored-session reconciliation may hash a large volume; keep status
    # polling responsive to other sessions.
    session = await asyncio.to_thread(_get_session, session_id)
    return JSONResponse(session_snapshot(session))

def _canonical_uuid(value: object) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _validate_mokuro_artifact(
    path: Path,
    expected_pages: int,
    expected_title: str,
    expected_volume: str,
    expected_title_uuid: Optional[str] = None,
    expected_volume_uuid: Optional[str] = None,
) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("generated .mokuro is missing or unsafe")
    data = _mokuro_submodule("utils").load_json(path)
    if not isinstance(data, dict):
        raise RuntimeError("generated .mokuro is not an object")
    pages = data.get("pages")
    if not isinstance(pages, list) or len(pages) != expected_pages:
        actual = len(pages) if isinstance(pages, list) else "invalid"
        raise RuntimeError(f"generated .mokuro page coverage is {actual}, expected {expected_pages}")
    if any(not isinstance(page, dict) or not isinstance(page.get("blocks"), list) for page in pages):
        raise RuntimeError("generated .mokuro contains an invalid page")
    if data.get("title") != expected_title or data.get("volume") != expected_volume:
        raise RuntimeError("generated .mokuro title/volume identity mismatch")
    if expected_title_uuid is not None and data.get("title_uuid") != expected_title_uuid:
        raise RuntimeError("generated .mokuro title UUID mismatch")
    if expected_volume_uuid is not None and data.get("volume_uuid") != expected_volume_uuid:
        raise RuntimeError("generated .mokuro volume UUID mismatch")


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
    session = await asyncio.to_thread(_get_session, session_id)
    do_delete = _truthy(delete_after_upload)
    raw_target = str(upload_method).strip() or str(upload_to_mega).strip() or None
    try:
        method = await asyncio.to_thread(resolve_upload_method, raw_target)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finalize_lease = _acquire_finalize_lock(session_id)
    if finalize_lease is None:
        raise HTTPException(status_code=409, detail="Session finalization is already in progress")
    try:
        with _ocr_cv:
            with session.lock:
                if session.finalized or session.finalizing or session.cover_uploading or session.ingesting or session.page_reservations:
                    raise HTTPException(status_code=409, detail="Session finalization is already in progress")
                session.finalizing = True
                session.finalize_lock_handle = finalize_lease
    except BaseException:
        _release_finalize_lock(finalize_lease)
        raise
    _set_activity("ocr", f"{session.safe_title} finalization queued", session_id)
    try:
        # Sticky choices are committed only after this request owns the
        # finalization lease; a losing concurrent finalize must not change the
        # process-wide defaults.
        if raw_target is not None:
            await asyncio.to_thread(_remember_upload_method, method)
        local_output_base = (
            await asyncio.to_thread(_effective_local_output_base, local_dir)
            if method == "local"
            else None
        )
    except BaseException:
        with session.lock:
            session.finalizing = False
        _release_finalize_lock(finalize_lease)
        _clear_activity(session_id)
        raise

    async def generate():
        try:
            snap = session_snapshot(session)
            _set_activity("ocr", f"waiting for OCR: {snap['pages_ocr_pending']} pending", session_id)
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
            with session.lock:
                session.finalize_wait_task = wait_task
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
            await asyncio.shield(wait_task)

            snap = session_snapshot(session)
            if snap["pages_ocr_failed"]:
                yield ndjson(
                    "error",
                    f"OCR failed for {snap['pages_ocr_failed']} page(s); refusing to assemble an incomplete .mokuro",
                    status="ocr_failed",
                    pages_ocr_failed=snap["pages_ocr_failed"],
                    pages=snap["pages_received"],
                )
                return
            try:
                volume_for_cache = _mokuro_submodule("volume").Volume(session.vol_dir)
            except Exception as error:
                yield ndjson("error", f"Could not inspect OCR cache: {error}")
                return
            def _find_missing_cache() -> list[str]:
                missing: list[str] = []
                for page in sorted(session.vol_dir.iterdir(), key=lambda p: p.name):
                    if page.is_symlink() or not page.is_file() or page.suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    json_path = ocr_json_path(volume_for_cache, page.name)
                    if not (
                        _safe_ocr_cache_path(json_path, volume_for_cache.path_ocr_cache)
                        and _valid_ocr_cache(json_path, page)
                    ):
                        missing.append(page.name)
                return missing

            missing_cache = await asyncio.to_thread(_find_missing_cache)
            if missing_cache:
                yield ndjson(
                    "error",
                    "OCR cache is missing or corrupt; refusing to assemble an incomplete .mokuro",
                    status="ocr_cache_invalid",
                    pages=missing_cache,
                )
                return
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
                with _assemble_session_lock(session.session_id), _ocr._assemble_lock:
                    # Never accept a stale artifact if the generator silently
                    # skips output for this run.
                    sibling = session.vol_dir.parent / f"{session.vol_dir.name}.mokuro"
                    # UUIDs are deterministic from the scoped title/volume;
                    # never parse a potentially huge stale artifact just to
                    # recover identity fields.
                    # The upstream generator discovers images recursively, while
                    # the bridge's page/OCR state is intentionally direct-child
                    # only. Reject nested images and every reparse/symlink before
                    # generation so a manually planted tree cannot expand scope.
                    for current, directories, files in os.walk(
                        session.vol_dir, topdown=True, followlinks=False
                    ):
                        current_path = Path(current)
                        for name in directories:
                            child = current_path / name
                            if _is_link_like(child):
                                raise RuntimeError("unsafe nested volume directory")
                        for name in files:
                            child = current_path / name
                            if _is_link_like(child):
                                raise RuntimeError("unsafe nested volume file")
                            if current_path != session.vol_dir and child.suffix.lower() in IMAGE_EXTENSIONS:
                                raise RuntimeError("nested images are not supported by bridge ingest")
                    backups: list[tuple[Path, Path]] = []
                    try:
                        old_artifacts = [sibling, *session.vol_dir.glob("*.mokuro")]
                        if any(old.is_symlink() for old in old_artifacts):
                            raise RuntimeError("unsafe symlink in artifact output paths")
                        for old in old_artifacts:
                            if old.is_file():
                                backup = old.with_name(
                                    f".{old.name}.backup-{uuid.uuid4().hex}"
                                )
                                os.replace(old, backup)
                                backups.append((old, backup))
                        volume = Volume(session.vol_dir)
                        volume.title = Title(session.vol_dir.parent)
                        # Title.set_uuid() scans every sibling artifact under WORK_DIR;
                        # set the scoped title identity directly so one volume cannot
                        # rewrite unrelated title UUIDs.
                        series_name = _validated_series(series_title_from_volume(session.safe_title))
                        volume.title.name = series_name
                        title_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mokuro-bridge:title:{series_name}"))
                        volume_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mokuro-bridge:volume:{session.safe_title}"))
                        volume.title._uuid = title_uuid
                        volume.uuid = volume_uuid
                        # Do not silently drop pages with missing/corrupt OCR JSON;
                        # an incomplete .mokuro is worse than a visible failure.
                        MokuroGenerator.generate_mokuro_file(volume, False)
                        generated = find_mokuro_output(session.vol_dir)
                        if not generated:
                            raise RuntimeError("generator produced no .mokuro artifact")
                        expected_pages = sum(
                            1
                            for page in session.vol_dir.iterdir()
                            if page.is_file() and not page.is_symlink()
                            and page.suffix.lower() in IMAGE_EXTENSIONS
                        )
                        _validate_mokuro_artifact(
                            generated, expected_pages, series_name, session.safe_title,
                            title_uuid, volume_uuid
                        )
                    except BaseException:
                        backed_up_destinations = {destination for destination, _backup in backups}
                        for produced in [sibling, *session.vol_dir.glob("*.mokuro")]:
                            if produced not in backed_up_destinations and produced.is_file() and not produced.is_symlink():
                                try:
                                    produced.unlink()
                                except OSError:
                                    pass
                        for destination, backup in reversed(backups):
                            try:
                                if destination.exists() and not destination.is_symlink():
                                    destination.unlink()
                                if backup.exists():
                                    os.replace(backup, destination)
                            except OSError:
                                pass
                        raise
                    else:
                        for _destination, backup in backups:
                            try:
                                backup.unlink(missing_ok=True)
                            except OSError:
                                pass

            assembly_task = asyncio.create_task(
                asyncio.to_thread(_assemble), name=f"assemble-{session_id}"
            )
            with session.lock:
                session.finalize_assembly_task = assembly_task
            try:
                await asyncio.shield(assembly_task)
            finally:
                with session.lock:
                    if assembly_task.done() and session.finalize_assembly_task is assembly_task:
                        session.finalize_assembly_task = None

            mokuro_file = find_mokuro_output(session.vol_dir)
            if not mokuro_file:
                yield ndjson(
                    "error",
                    f"No .mokuro after assemble (expected {session.vol_dir.parent / (session.vol_dir.name + '.mokuro')})",
                )
                return
            yield ndjson("pack", "Packaging CBZ + cover…")
            await asyncio.sleep(0)

            image_files = _natural_sorted_paths([
                p
                for p in session.vol_dir.iterdir()
                if p.suffix.lower() in IMAGE_EXTENSIONS and p.is_file() and not p.is_symlink()
            ])
            if not image_files:
                yield ndjson("error", "No images in volume directory")
                return
            stems: dict[str, str] = {}
            collisions = []
            for image in image_files:
                key = image.stem.casefold()
                if key in stems:
                    collisions.extend((stems[key], image.name))
                else:
                    stems[key] = image.name
            if collisions:
                yield ndjson(
                    "error",
                    "Image filenames collide by stem; refusing to assemble",
                    status="page_name_collision",
                    pages=sorted(set(collisions)),
                )
                return
            with session.lock:
                received_names = set(session.pages_received)
            image_names = {p.name for p in image_files}
            if image_names != received_names:
                yield ndjson(
                    "error",
                    "Image set does not match the captured page set; refusing partial output",
                    status="page_set_mismatch",
                    missing=sorted(received_names - image_names),
                    unexpected=sorted(image_names - received_names),
                )
                return
            try:
                await asyncio.to_thread(
                    _validate_mokuro_artifact,
                    mokuro_file,
                    len(image_files),
                    _validated_series(series_title_from_volume(session.safe_title)),
                    session.safe_title,
                )
            except Exception as error:
                yield ndjson("error", f"Generated .mokuro failed validation: {error}", status="artifact_invalid")
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
            series_dir_name = _validated_series(series_title_from_volume(session.safe_title))
            file_base = session.safe_title
            staging = (
                session.vol_dir / "_mega_upload"
                if method != "local"
                else (local_output_base or OUTPUT_DIR) / series_dir_name
            )
            if method == "local" and (
                _paths_overlap(staging, session.vol_dir)
                or _paths_overlap(staging, WORK_DIR)
            ):
                yield ndjson(
                    "error",
                    "Local output directory must not overlap the working volume",
                    status="invalid_local_output",
                )
                return
            if staging.is_symlink() or (staging.exists() and not staging.is_dir()):
                yield ndjson("error", "Final output staging path is not a safe directory", status="invalid_output_path")
                return
            staging.mkdir(parents=True, exist_ok=True)
            # Files keep the full volume title; folder is the shared series name.
            titled_mokuro = staging / f"{file_base}.mokuro"
            titled_cbz = staging / f"{file_base}.cbz"
            titled_cover = staging / f"{file_base}.webp"
            def _package_artifacts():
                # Build the complete trio in a private directory, then publish
                # all three with rollback. A cover conversion failure must not
                # leave a new .mokuro next to an old .cbz/.webp.
                pack_tmp = Path(tempfile.mkdtemp(prefix=f".{file_base}.pack-", dir=str(staging)))
                try:
                    try:
                        os.chmod(pack_tmp, 0o700)
                    except OSError:
                        pass
                    tmp_mokuro = pack_tmp / titled_mokuro.name
                    tmp_cbz = pack_tmp / titled_cbz.name
                    tmp_cover = pack_tmp / titled_cover.name
                    _copy_replace_no_follow(mokuro_file, tmp_mokuro)
                    _zip_replace_no_follow(tmp_cbz, image_files)
                    reuse_cover = False
                    if method == "local":
                        with session.lock:
                            cover_meta = (
                                dict(session.cover_uploaded)
                                if isinstance(session.cover_uploaded, dict)
                                else None
                            )
                        if (
                            cover_meta
                            and cover_meta.get("method") == "local"
                            and cover_meta.get("file") == f"{file_base}.webp"
                            and cover_meta.get("path") == str(titled_cover)
                            and titled_cover.is_file()
                            and not titled_cover.is_symlink()
                            and cover_meta.get("sha256") == _file_digest(titled_cover)
                        ):
                            _copy_replace_no_follow(titled_cover, tmp_cover)
                            reuse_cover = True
                    if not reuse_cover:
                        webp_pages = [p for p in image_files if p.suffix.lower() == ".webp"]
                        _cover_replace(webp_pages[0] if webp_pages else image_files[0], tmp_cover)
                    destinations = (titled_mokuro, titled_cbz, titled_cover)
                    staged = (tmp_mokuro, tmp_cbz, tmp_cover)
                    if any(
                        not source.is_file() or source.is_symlink() or source.stat().st_size <= 0
                        for source in staged
                    ):
                        raise RuntimeError("packaging produced an incomplete artifact")
                    if any(
                        dest.is_symlink()
                        or (dest.exists() and (not dest.is_file() or dest.is_dir()))
                        for dest in destinations
                    ):
                        raise RuntimeError("unsafe or non-regular packaging output path")
                    backups: list[tuple[Path, Path]] = []
                    published: list[Path] = []
                    try:
                        for destination in destinations:
                            if destination.exists():
                                backup = destination.with_name(
                                    f".{destination.name}.backup-{uuid.uuid4().hex}"
                                )
                                os.replace(destination, backup)
                                backups.append((destination, backup))
                        for source, destination in zip(staged, destinations):
                            os.replace(source, destination)
                            published.append(destination)
                    except BaseException:
                        for destination in reversed(published):
                            destination.unlink(missing_ok=True)
                        for destination, backup in reversed(backups):
                            if backup.exists():
                                os.replace(backup, destination)
                        raise
                    else:
                        for _destination, backup in backups:
                            backup.unlink(missing_ok=True)
                finally:
                    shutil.rmtree(pack_tmp, ignore_errors=True)

            pack_task = asyncio.create_task(
                asyncio.to_thread(_package_artifacts), name=f"package-{session_id}"
            )
            with session.lock:
                session.finalize_pack_task = pack_task
            try:
                await asyncio.shield(pack_task)
            finally:
                with session.lock:
                    if pack_task.done() and session.finalize_pack_task is pack_task:
                        session.finalize_pack_task = None

            if method == "local":
                _log.info(
                    "upload",
                    f"{session.safe_title}: saved locally → {staging}",
                )
                yield ndjson(
                    "pack",
                    f"Saved local pack → {staging}",
                    output_dir=str(staging),
                    series=series_dir_name,
                )
                await asyncio.sleep(0)

            series = _validated_series(series_title_from_volume(session.safe_title))
            remote_dir = "" if method == "local" else f"{method_root(method)}/{series}"
            target_label = method_label(method)
            upload_results = []
            all_success = True

            if method != "local":
                _set_activity("uploading", f"{target_label} → {remote_dir}", session_id)
                # Announce every file that will upload (with its size) before
                # any bytes flow, so clients can pre-size the overall progress
                # bar instead of discovering totals one file at a time (which
                # makes a finished file look like 100% until the next total
                # arrives and the bar has to step back down).
                # Cover .webp first: it is tiny, so it completes almost instantly,
                # giving clients immediate visible progress and a usable file
                # URL (and failing fast on a broken destination) before the big
                # .cbz/.mokuro transfers start.
                file_plan = [
                    {"file": f"{file_base}.webp", "total_bytes": titled_cover.stat().st_size},
                    {"file": f"{file_base}.cbz", "total_bytes": titled_cbz.stat().st_size},
                    {"file": f"{file_base}.mokuro", "total_bytes": titled_mokuro.stat().st_size},
                ]
                yield ndjson(
                    "upload",
                    f"Uploading to {target_label}… ({series_dir_name}/)",
                    series=series_dir_name,
                    remote_path=remote_dir,
                    mega_path=remote_dir if method_provider(method) == "mega" else None,
                    method=method,
                    files=file_plan,
                )
                await asyncio.sleep(0)

                # Thread-safe queue the upload worker fills with NDJSON
                # progress frames; drained and yielded live by this generator
                # so clients see real-time upload progress, not a burst after
                # every file finishes.
                ev_queue = _queue.Queue()

                def _upload_batch():
                    results = []
                    items = [
                        (titled_cover, f"{file_base}.webp"),
                        (titled_cbz, f"{file_base}.cbz"),
                        (titled_mokuro, f"{file_base}.mokuro"),
                    ]
                    # The cover may already have been pushed early via
                    # POST /session/{id}/cover — don't upload it again.
                    cover_pre = None
                    with session.lock:
                        if (
                            session.cover_uploaded
                            and session.cover_uploaded.get("method") == method
                            and session.cover_uploaded.get("file") == f"{file_base}.webp"
                            and session.cover_uploaded.get("remote_path") == remote_dir
                            and titled_cover.is_file()
                            and not titled_cover.is_symlink()
                            and session.cover_uploaded.get("sha256") == _file_digest(titled_cover)
                        ):
                            cover_pre = dict(session.cover_uploaded)
                    if cover_pre:
                        # Keep the cover in the batch, but ask the provider to
                        # skip an existing object. If it was deleted remotely,
                        # `skip` falls through to a fresh upload; unlike blindly
                        # trusting persisted metadata this verifies presence.
                        cover_name = f"{file_base}.webp"
                    else:
                        cover_name = None
                    for local_path, remote_name in items:
                        start = time.monotonic()
                        last_progress = {"bytes": 0, "speed": 0, "emitted": 0.0}
                        total = local_path.stat().st_size
                        _set_upload_state(
                            session, active=True, method=method, file=remote_name,
                            current_bytes=0, total_bytes=total, percent=0.0,
                            remote_path=remote_dir,
                        )

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
                                ev_queue.put(
                                    _upload_progress_event(
                                        _name, bytes_done, total_bytes, speed_bps,
                                        remote_dir, method,
                                    )
                                )
                                # Console: in-place upload progress.
                                pct = (bytes_done * 100.0 / total_bytes) if total_bytes else 0.0
                                _log.progress(
                                    "upload",
                                    f"Uploading {_name}: {pct:.0f}% "
                                    f"({bytes_done/1048576:.1f}/{total_bytes/1048576:.1f} MiB)",
                                )
                                # Live session state for GET /session/{id}/status.
                                speed_human = (
                                    f"{speed_bps / 1024 / 1024:.2f} MiB/s"
                                    if speed_bps >= 1024**2
                                    else f"{speed_bps / 1024:.1f} KiB/s"
                                    if speed_bps else "—"
                                )
                                _set_upload_state(
                                    session, active=True, method=method,
                                    file=_name, percent=pct,
                                    current_bytes=bytes_done, total_bytes=total_bytes,
                                    speed_bps=speed_bps, speed_human=speed_human,
                                    remote_path=remote_dir,
                                )

                        try:
                            item_overwrite = "skip" if remote_name == cover_name else overwrite
                            ok, err, url = upload_file(method, local_path, remote_dir, _on_progress, item_overwrite)
                        except Exception as upload_error:
                            ok, err, url = False, str(upload_error)[:1000], None
                        safe_url = _safe_provider_url(url) if ok else None
                        duration_s = round(time.monotonic() - start, 2)
                        results.append(
                            {
                                "file": remote_name,
                                "size": local_path.stat().st_size,
                                "success": ok,
                                "stderr": _safe_provider_error(err) if not ok else None,
                                "url": safe_url,
                                "duration_s": duration_s,
                                "early": remote_name == cover_name,
                            }
                        )
                        # Per-file end state: success keeps the URL for the
                        # "Open stored file" action; failure marks the error.
                        _set_upload_state(
                            session, active=False, method=method, file=remote_name,
                            percent=100.0 if ok else 0.0,
                            current_bytes=total if ok else 0, total_bytes=total,
                            speed_bps=0, speed_human="—", remote_path=remote_dir,
                            url=safe_url,
                            error=None if ok else _safe_provider_error(err or "unknown"),
                        )
                    return results

                upload_task = asyncio.create_task(asyncio.to_thread(_upload_batch))
                with session.lock:
                    session.finalize_upload_task = upload_task
                while not upload_task.done():
                    while True:
                        try:
                            ev = ev_queue.get_nowait()
                        except _queue.Empty:
                            break
                        yield ev
                        await asyncio.sleep(0)
                    await asyncio.sleep(0.02)
                upload_results = await asyncio.shield(upload_task)
                # Drain any frames emitted right at completion.
                while True:
                    try:
                        yield ev_queue.get_nowait()
                        await asyncio.sleep(0)
                    except _queue.Empty:
                        break
                for r in upload_results:
                    if r["success"]:
                        _log.info(
                            "upload",
                            f"{session.safe_title}: uploaded {r['file']}"
                            + (f" → {r.get('url')}" if r.get("url") else ""),
                        )
                        yield ndjson(
                            "upload",
                            f"Uploaded {r['file']}",
                            file=r["file"],
                            method=method,
                            url=r.get("url"),
                        )
                    else:
                        _log.error(
                            "upload",
                            f"{session.safe_title}: {r['file']} upload failed: "
                            f"{r.get('stderr') or 'unknown'}",
                        )
                        yield ndjson(
                            "upload",
                            f"Upload failed for {r['file']}: {r.get('stderr') or 'unknown'}",
                            file=r["file"],
                            method=method,
                        )
                    await asyncio.sleep(0)

                all_success = all(r["success"] for r in upload_results)
                if all_success:
                    _log.info(
                        "upload",
                        f"{session.safe_title}: upload complete ({target_label})",
                    )
                # Terminal upload state: all files done. The last per-file
                # entry already carries the final URL; mark the batch finished.
                _set_upload_state(
                    session, active=False, method=method,
                    file=upload_results[-1]["file"] if upload_results else None,
                    percent=100.0 if all_success else 0.0,
                    current_bytes=(
                        sum(r["size"] for r in upload_results if r["success"])
                        if all_success else 0
                    ),
                    total_bytes=sum(r["size"] for r in upload_results),
                    speed_bps=0, speed_human="—",
                    remote_path=remote_dir,
                    url=upload_results[-1].get("url") if upload_results and all_success else None,
                    error=None if all_success else "one or more files failed",
                )

            if method != "local" and not all_success:
                failed = [r for r in upload_results if not r["success"]]
                yield ndjson(
                    "done",
                    f"{target_label} upload partially completed: "
                    + "; ".join(f"{r['file']}: {r.get('stderr') or 'unknown'}" for r in failed),
                    status="partial_upload",
                    title=session.safe_title,
                    pages=len(image_files),
                    remote_path=remote_dir,
                    mega_path=remote_dir if method_provider(method) == "mega" else None,
                    uploads=upload_results,
                    method=method,
                )
                return

            if do_delete and all_success:
                yield ndjson("cleanup", "Cleaning up working files…")
                await asyncio.sleep(0)
                cleanup_task = asyncio.create_task(
                    asyncio.to_thread(cleanup_volume_artifacts, session.vol_dir),
                    name=f"cleanup-{session_id}",
                )
                with session.lock:
                    session.finalize_cleanup_task = cleanup_task
                try:
                    await asyncio.shield(cleanup_task)
                finally:
                    with session.lock:
                        if cleanup_task.done() and session.finalize_cleanup_task is cleanup_task:
                            session.finalize_cleanup_task = None

            # Mark the session terminal only after cleanup has completed. A
            # crash before this point leaves a restartable, non-finalized
            # record rather than an inaccessible finalized ghost.
            with session.lock:
                session.finalized = True
            if not _persist_session(session):
                with session.lock:
                    session.finalized = False
                yield ndjson("error", "Finalization completed output but could not persist session state", status="persistence_error")
                return

            with _sessions_lock:
                _sessions.pop(session_id, None)
            _delete_persisted_session(session_id)

            done_msg = (
                f"Done! {len(image_files)} pages → {target_label} {remote_dir}/"
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
                            "url": r.get("url") if r["success"] else None,
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
                mega_path=remote_dir if method_provider(method) == "mega" else None,
                output_dir=str(staging) if method == "local" else None,
                staging=str(staging),
                uploads=uploads_summary if method != "local" else upload_results,
                reader_url="https://reader.mokuro.app/" if method != "local" else None,
                method=method,
            )
            _clear_activity(session_id)
        except Exception as e:
            _clear_activity(session_id)
            # If output was already marked finalized and a post-commit step
            # (usually cleanup) failed, make the session retryable instead of
            # leaving a permanently finalized in-memory/persisted record.
            with session.lock:
                post_commit_failure = session.finalized
                if post_commit_failure:
                    session.finalized = False
            status = "cleanup_error" if post_commit_failure else "finalize_error"
            yield ndjson("error", f"Finalize failed: {_safe_provider_error(e)}", status=status)
        finally:
            _clear_activity(session_id)
            with session.lock:
                owned_tasks = [
                    task for task in (
                        session.finalize_wait_task,
                        session.finalize_upload_task,
                        session.finalize_assembly_task,
                        session.finalize_cleanup_task,
                        session.finalize_pack_task,
                    )
                    if task is not None
                ]
                session.finalize_wait_task = None
                session.finalize_upload_task = None
                session.finalize_assembly_task = None
                session.finalize_cleanup_task = None
                session.finalize_pack_task = None
            if owned_tasks:
                async def _drain_owned():
                    await asyncio.gather(*owned_tasks, return_exceptions=True)
                drain_task = asyncio.create_task(_drain_owned())
                while not drain_task.done():
                    try:
                        await asyncio.shield(drain_task)
                    except asyncio.CancelledError:
                        # Do not release the session while provider/OCR work
                        # still owns its files; a retry may start only after
                        # this drain completes.
                        continue
            _drop_assemble_session_lock(session_id)
            with _sessions_lock:
                still_live = session_id in _sessions
            with session.lock:
                if still_live and session.finalized:
                    # No terminal pop occurred; never strand a live retryable
                    # session behind a finalized flag after cancellation.
                    session.finalized = False
                session.finalizing = False
                finalize_lease = session.finalize_lock_handle
                session.finalize_lock_handle = None
            _release_finalize_lock(finalize_lease)
            if still_live:
                try:
                    _persist_session(session)
                except Exception:
                    pass

    # The response body is only a consumer.  Run the state machine in an
    # independent owner task so a client disconnect (including before the
    # first body chunk is iterated) cannot strand finalizing=True or cancel
    # assembly/cleanup/upload threads underneath it.
    # Keep one spare slot for the stream sentinel. If a disconnected/slow
    # client fills the progress buffer, terminal frames are never displaced by
    # the final ``None`` marker.
    events: asyncio.Queue = asyncio.Queue(maxsize=65)

    async def _put_event(frame):
        # Progress is advisory; when the buffer is full, evict one old
        # progress/terminal frame. The extra sentinel slot above guarantees a
        # terminal event has room before the owner exits. ``put_nowait`` also
        # keeps the owner finally-sentinel safe during task cancellation.
        if events.full():
            try:
                events.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            events.put_nowait(frame)
        except asyncio.QueueFull:
            # A producer/consumer race can only consume the spare slot between
            # the full check and put; retry once after discarding progress.
            try:
                events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            events.put_nowait(frame)

    async def _owner():
        try:
            async for frame in generate():
                await _put_event(frame)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log.error("finalize", f"Finalize owner failed: {_safe_provider_error(error)}")
            try:
                await _put_event(ndjson("error", "Finalize failed", status="finalize_error"))
            except asyncio.CancelledError:
                raise
        finally:
            await _put_event(None)

    owner_task = asyncio.create_task(_owner(), name=f"finalize-owner-{session_id}")
    with session.lock:
        session.finalize_owner_task = owner_task

    async def _response_stream():
        try:
            while True:
                event = await events.get()
                if event is None:
                    break
                yield event
        finally:
            # Cancellation of the HTTP response must not cancel the owner. Keep
            # draining until its durable terminal transition has completed.
            while not owner_task.done():
                try:
                    await asyncio.shield(owner_task)
                except asyncio.CancelledError:
                    continue
            # Consume a possible owner exception so it never becomes an
            # unhandled task warning. The owner normally converts it to a frame.
            try:
                await owner_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            with session.lock:
                if session.finalize_owner_task is owner_task:
                    session.finalize_owner_task = None

    return StreamingResponse(
        _response_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/upload-methods")
async def upload_methods():
    methods = []
    for m in (await asyncio.to_thread(_build_upload_methods)).values():
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

@app.get("/queue")
async def queue_status():
    """Read-only OCR queue metrics for multi-volume backpressure monitoring."""
    return JSONResponse(await asyncio.to_thread(ocr_queue_metrics))


@app.get("/health")
async def health():
    queue = await asyncio.to_thread(ocr_queue_metrics)
    upload_methods = await asyncio.to_thread(_build_upload_methods)
    with _sessions_lock:
        active_sessions = len(_sessions)
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
        "mega_library_root": method_root("mega") or MEGA_LIBRARY_ROOT,
        "upload_default": _MEGA_UPLOAD_DEFAULT,
        "upload_methods": [
            {"id": m.id, "name": m.name, "configured": m.configured,
             "default": m.default, **m.extra}
            for m in upload_methods.values()
        ],
        "upload_method_default": _default_upload_method(),
        "upload_method_selected": None,
        "work_dir": str(WORK_DIR),
        "output_dir": str(OUTPUT_DIR),
        "cors_origins": CORS_ORIGINS,
        # Extra ports the downloader can fetch pages through — each is a separate
        # browser origin worth 6 more sockets. See mokuro_bridge/fetchproxy.py.
        "fetchProxyPorts": list(_fetchproxy.ACTIVE_PORTS),
        # Which CDN hosts those ports may serve, so the userscript can keep one
        # store's pages off a port configured for another store's CDN.
        "fetchUpstreams": list(_fetchproxy.UPSTREAM_PATTERNS),
        "active_sessions": active_sessions,
        "ocr_chunk_size": _OCR_CHUNK_SIZE,
        "ocr_idle_flush_s": _OCR_IDLE_FLUSH_S,
        # Keep the historical scalar field and add count-only details for
        # multi-volume backpressure.  /queue exposes the same snapshot.
        "ocr_queue_depth": queue["queue_depth"],
        "ocr_processing_depth": queue["processing_depth"],
        "ocr_queue_sessions": queue["per_session"],
        "ocr_active_batch_sessions": queue["active_batch_sessions"],
        "ocr_worker": queue["worker"],
    }
    # Busy flag: derived from the activity tracker (what finalize is doing
    # right now) plus any queued OCR work. A polling client can use this to
    # wait for the bridge to go idle instead of racing finalize.
    stage, detail = _current_activity()
    if stage != "idle":
        result["busy"] = True
        result["busy_stage"] = stage  # "ocr" | "uploading"
        result["busy_detail"] = detail
    elif (
        queue["queue_depth"] > 0
        or queue["processing_depth"] > 0
        or any(row.get("ingesting", 0) for row in queue["per_session"])
        or queue.get("source_ingesting_sessions", 0) > 0
    ):
        result["busy"] = True
        result["busy_stage"] = "ocr"
        ingesting = sum(row.get("ingesting", 0) for row in queue["per_session"])
        result["busy_detail"] = (
            f"{ingesting} page(s) being ingested"
            if ingesting
            else "source ingest in progress"
            if queue.get("source_ingesting_sessions", 0)
            else f"{queue['queue_depth'] + queue['processing_depth']} pages in OCR"
        )
    else:
        result["busy"] = False
        result["busy_stage"] = "idle"
        result["busy_detail"] = ""
    if _mokuro_pkg is not None:
        result["mokuro_installed"] = True
        result["mokuro_version"] = getattr(_mokuro_pkg, "__version__", "?")
        result["mokuro_path"] = getattr(_mokuro_pkg, "__file__", "?")
        cached_fork = getattr(_ocr, "_fork_supported_cache", None)
        result["mokuro_fork_api"] = bool(cached_fork) if cached_fork is not None else False

    source = await asyncio.to_thread(_mega_creds_source)
    if source is not None:
        result["mega_configured"] = True
        result["mega_creds_source"] = source

    return JSONResponse(result)
