"""Control-plane responsiveness under load.

The failure these tests lock down: 20-30 volumes finalizing at once. Every
``asyncio.to_thread`` call shared the event loop's one default executor, and
finalize filled it with threads parked for minutes (a polling
``wait_for_session_ocr`` per volume, queued on a process-wide assembly lock,
plus uploads). ``/health`` then had to wait for a free thread just to count the
OCR queue, and ``dokuha-cli`` timed out ``session/start`` after 15 s.

The fix has two halves, both tested here: /health is served from a snapshot
built off the request path, and the request path itself no longer depends on the
shared default executor.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import api, ocr, sessions

QUEUE = {
    "queue_depth": 0,
    "processing_depth": 0,
    "per_session": [],
    "active_batch_sessions": [],
    "worker": {"state": "idle"},
}


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """A bridge with no model, no keychain, no home-directory writes."""
    work = tmp_path / "work"
    work.mkdir()
    sessions_dir = work / ".bridge_sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(api, "WORK_DIR", work)
    monkeypatch.setattr(sessions, "WORK_DIR", work)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(api, "_ensure_ocr_worker", lambda: None)
    monkeypatch.setattr(api, "_mega_creds_source", lambda: None)
    monkeypatch.setattr(api, "_build_upload_methods", lambda: {})
    def _metrics(timeout=None):
        payload = dict(QUEUE)
        if timeout is not None:
            payload["partial"] = False
        return payload

    monkeypatch.setattr(api, "ocr_queue_metrics", _metrics)
    monkeypatch.setattr(api.update, "maybe_refresh_async", lambda: None)
    with sessions._sessions_lock:
        sessions._sessions.clear()
    try:
        yield work
    finally:
        api._stop_health_reporter()
        with sessions._sessions_lock:
            sessions._sessions.clear()


def _preset_snapshot(monkeypatch, *, payload=None, queue=None):
    """Install a snapshot as if the reporter had already built one."""
    monkeypatch.setattr(
        api, "_health_snapshot_payload", payload if payload is not None else api._health_base_payload()
    )
    monkeypatch.setattr(api, "_health_snapshot_queue", queue)
    monkeypatch.setattr(api, "_health_snapshot_built_at", time.monotonic())
    monkeypatch.setattr(api, "_health_snapshot_error", None)


def test_health_is_answered_from_the_snapshot_while_the_ocr_lock_is_held(isolated, monkeypatch):
    _preset_snapshot(monkeypatch, queue=dict(QUEUE))
    client = TestClient(api.app)  # no lifespan: the preset snapshot is in force
    held = threading.Event()
    release = threading.Event()

    def hold_ocr_lock():
        with api._ocr_cv:
            held.set()
            release.wait(3)

    holder = threading.Thread(target=hold_ocr_lock, daemon=True)
    holder.start()
    assert held.wait(1.0), "the test could not take the OCR lock"
    try:
        started = time.monotonic()
        for _ in range(10):
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(3)

    # Ten round trips through the ASGI app, with the OCR lock the health builder
    # itself needs held the whole time. The old handler waited on that lock.
    assert elapsed < 2.0, f"/health took {elapsed:.2f}s while the OCR lock was held"


def test_queue_is_bounded_instead_of_waiting_on_the_ocr_lock(isolated, monkeypatch):
    """A busy OCR writer cannot make /queue hang; it answers a partial snapshot."""
    monkeypatch.setattr(api, "ocr_queue_metrics", ocr.ocr_queue_metrics)
    held = threading.Event()
    release = threading.Event()

    def hold_ocr_lock():
        with api._ocr_cv:
            held.set()
            release.wait(3)

    holder = threading.Thread(target=hold_ocr_lock, daemon=True)
    holder.start()
    assert held.wait(1.0), "the test could not take the OCR lock"
    try:
        started = time.monotonic()
        body = TestClient(api.app).get("/queue").json()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(3)

    assert elapsed < 1.0, f"/queue took {elapsed:.2f}s behind a held OCR lock"
    assert body["partial"] is True
    assert isinstance(body["queue_depth"], int)


def test_session_start_does_not_need_the_shared_executor(isolated, monkeypatch):
    """A saturated default executor (what finalize used to do) cannot delay start."""

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        gate = threading.Event()
        blocker = loop.run_in_executor(None, gate.wait)  # occupies the only thread
        try:
            started = time.monotonic()
            response = await asyncio.wait_for(
                api._session_start_impl("Saturated Volume", "false"), timeout=5
            )
            return time.monotonic() - started, response
        finally:
            gate.set()
            await blocker

    elapsed, response = asyncio.run(scenario())
    assert response.status_code == 200, response.body
    assert elapsed < 2.0, f"session/start took {elapsed:.2f}s with the executor saturated"


def test_lifespan_starts_and_stops_the_health_reporter(isolated):
    with TestClient(api.app) as client:
        assert api._health_thread is not None
        assert api._health_thread.is_alive()
        assert api._health_snapshot_payload is not None
        body = client.get("/health").json()
        # Every workload class has its own lane, and the report names them.
        assert set(body["lanes"]) == {"control", "data", "finalize", "upload"}
        assert body["health_snapshot"]["age_ms"] >= 0
    assert not api._health_thread.is_alive(), "the reporter outlived the ASGI lifespan"


def test_wait_for_session_ocr_async_does_not_park_a_worker_thread(isolated, monkeypatch):
    monkeypatch.setattr(ocr, "_ensure_ocr_worker", lambda: None)
    session = sessions.Session(
        session_id="abc123", title="Volume", safe_title="Volume", vol_dir=isolated / "Volume"
    )

    started = time.monotonic()
    asyncio.run(asyncio.wait_for(ocr.wait_for_session_ocr_async(session), timeout=2))
    assert time.monotonic() - started < 1.0
