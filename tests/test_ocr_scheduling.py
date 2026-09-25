"""Fair multi-volume OCR scheduling and count-only queue metrics.

These tests drive the scheduler with tuple-only fake queue state.  They never
start the OCR worker, load mokuro, read a page, or make a network request.
"""
from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import ocr
from mokuro_bridge import sessions


@pytest.fixture
def queue_state(monkeypatch):
    """Give each test an isolated queue and scheduler configuration."""
    with ocr._ocr_cv:
        old_queue = deque(ocr._ocr_queue)
        old_processing = set(ocr._ocr_processing)
        old_force = set(ocr._ocr_force_sessions)
        old_order = deque(ocr._ocr_session_order)
        old_cursor = ocr._ocr_schedule_cursor
        old_started = ocr._ocr_worker_started
        old_thread = ocr._ocr_worker_thread
        old_generator = ocr._generator
        old_force_turn = ocr._ocr_force_turn
        old_retry_counts = dict(ocr._ocr_item_retry_counts)
        old_fork_cache = ocr._fork_supported_cache
        ocr._ocr_queue.clear()
        ocr._ocr_processing.clear()
        ocr._ocr_force_sessions.clear()
        ocr._ocr_session_order.clear()
        ocr._ocr_schedule_cursor = None
        ocr._ocr_worker_started = False
        ocr._ocr_worker_thread = None
        ocr._generator = None
        ocr._ocr_force_turn = False
        ocr._ocr_item_retry_counts.clear()
        ocr._fork_supported_cache = None
    monkeypatch.setattr(ocr, "_OCR_FAIR_SCHEDULING", True)
    monkeypatch.setattr(ocr, "_OCR_CHUNK_SIZE", 4)
    monkeypatch.setattr(ocr, "_OCR_FINALIZE_PRIORITY_RATIO", 0.5)
    yield
    with ocr._ocr_cv:
        ocr._ocr_queue.clear()
        ocr._ocr_queue.extend(old_queue)
        ocr._ocr_processing.clear()
        ocr._ocr_processing.update(old_processing)
        ocr._ocr_force_sessions.clear()
        ocr._ocr_force_sessions.update(old_force)
        ocr._ocr_session_order.clear()
        ocr._ocr_session_order.extend(old_order)
        ocr._ocr_schedule_cursor = old_cursor
        ocr._ocr_worker_started = old_started
        ocr._ocr_worker_thread = old_thread
        ocr._generator = old_generator
        ocr._ocr_force_turn = old_force_turn
        ocr._ocr_item_retry_counts.clear()
        ocr._ocr_item_retry_counts.update(old_retry_counts)
        ocr._fork_supported_cache = old_fork_cache


def test_single_volume_keeps_fifo_page_and_idle_order(queue_state, monkeypatch):
    monkeypatch.setattr(ocr, "_OCR_CHUNK_SIZE", 2)
    with ocr._ocr_cv:
        ocr._ocr_queue.extend(
            [("one", "page-1"), ("one", "page-2"), ("one", "page-3")]
        )
        assert ocr._take_ocr_batch() == [
            ("one", "page-1"),
            ("one", "page-2"),
        ]
        # A partial queue still waits for the normal chunk threshold, then
        # flushes in the same FIFO order during the idle path.
        assert ocr._take_ocr_batch() == []
        assert ocr._take_idle_batch() == [("one", "page-3")]


def test_mixed_finalize_batch_reserves_room_for_active_volume(queue_state):
    with ocr._ocr_cv:
        ocr._ocr_queue.extend(
            [("finalizing", f"f{i}") for i in range(20)]
            + [("active", f"a{i}") for i in range(20)]
        )
        ocr._ocr_force_sessions.add("finalizing")

        first = ocr._take_ocr_batch()
        second = ocr._take_ocr_batch()

    assert len(first) == len(second) == 4
    assert sum(sid == "finalizing" for sid, _ in first) == 2
    assert any(sid == "active" for sid, _ in first)
    # The bounded allotment repeats; the active volume cannot be starved by a
    # continuously draining finalize request.
    assert any(sid == "active" for sid, _ in second)


