"""A mokuro-bunko destination owns series.json, so finalize must leave it alone."""
from __future__ import annotations

import json

import pytest

from mokuro_bridge import bunko

HEARTBEAT = "2026-10-19T20:00:00.000Z"
HEARTBEAT_SECONDS = bunko._parse_iso_milliseconds(HEARTBEAT)


def _marker(**overrides):
    payload = {
        "version": 1,
        "kind": "mokuro-bunko",
        "instance_id": "6f1c",
        "boot_id": "a93e",
        "instance_name": "nas",
        "bunko_version": "0.8.0",
        "epoch": 3,
        "claimed_at": HEARTBEAT,
        "heartbeat_at": HEARTBEAT,
        "ttl_s": 900,
        "state": "active",
        "compiles": ["series.json", "catalog.json"],
        "ocr": True,
        "queue": ".mokuro-queue.json",
        "took_over_from": None,
    }
    payload.update(overrides)
    return payload


def _fresh_enough(**overrides):
    return bunko.parse_marker(json.dumps(_marker(**overrides)))


def test_marker_with_a_fresh_heartbeat_owns_series_json():
    marker = _fresh_enough()
    assert marker is not None
    assert marker.manages(now=HEARTBEAT_SECONDS + 60)


def test_released_marker_does_not_own_series_json():
    marker = _fresh_enough(state="released")
    assert marker is not None
    assert not marker.manages(now=HEARTBEAT_SECONDS + 60)


def test_missing_series_json_from_compiles_does_not_own_it():
    marker = _fresh_enough(compiles=["catalog.json"])
    assert marker is not None
    assert not marker.manages(now=HEARTBEAT_SECONDS + 60)


def test_stale_heartbeat_does_not_own_series_json():
    marker = _fresh_enough()
    assert marker is not None
    late = HEARTBEAT_SECONDS + marker.ttl_s + bunko.HEARTBEAT_GRACE_SECONDS + 1
    assert not marker.manages(now=late)


def test_a_future_heartbeat_inside_the_skew_grace_is_still_fresh():
    marker = _fresh_enough()
    assert marker is not None
    assert marker.manages(now=HEARTBEAT_SECONDS - 60)


def test_unknown_kind_and_bad_json_are_not_markers():
    assert bunko.parse_marker(json.dumps(_marker(kind="something-else"))) is None
    assert bunko.parse_marker(b"not json") is None
    assert bunko.parse_marker(b"[]") is None
    assert bunko.parse_marker(None) is None


def test_missing_state_reads_as_active():
    payload = dict(_marker())
    payload.pop("state")
    marker = bunko.parse_marker(payload)
    assert marker is not None
    assert marker.state == bunko.ACTIVE_STATE


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok", "db_status": "ok", "library_status": "ok",
         "total_users": 1, "total_volumes": 12,
         "ocr": {"backend": "cpu", "worker_alive": True, "pending": 0, "failed": 0}},
        {"status": "degraded", "db_status": "ok", "library_status": "unavailable",
         "total_volumes": None, "ocr": {"backend": "skip", "worker_alive": None}},
    ],
)
def test_bunko_health_bodies_are_recognised(body):
    assert bunko.is_bunko_health(body)


@pytest.mark.parametrize(
    "body",
    [None, {}, {"status": "ok"}, {"ultimate": "ok", "ocr": {}},
     {"library_status": "ok"}, {"library_status": "ok", "ocr": "cpu"}],
)
def test_generic_health_bodies_are_not_bunko(body):
    assert not bunko.is_bunko_health(body)


def test_probe_marker_reads_the_root_and_fails_closed():
    calls = []

    def download(name, remote_dir, filename):
        calls.append((name, remote_dir, filename))
        return json.dumps(_marker()).encode("utf-8")

    assert bunko.probe_marker(download, "default", "mokuro-reader", now=HEARTBEAT_SECONDS + 60)
    assert calls == [("default", "mokuro-reader", bunko.MARKER_NAME)]

    def absent(name, remote_dir, filename):
        return None

    assert not bunko.probe_marker(absent, "default", "mokuro-reader", now=HEARTBEAT_SECONDS + 60)
