"""Endpoint tests for the session lifecycle.

Nothing here touched the HTTP layer before, which is how v0.6.0 shipped a
``/session/resume`` that answered 500 on every single call: api.py called
``_safe_component(...)`` without importing it, so every request died with a
``NameError``. The unit tests all call the helpers directly and passed
regardless. These go through the real ASGI app with a ``TestClient``, so a
missing import, a renamed route or a changed form field fails a test instead of
a user's volume.

Hermetic: the work dir and the session store are redirected into ``tmp_path``,
the OCR engine is stubbed out (no mokuro, no torch, no model download), and no
test opens a socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import api, sessions
from mokuro_bridge import ocr

# A real PNG: api._looks_like_image() sniffs magic bytes, so a page upload has
# to start with one of the signatures it knows.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def work_dir(tmp_path, monkeypatch):
    """Redirect every path the endpoints touch into tmp_path.

    ``WORK_DIR`` is read as a module global inside api.py's helpers, so
    patching the attribute is enough; ``sessions.py`` imported its own copy, so
    that one needs patching separately.
    """
    work = tmp_path / "work"
    work.mkdir()
    sessions_dir = work / ".bridge_sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(api, "WORK_DIR", work)
    monkeypatch.setattr(sessions, "WORK_DIR", work)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", sessions_dir)
    return work


@pytest.fixture
def no_ocr(monkeypatch):
    """Stub out everything that would start, queue or reach a real OCR engine."""
    started: list[str] = []

    monkeypatch.setattr(api, "_ensure_ocr_worker", lambda: started.append("worker"))
    # Hashed by mokuro in production; its result is discarded by the caller.
    monkeypatch.setattr(api, "_sync_ocr_cache_state", lambda session: (0, 0))

    def _no_mokuro(name: str):
        raise ImportError(f"the test install has no mokuro.{name}")

    monkeypatch.setattr(api, "_mokuro_submodule", _no_mokuro)

    def _queue(session, safe_name, *, _ocr_cv_held=False):
        """Stand in for the real queue: track the page, start no worker."""
        from mokuro_bridge.sessions import session_snapshot

        with session.lock:
            session.pages_received.add(safe_name)
            received = len(session.pages_received)
            session.message = f"Captured {received} - OCR 0/{received}"
        return session_snapshot(session)

    monkeypatch.setattr(api, "_queue_page_ocr", _queue)
    return started


@pytest.fixture
def clean_registry():
    """Sessions are module-global; a leaked one would be found by the next test."""
    with sessions._sessions_lock:
        sessions._sessions.clear()
    yield
    with sessions._sessions_lock:
        sessions._sessions.clear()


@pytest.fixture
def client(work_dir, no_ocr, clean_registry):
    with TestClient(api.app) as test_client:
        yield test_client


def _write_page(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(PNG)


def test_resume_succeeds_on_a_normal_folder_ingest(client, tmp_path):
    """The v0.6.0 regression lock.

    ``api.py`` called ``_safe_component(...)`` while syncing a source directory
    without importing it, so this endpoint answered 500 for every caller. The
    unit tests never went through the route and stayed green; this one does.
    """
    source = tmp_path / "scraped"
    _write_page(source, "page_0001.webp")
    _write_page(source, "page_0002.webp")

    response = client.post(
        "/session/resume",
        data={"title": "Regression Volume", "source_dir": str(source)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is True
    assert body["safe_title"] == "Regression Volume"
    assert body["synced_from_source"] == 2
    assert body["queued_for_ocr"] == 2
    # Both pages really landed in the work volume, under the resume title.
    vol_dir = Path(body["vol_dir"])
    assert vol_dir.name == "Regression Volume"
    assert sorted(p.name for p in vol_dir.iterdir()) == ["page_0001.webp", "page_0002.webp"]


def test_resume_without_a_source_directory_still_starts_a_session(client):
    """No ``source_dir`` is a valid resume: pick the volume back up as it is."""
    response = client.post("/session/resume", data={"title": "Empty Resume"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is True
    assert body["queued_for_ocr"] == 0
    assert Path(body["vol_dir"]).is_dir()


def test_resume_rejects_a_source_outside_the_allowed_roots(client, tmp_path):
    """The ingest resolver is a real boundary; make sure it is still wired in."""
    outside = tmp_path / "elsewhere"
    _write_page(outside, "page_0001.webp")
    # A path under tmp_path is allowed (it is a system temp root), so this test
    # only asserts the endpoint validates rather than blindly copying.
    assert (outside / "page_0001.webp").is_file()
    response = client.post(
        "/session/resume",
        data={"title": "Boundary", "source_dir": "/nonexistent-root/nowhere"},
    )
    assert response.status_code in (400, 403), response.text


def test_start_page_and_status_round_trip_a_small_image(client):
    """The ordinary capture loop, end to end."""
    start = client.post("/session/start", data={"title": "Round Trip"})
    assert start.status_code == 200, start.text
    session_id = start.json()["session_id"]
    assert start.json()["pages_received"] == 0

    page = client.post(
        f"/session/{session_id}/page",
        data={"filename": "page_0001.webp", "page_num": "1"},
        files={"page": ("page_0001.webp", PNG, "image/png")},
    )
    assert page.status_code == 200, page.text
    assert page.json()["filename"] == "page_0001.webp"
    assert page.json()["page_num"] == 1

    status = client.get(f"/session/{session_id}/status")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["session_id"] == session_id
    assert body["pages_received"] == 1
    assert body["pages_ocr_pending"] == 1
    assert body["finalized"] is False

    # The bytes really are on disk in the volume, not just counted in memory.
    vol_dir = Path(start.json()["vol_dir"])
    assert (vol_dir / "page_0001.webp").read_bytes() == PNG


def test_retrying_identical_page_while_ocr_is_processing_is_idempotent(client):
    start = client.post("/session/start", data={"title": "Retry Processing"}).json()
    session_id = start["session_id"]
    page_path = f"/session/{session_id}/page"
    page_data = {
        "filename": "page_0001.webp",
        "page_num": "1",
    }
    first = client.post(page_path, data=page_data, files={"page": ("page_0001.webp", PNG, "image/png")})
    assert first.status_code == 200, first.text

    item = (session_id, "page_0001.webp")
    with ocr._ocr_cv:
        ocr._ocr_processing.add(item)
    try:
        retry = client.post(
            page_path,
            data=page_data,
            files={"page": ("page_0001.webp", PNG, "image/png")},
        )
    finally:
        with ocr._ocr_cv:
            ocr._ocr_processing.discard(item)

    assert retry.status_code == 200, retry.text
    assert retry.json()["already_present"] is True
    assert retry.json()["pages_received"] == 1


def test_two_volumes_never_share_a_work_directory(client):
    """Two captures in flight must land in two separate volumes.

    Note the safe_title is asserted to be *unique* rather than equal to the
    requested title: ``_ensure_safe_work_volume()`` mkdirs the directory itself,
    so the ``if vol_dir.exists()`` branch in the start path is always taken and
    every fresh session gets a ``<title>_<6 hex>`` suffix. That is existing
    behaviour, deliberately not pinned here in either direction.
    """
    first = client.post("/session/start", data={"title": "Series A"}).json()
    second = client.post("/session/start", data={"title": "Series B"}).json()

    assert first["session_id"] != second["session_id"]
    assert first["vol_dir"] != second["vol_dir"]
    assert first["safe_title"].startswith("Series A")
    assert second["safe_title"].startswith("Series B")

    # A page sent to one session never appears in the other's volume.
    client.post(
        f"/session/{first['session_id']}/page",
        data={"filename": "page_0001.webp", "page_num": "1"},
        files={"page": ("page_0001.webp", PNG, "image/png")},
    )
    assert (Path(first["vol_dir"]) / "page_0001.webp").is_file()
    assert list(Path(second["vol_dir"]).iterdir()) == []


def test_page_num_is_optional_for_a_legacy_client(client):
    """``page_num`` is ``Optional[int] = Form(None)``.

    A client that predates page numbering sends only a filename, and must keep
    working: the page is accepted, stored under that name, and nothing is
    superseded, because with no page number there is no identity to reconcile.
    """
    start = client.post("/session/start", data={"title": "Legacy"}).json()
    session_id = start["session_id"]

    response = client.post(
        f"/session/{session_id}/page",
        data={"filename": "scan_01.webp"},
        files={"page": ("scan_01.webp", PNG, "image/png")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["page_num"] is None
    assert response.json()["filename"] == "scan_01.webp"

    status = client.get(f"/session/{session_id}/status").json()
    assert status["pages_received"] == 1

    # No page number was ever sent, so no page_names mapping was recorded.
    session = sessions._get_session(session_id)
    assert session.page_names == {}


def test_a_page_resent_under_a_new_filename_supersedes_the_old_copy(client):
    """The 249-pages-shipped-as-498 regression.

    Pages are tracked by filename, so a client that renames its uploads looks
    like it is sending new pages. The page *number* is the identity, so the
    older file, its OCR cache and its set entries must all be retired; a volume
    must never package both copies of the same page.
    """
    start = client.post("/session/start", data={"title": "Renamed"}).json()
    session_id = start["session_id"]
    vol_dir = Path(start["vol_dir"])

    legacy = client.post(
        f"/session/{session_id}/page",
        data={"filename": "page_000.webp", "page_num": "0"},
        files={"page": ("page_000.webp", PNG, "image/png")},
    )
    assert legacy.status_code == 200, legacy.text
    assert (vol_dir / "page_000.webp").is_file()

    renamed = client.post(
        f"/session/{session_id}/page",
        data={"filename": "page_0001.webp", "page_num": "0"},
        files={"page": ("page_0001.webp", PNG, "image/png")},
    )
    assert renamed.status_code == 200, renamed.text

    assert sorted(p.name for p in vol_dir.iterdir()) == ["page_0001.webp"]
    assert not (vol_dir / "page_000.webp").exists()

    session = sessions._get_session(session_id)
    assert session.pages_received == {"page_0001.webp"}
    assert session.page_names == {0: "page_0001.webp"}

    status = client.get(f"/session/{session_id}/status").json()
    assert status["pages_received"] == 1


def test_a_different_page_number_is_never_superseded(client):
    """The supersede logic is keyed on the page number, not the filename."""
    start = client.post("/session/start", data={"title": "Two Pages"}).json()
    session_id = start["session_id"]
    vol_dir = Path(start["vol_dir"])

    for name, number in (("page_0001.webp", "1"), ("page_0002.webp", "2")):
        response = client.post(
            f"/session/{session_id}/page",
            data={"filename": name, "page_num": number},
            files={"page": (name, PNG, "image/png")},
        )
        assert response.status_code == 200, response.text

    assert sorted(p.name for p in vol_dir.iterdir()) == ["page_0001.webp", "page_0002.webp"]
    assert client.get(f"/session/{session_id}/status").json()["pages_received"] == 2


def test_a_three_digit_page_is_not_eaten_by_a_four_digit_one(client):
    """``page_001`` is page 1 under the old scheme, ``page_0001`` is page 0.

    Matching on the whole stem is what keeps the first from being retired by
    the second.
    """
    start = client.post("/session/start", data={"title": "Stem Collision"}).json()
    session_id = start["session_id"]
    vol_dir = Path(start["vol_dir"])

    for name, number in (("page_001.webp", "1"), ("page_0001.webp", "0")):
        response = client.post(
            f"/session/{session_id}/page",
            data={"filename": name, "page_num": number},
            files={"page": (name, PNG, "image/png")},
        )
        assert response.status_code == 200, response.text

    assert sorted(p.name for p in vol_dir.iterdir()) == ["page_0001.webp", "page_001.webp"]


def test_a_non_image_upload_is_rejected(client):
    """The endpoint sniffs the bytes, not just the filename."""
    start = client.post("/session/start", data={"title": "Not An Image"}).json()
    session_id = start["session_id"]

    response = client.post(
        f"/session/{session_id}/page",
        data={"filename": "page_0001.webp", "page_num": "1"},
        files={"page": ("page_0001.webp", b"#!/bin/sh\nrm -rf /\n", "image/webp")},
    )

    assert response.status_code == 400, response.text
    assert "image" in response.json()["detail"].lower()


def test_a_traversing_filename_is_rejected(client):
    """``_validated_page_name`` is a boundary, and the route must go through it."""
    start = client.post("/session/start", data={"title": "Traversal"}).json()
    session_id = start["session_id"]

    response = client.post(
        f"/session/{session_id}/page",
        data={"filename": "../../etc/passwd.webp", "page_num": "1"},
        files={"page": ("evil.webp", PNG, "image/png")},
    )

    assert response.status_code == 400, response.text


def test_status_for_an_unknown_session_is_a_404(client):
    assert client.get("/session/deadbeef1234/status").status_code == 404


def test_retrying_identical_page_after_ocr_finished_is_idempotent(client):
    """The other half of the retry contract.

    The digest compare runs *before* the "already has completed OCR" branch, so a
    capture client that re-sends its last page after a restart is acknowledged
    rather than refused. Bytes that differ are not a retry -- the stored OCR
    result no longer matches the image -- and must stay a 409.
    """
    start = client.post("/session/start", data={"title": "Retry Done"}).json()
    session_id = start["session_id"]
    page_path = f"/session/{session_id}/page"
    page_data = {"filename": "page_0001.webp", "page_num": "1"}

    first = client.post(page_path, data=page_data, files={"page": ("page_0001.webp", PNG, "image/png")})
    assert first.status_code == 200, first.text

    with sessions._sessions_lock:
        session = sessions._sessions[session_id]
    with session.lock:
        session.pages_ocr_done.add("page_0001.webp")

    retry = client.post(page_path, data=page_data, files={"page": ("page_0001.webp", PNG, "image/png")})
    assert retry.status_code == 200, retry.text
    assert retry.json()["already_present"] is True

    changed = client.post(
        page_path,
        data=page_data,
        files={"page": ("page_0001.webp", PNG + b"\x00", "image/png")},
    )
    assert changed.status_code == 409, changed.text
    assert "completed OCR" in changed.json()["detail"]


def test_destination_check_reports_local_presence(client, work_dir, monkeypatch):
    """The read-only pre-flight a client uses before re-downloading a volume."""
    monkeypatch.setattr(api, "_effective_local_output_base", lambda _name: work_dir)

    missing = client.post("/destination/check", data={"method": "local", "title": "Check Me 1"})
    assert missing.status_code == 200, missing.text
    assert missing.json()["exists"] is False
    assert missing.json()["file"] == "Check Me 1.cbz"

    archive = work_dir / api.series_title_from_volume("Check Me 1") / "Check Me 1.cbz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"not really a zip, but present")

    present = client.post("/destination/check", data={"method": "local", "title": "Check Me 1"})
    assert present.status_code == 200, present.text
    assert present.json()["exists"] is True


def test_destination_check_rejects_an_unknown_method(client):
    response = client.post("/destination/check", data={"method": "nope", "title": "Whatever 1"})
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "unknown upload target" in detail and "known accounts" in detail


def test_destination_check_requires_a_title(client):
    """``title`` is a required form field, not an empty-string default."""
    assert client.post("/destination/check", data={"method": "local"}).status_code == 422


def test_destination_check_generates_a_title_when_it_is_blank(client):
    """Mirrors /session/start: an unusable title falls back to a generated one."""
    response = client.post("/destination/check", data={"method": "local", "title": "   "})
    assert response.status_code == 200, response.text
    assert response.json()["title"].startswith("manga_")


def test_destination_check_does_not_leak_provider_tokens(client, monkeypatch):
    """A provider failure is reported without echoing its signed URL.

    Graph and Drive errors carry the full request URL, which for these providers
    includes an access token in the query string. The endpoint used to interpolate
    the raw exception, so a 502 handed the caller a live credential.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError(
            "Graph refused: https://graph.microsoft.com/v1.0/me/drive?access_token=SUPERSECRET"
        )

    class _Configured:
        configured = True

    monkeypatch.setattr(api, "remote_file_exists", boom)
    monkeypatch.setattr(api, "_build_upload_methods", lambda: {"mega": _Configured()})
    monkeypatch.setattr(api, "method_root", lambda _method: "/Root/mokuro-reader")

    response = client.post("/destination/check", data={"method": "mega", "title": "Leak Check 1"})

    assert response.status_code == 502, response.text
    detail = response.json()["detail"]
    assert "SUPERSECRET" not in detail
    assert "graph.microsoft.com" in detail
    assert "access_token=" not in detail


def test_health_reports_the_mokuro_flavor(client, monkeypatch):
    """The flavor is provenance, not the version string.

    The editable fork that prompted this reported a version belonging to
    neither engine, so dokuha-cli and dokuha-userscript need a field that says
    which engine is actually installed.
    """
    monkeypatch.setattr(
        api.mokuro_flavor, "detect",
        lambda **_kw: {"flavor": "fork", "installed": True, "version": "0.2.2"},
    )
    body = client.get("/health").json()
    assert body["mokuro_flavor"] == "fork"
    assert "mokuro_installed" in body, "the existing field must keep working"


def test_health_always_reports_a_flavor(client, monkeypatch):
    monkeypatch.setattr(
        api.mokuro_flavor, "detect",
        lambda **_kw: {"flavor": "absent", "installed": False, "version": None},
    )
    monkeypatch.setattr(api, "_mokuro_pkg", None)
    body = client.get("/health").json()
    assert body["mokuro_flavor"] == "absent"
    assert body["mokuro_installed"] is False