def test_one_page_batches_still_make_progress_for_other_sessions(
    queue_state, monkeypatch
):
    monkeypatch.setattr(ocr, "_OCR_CHUNK_SIZE", 1)
    with ocr._ocr_cv:
        ocr._ocr_queue.extend(
            [("forced", "f0"), ("forced", "f1"), ("other", "o0")]
        )
        ocr._ocr_force_sessions.add("forced")
        batch = ocr._take_ocr_batch()
    assert batch == [("other", "o0")]


def test_idle_flush_is_round_robin_too(queue_state, monkeypatch):
    monkeypatch.setattr(ocr, "_OCR_CHUNK_SIZE", 8)
    with ocr._ocr_cv:
        ocr._ocr_queue.extend(
            [("a", "a0"), ("a", "a1"), ("b", "b0"), ("b", "b1")]
        )
        assert ocr._take_ocr_batch() == []
        batch = ocr._take_idle_batch()
    assert batch == [("a", "a0"), ("b", "b0"), ("a", "a1"), ("b", "b1")]


def test_legacy_fifo_mode_remains_available(queue_state, monkeypatch):
    monkeypatch.setattr(ocr, "_OCR_FAIR_SCHEDULING", False)
    monkeypatch.setattr(ocr, "_OCR_CHUNK_SIZE", 2)
    with ocr._ocr_cv:
        ocr._ocr_queue.extend(
            [("forced", f"f{i}") for i in range(40)]
            + [("other", f"o{i}") for i in range(3)]
        )
        ocr._ocr_force_sessions.add("forced")
        batch = ocr._take_ocr_batch()
    # This is the historical force-drain behavior: up to 32 forced pages can
    # be selected before ordinary work, useful as an explicit compatibility
    # escape hatch.
    assert len(batch) == 32
    assert {sid for sid, _ in batch} == {"forced"}


def test_queue_metrics_are_count_only_and_report_worker_state(
    queue_state, monkeypatch
):
    monkeypatch.setattr(
        sessions,
        "_sessions",
        {"finalizing": object(), "active": object(), "idle": object()},
    )
    monkeypatch.setattr(
        ocr,
        "_ocr_worker_thread",
        SimpleNamespace(is_alive=lambda: True),
    )
    monkeypatch.setattr(ocr, "_ocr_worker_started", True)
    monkeypatch.setattr(ocr, "_generator", object())
    with ocr._ocr_cv:
        ocr._ocr_queue.extend(
            [("finalizing", "secret-page-1"), ("finalizing", "secret-page-2"),
             ("active", "secret-page-3")]
        )
        ocr._ocr_processing.update(
            {("finalizing", "secret-page-4"), ("active", "secret-page-5")}
        )

    payload = ocr.ocr_queue_metrics()
    rows = {row["session_id"]: row for row in payload["per_session"]}
    assert payload["queue_depth"] == 3
    assert payload["processing_depth"] == 2
    assert payload["total_depth"] == 5
    assert rows["finalizing"]["pending"] == 2
    assert rows["finalizing"]["processing"] == 1
    assert rows["active"]["pending"] == 1
    assert rows["active"]["processing"] == 1
    assert rows["idle"] == {"session_id": "idle", "pending": 0, "processing": 0}
    assert payload["active_batch_sessions"] == ["active", "finalizing"]
    assert payload["worker"]["state"] == "processing"
    assert payload["worker"]["alive"] is True
    assert "secret-page" not in repr(payload)
    assert "vol_dir" not in repr(payload)


def test_queue_endpoint_is_read_only_and_does_not_expose_paths(queue_state):
    from mokuro_bridge.api import app

    with ocr._ocr_cv:
        ocr._ocr_queue.append(("session-1", "/private/secret/page.webp"))

    response = TestClient(app).get("/queue")
    assert response.status_code == 200
    payload = response.json()
    assert payload["queue_depth"] == 1
    assert payload["per_session"] == [
        {"session_id": "session-1", "pending": 1, "processing": 0}
    ]
    serialized = response.text
    assert "/private/secret" not in serialized
    assert "page.webp" not in serialized
