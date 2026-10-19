"""Independent execution lanes for the blocking work behind the HTTP routes.

The bridge used to run every ``asyncio.to_thread`` call on the event loop's
default executor. That executor is a single shared queue, so a workload that
occupies its threads for a long time starves every other route: with 20-30
volumes finalizing at once, threads sat in ``wait_for_session_ocr`` (one per
volume, polling), queued on the global assembly lock, and uploaded artifacts for
minutes. ``/health`` and ``/session/start`` then queued behind them and the
client timed out after 15 s.

Each workload class gets its own bounded pool instead. Bounded, so a burst of 50
volumes cannot spawn 150 threads; independent, so a blocked lane leaves the
others answering. The lanes are:

``control``
    Short bookkeeping on the request path: session lookup, provider selection,
    the upload-method registry. Must never be occupied by a long task.
``data``
    Page bytes: writes, hashing, copies, source-directory sync. Bounded so 50
    concurrent capture clients cannot exhaust the machine's I/O.
``finalize``
    Per-volume assembly and packaging. These are CPU-bound and can take seconds
    per 200-page volume; keeping them here lets several volumes assemble in
    parallel without touching the request path.
``upload``
    Provider uploads, including the early cover push and ``series.json``. Network
    bound and the longest-running blocking work, so it is capped and isolated.
``ocr``
    Not an executor: the single OCR worker thread in ``ocr.py`` is already a
    dedicated lane.

Every lane is a plain ``ThreadPoolExecutor``. Nothing here imports the bridge
application, so tests and tools can use it without pulling in FastAPI.
"""

from __future__ import annotations

import asyncio
import functools
import os
from concurrent.futures import ThreadPoolExecutor


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 64) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


# Sized so the lanes cannot add up to more threads than one volume's OCR + upload
# needs, while still letting independent series make progress in parallel. All
# four are overridable for small machines and for CI, where a single worker keeps
# tests deterministic.
CONTROL_WORKERS = _env_int("MOKURO_BRIDGE_CONTROL_THREADS", 4)
DATA_WORKERS = _env_int("MOKURO_BRIDGE_DATA_THREADS", 8)
FINALIZE_WORKERS = _env_int("MOKURO_BRIDGE_FINALIZE_THREADS", 4)
UPLOAD_WORKERS = _env_int("MOKURO_BRIDGE_UPLOAD_THREADS", 8)

control = ThreadPoolExecutor(max_workers=CONTROL_WORKERS, thread_name_prefix="mokuro-control")
data = ThreadPoolExecutor(max_workers=DATA_WORKERS, thread_name_prefix="mokuro-data")
finalize = ThreadPoolExecutor(max_workers=FINALIZE_WORKERS, thread_name_prefix="mokuro-finalize")
upload = ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="mokuro-upload")

_ALL = (control, data, finalize, upload)


def _queued(executor: ThreadPoolExecutor) -> int:
    """Work items waiting for a thread. ``SimpleQueue`` has no public qsize."""
    try:
        return int(executor._work_queue.qsize())  # type: ignore[attr-defined]
    except Exception:
        return 0


def lane_stats() -> dict[str, dict[str, int]]:
    """Per-lane worker and backlog counts for /health.

    Exposed so a stalled run is diagnosable from the same cheap snapshot that
    proves the bridge is alive: a queue that grows without bound names the lane
    that is behind instead of leaving one slow route as the only symptom.
    """
    names = ("control", "data", "finalize", "upload")
    return {
        name: {"workers": executor._max_workers, "queued": _queued(executor)}
        for name, executor in zip(names, _ALL)
    }


async def to_control(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(control, functools.partial(func, *args, **kwargs))


async def to_data(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(data, functools.partial(func, *args, **kwargs))


async def to_finalize(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(finalize, functools.partial(func, *args, **kwargs))


async def to_upload(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(upload, functools.partial(func, *args, **kwargs))


def shutdown(wait: bool = False) -> None:
    """Stop the lanes. Called at interpreter exit and by tests that need a clean slate."""
    for executor in _ALL:
        executor.shutdown(wait=wait, cancel_futures=True)
