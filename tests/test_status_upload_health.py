"""GET /health carries a small upload snapshot only while an upload is active.

The terminal display (``mokuro_bridge --status``) reads ``/health`` only, so the
progress bar it draws depends on this field. Keep the snapshot small and absent
when idle so no existing client sees a new key in the common case.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mokuro_bridge import api
from mokuro_bridge.sessions import Session


@pytest.fixture()
def client(monkeypatch):
    queue = {"queue_depth": 0, "processing_depth": 0, "per_session": [],
             "active_batch_sessions": [], "worker": {"state": "idle"}}
    monkeypatch.setattr(api, "ocr_queue_metrics", lambda: queue)
    monkeypatch.setattr(api, "_build_upload_methods", lambda: [])
    monkeypatch.setattr(api, "_health_upload_methods", lambda methods: [])
    monkeypatch.setattr(api, "_mega_creds_source", lambda: None)
    monkeypatch.setattr(api.update, "maybe_refresh_async", lambda: None)
    monkeypatch.setattr(api.mokuro_flavor, "health_fields",
                        lambda: {"mokuro_flavor": "fork", "mokuro_version": "0.4.0",
                                 "mokuro_installed": True})
    return TestClient(api.app)


@pytest.fixture()
def session():
    created = Session(session_id="abc123", title="Series 1", safe_title="Series_1",
                      vol_dir=Path("/tmp/series-1"))
    with api._sessions_lock:
        api._sessions[created.session_id] = created
    try:
        yield created
    finally:
        with api._sessions_lock:
            api._sessions.pop(created.session_id, None)


def test_idle_health_has_no_upload_key(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert "upload" not in response.json()


def test_active_upload_is_reported_in_health(client, session):
    api._set_upload_state(
        session, active=True, file="vol01.cbz", percent=42.0,
        current_bytes=21_000_000, total_bytes=50_000_000, speed_human="3.4 MB/s",
    )
    body = client.get("/health").json()
    assert body["upload"] == {
        "file": "vol01.cbz",
        "percent": 42.0,
        "current_bytes": 21_000_000,
        "total_bytes": 50_000_000,
        "speed_human": "3.4 MB/s",
    }


def test_finished_upload_disappears_from_health(client, session):
    api._set_upload_state(session, active=True, file="vol01.cbz", percent=42.0)
    assert client.get("/health").json()["upload"]["file"] == "vol01.cbz"
    api._set_upload_state(session, active=False, file="vol01.cbz", percent=100.0)
    assert "upload" not in client.get("/health").json()


def test_snapshot_helper_ignores_missing_and_malformed_upload_state(session):
    assert api._active_upload_snapshot() is None
    session.upload = {"percent": 10}  # no "active" flag
    assert api._active_upload_snapshot() is None
    session.upload = ["not", "a", "dict"]
    assert api._active_upload_snapshot() is None
