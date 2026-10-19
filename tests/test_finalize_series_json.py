"""Finalize writes and merges series.json, through the real app."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import api, series_json, sessions

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _Title:
    def __init__(self, path):
        self.name = ""
        self._uuid = ""


class _Volume:

    def __init__(self, path):
        self.dir = Path(path)
        self.title = _Title(path)
        self.uuid = ""
        self.path_ocr_cache = Path(path) / "_ocr"


class _Generator:

    def __init__(self, holder: dict):
        self._holder = holder

    def generate_mokuro_file(self, volume, _convert):
        pages = [
            {"version": "0.2.2", "blocks": [{"lines": ["日本語のテキスト", "abc"]}]}
            for image in sorted(Path(volume.dir).iterdir())
            if image.suffix.lower() in api.IMAGE_EXTENSIONS
        ]
        payload = {
            "version": "0.2.2",
            "title": volume.title.name,
            "title_uuid": volume.title._uuid,
            "volume": Path(volume.dir).name,
            "volume_uuid": volume.uuid,
            "pages": pages,
        }
        target = Path(volume.dir) / f"{volume.title.name}.mokuro"
        target.write_text(json.dumps(payload), encoding="utf-8")
        self._holder["path"] = target


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    sessions_dir = work / ".bridge_sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(api, "WORK_DIR", work)
    monkeypatch.setattr(api, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(sessions, "WORK_DIR", work)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(api, "_remember_local_dir", lambda value: None)
    monkeypatch.setattr(api, "_load_remembered_local_dir", lambda: "")
    monkeypatch.setattr(api, "_remember_upload_method", lambda method: None)

    holder: dict = {}
    stubs = SimpleNamespace(
        Volume=lambda path: _Volume(path),
        Title=lambda path: _Title(path),
        MokuroGenerator=_Generator(holder),
    )
    monkeypatch.setattr(api, "_mokuro_submodule", lambda name: stubs)
    monkeypatch.setattr(api, "wait_for_session_ocr", lambda session: None)
    monkeypatch.setattr(api, "_ensure_ocr_worker", lambda: None)
    monkeypatch.setattr(api, "_sync_ocr_cache_state", lambda session: (0, 0))
    monkeypatch.setattr(api, "ocr_json_path", lambda volume, name: Path(volume.dir) / name)
    monkeypatch.setattr(api, "_safe_ocr_cache_path", lambda path, root: True)
    monkeypatch.setattr(api, "_valid_ocr_cache", lambda path, page: True)
    monkeypatch.setattr(
        api,
        "cleanup_volume_artifacts",
        lambda vol_dir: shutil.rmtree(vol_dir, ignore_errors=True),
    )
    monkeypatch.setattr(
        api,
        "find_mokuro_output",
        lambda vol_dir: holder.get("path"),
    )
    monkeypatch.setattr(
        api, "_cover_replace", lambda source, destination: shutil.copyfile(source, destination)
    )

    def _queue(session, safe_name, *, _ocr_cv_held=False):
        with session.lock:
            session.pages_received.add(safe_name)
        return sessions.session_snapshot(session)

    monkeypatch.setattr(api, "_queue_page_ocr", _queue)

    with sessions._sessions_lock:
        sessions._sessions.clear()
    with TestClient(api.app) as client:
        yield SimpleNamespace(client=client, library=tmp_path / "library")
    with sessions._sessions_lock:
        sessions._sessions.clear()


def _capture_and_finalize(bridge, *, title: str, pages: int, local_dir: Path) -> str:
    start = bridge.client.post("/session/start", data={"title": title})
    assert start.status_code == 200, start.text
    session_id = start.json()["session_id"]
    for index in range(1, pages + 1):
        name = f"page_{index:04d}.webp"
        response = bridge.client.post(
            f"/session/{session_id}/page",
            data={"filename": name, "page_num": str(index)},
            files={"page": (name, PNG, "image/png")},
        )
        assert response.status_code == 200, response.text
    finalize = bridge.client.post(
        f"/session/{session_id}/finalize",
        data={
            "upload_method": "local",
            "local_dir": str(local_dir),
            "delete_after_upload": "true",
        },
    )
    assert finalize.status_code == 200, finalize.text
    return finalize.text


def _frames(stream_text: str) -> list[dict]:
    return [json.loads(line) for line in stream_text.splitlines() if line.strip()]


@pytest.fixture
def remote_bridge(bridge, monkeypatch):
    store: dict[str, bytes] = {}
    uploads: list[tuple[str, str, str]] = []

    monkeypatch.setattr(api, "MIN_PAGES_FOR_MEGA", 1)
    monkeypatch.setattr(api, "resolve_upload_method", lambda raw: "fake")
    monkeypatch.setattr(api, "method_root", lambda method: "/library")
    monkeypatch.setattr(api, "method_label", lambda method: "Fake Cloud")
    monkeypatch.setattr(api, "method_provider", lambda method: "fake")

    def upload_file(method, local_path, remote_dir, on_progress, overwrite="fail"):
        name = Path(local_path).name
        if name in store and overwrite not in ("overwrite", "skip", "replace"):
            return False, f"destination already exists: {remote_dir}/{name}", None
        if name in store and overwrite == "skip":
            return True, None, f"https://fake.invalid/{name}"
        uploads.append((name, remote_dir, overwrite))
        store[name] = Path(local_path).read_bytes()
        return True, None, f"https://fake.invalid/{name}"

    def download_file(method, remote_dir, filename):
        return store.get(filename)

    monkeypatch.setattr(api, "upload_file", upload_file)
    monkeypatch.setattr(api, "download_file", download_file)
    bridge.store = store
    bridge.uploads = uploads
    return bridge


def _capture_and_finalize_remote(bridge, *, title: str, pages: int) -> str:
    start = bridge.client.post("/session/start", data={"title": title})
    assert start.status_code == 200, start.text
    session_id = start.json()["session_id"]
    for index in range(1, pages + 1):
        name = f"page_{index:04d}.webp"
        response = bridge.client.post(
            f"/session/{session_id}/page",
            data={"filename": name, "page_num": str(index)},
            files={"page": (name, PNG, "image/png")},
        )
        assert response.status_code == 200, response.text
    finalize = bridge.client.post(
        f"/session/{session_id}/finalize",
        data={
            "upload_method": "fake",
            "delete_after_upload": "true",
        },
    )
    assert finalize.status_code == 200, finalize.text
    return finalize.text


def test_remote_finalize_publishes_the_manifest_after_the_volume(remote_bridge):
    stream = _capture_and_finalize_remote(remote_bridge, title="Sample Series 1", pages=2)
    frames = _frames(stream)
    assert frames[-1]["status"] == "success", stream
    assert remote_bridge.uploads == [
        ("Sample Series 1.webp", "/library/Sample Series", "fail"),
        ("Sample Series 1.cbz", "/library/Sample Series", "fail"),
        ("Sample Series 1.mokuro", "/library/Sample Series", "fail"),
        (series_json.SERIES_FILE_NAME, "/library/Sample Series", "replace"),
    ]
    manifest = json.loads(
        remote_bridge.store[series_json.SERIES_FILE_NAME].decode("utf-8")
    )
    assert manifest["series_title"] == "Sample Series"
    assert [entry["volume_title"] for entry in manifest["volumes"]] == ["Sample Series 1"]
    assert frames[-1]["uploads"][-1]["file"] == series_json.SERIES_FILE_NAME


def test_remote_manifest_is_merges_the_existing_remote_copy(remote_bridge):
    _capture_and_finalize_remote(remote_bridge, title="Sample Series 1", pages=1)
    stream = _capture_and_finalize_remote(remote_bridge, title="Sample Series 2", pages=1)
    assert _frames(stream)[-1]["status"] == "success", stream
    manifest = json.loads(
        remote_bridge.store[series_json.SERIES_FILE_NAME].decode("utf-8")
    )
    assert [entry["volume_title"] for entry in manifest["volumes"]] == [
        "Sample Series 1",
        "Sample Series 2",
    ]


def test_remote_finalize_fails_loudly_when_the_manifest_cannot_be_read(
    remote_bridge, monkeypatch
):
    _capture_and_finalize_remote(remote_bridge, title="Sample Series 1", pages=1)
    remote_bridge.store["series.json"] = b'{"version":2,"series_title":"Sample Series",' \
                                         b'"updated_at":"2024-01-01T00:00:00.000Z","volumes":[]}'
    before = remote_bridge.store["series.json"]

    def broken_download(method, remote_dir, filename):
        raise RuntimeError("provider read failed")

    monkeypatch.setattr(api, "download_file", broken_download)
    stream = _capture_and_finalize_remote(remote_bridge, title="Sample Series 2", pages=1)
    frames = _frames(stream)
    assert frames[-1]["stage"] == "done"
    assert frames[-1]["status"] == "partial_upload"
    assert series_json.SERIES_FILE_NAME in frames[-1]["message"]
    assert remote_bridge.store["series.json"] == before


def test_a_failed_manifest_upload_puts_the_previous_index_back(
    remote_bridge, monkeypatch
):
    _capture_and_finalize_remote(remote_bridge, title="Sample Series 1", pages=1)
    before = remote_bridge.store["series.json"]
    real_upload = api.upload_file
    attempts: list[tuple[str, str]] = []

    def flaky_upload(method, local_path, remote_dir, on_progress, overwrite="fail"):
        name = Path(local_path).name
        if name == series_json.SERIES_FILE_NAME:
            attempts.append((name, overwrite))
            if len(attempts) == 1:
                remote_bridge.store.pop(series_json.SERIES_FILE_NAME, None)
                return False, "MEGA: transfer failed", None
        return real_upload(method, local_path, remote_dir, on_progress, overwrite)

    monkeypatch.setattr(api, "upload_file", flaky_upload)
    stream = _capture_and_finalize_remote(remote_bridge, title="Sample Series 2", pages=1)
    frames = _frames(stream)
    assert frames[-1]["status"] == "partial_upload"
    assert "put back" in frames[-1]["message"], stream
    assert remote_bridge.store["series.json"] == before
    assert [name for name, _ in attempts] == [series_json.SERIES_FILE_NAME] * 2


def test_local_finalize_writes_a_series_json_beside_the_volume(bridge):
    stream = _capture_and_finalize(
        bridge, title="Sample Series 1", pages=2, local_dir=bridge.library
    )
    frames = _frames(stream)
    stages = [frame["stage"] for frame in frames]
    assert "series_json" in stages, stream
    terminal = frames[-1]
    assert terminal["stage"] == "done", stream
    assert terminal["status"] == "success", stream
    assert any(
        entry["file"] == series_json.SERIES_FILE_NAME
        for entry in terminal["uploads"]
    ), terminal

    series_dir = bridge.library / "Sample Series"
    assert sorted(path.name for path in series_dir.iterdir()) == [
        "Sample Series 1.cbz",
        "Sample Series 1.mokuro",
        "Sample Series 1.webp",
        series_json.SERIES_FILE_NAME,
    ]
    manifest = json.loads((series_dir / series_json.SERIES_FILE_NAME).read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["series_title"] == "Sample Series"
    assert manifest["updated_at"] == series_json.FACTLESS_UPDATED_AT
    assert manifest["external_ids"] == {}
    assert len(manifest["volumes"]) == 1
    entry = manifest["volumes"][0]
    assert entry["volume_title"] == "Sample Series 1"
    assert entry["page_count"] == 2
    assert entry["character_count"] == 16  # "日本語のテキスト" is 8, and only ASCII is free
    assert entry["mokuro_version"] == "0.2.2"
    assert entry["archive_size"] > 0
    assert entry["mokuro_size"] > 0
    assert len(entry["mokuro_sha256"]) == 64
    mokuro = json.loads((series_dir / "Sample Series 1.mokuro").read_text(encoding="utf-8"))
    assert entry["volume_uuid"] == mokuro["volume_uuid"]


def test_a_second_volume_is_appended_and_the_first_survives(bridge):
    _capture_and_finalize(
        bridge, title="Sample Series 1", pages=1, local_dir=bridge.library
    )
    stream = _capture_and_finalize(
        bridge, title="Sample Series 2", pages=3, local_dir=bridge.library
    )
    assert _frames(stream)[-1]["status"] == "success", stream

    series_dir = bridge.library / "Sample Series"
    manifest = json.loads((series_dir / series_json.SERIES_FILE_NAME).read_text(encoding="utf-8"))
    assert [entry["volume_title"] for entry in manifest["volumes"]] == [
        "Sample Series 1",
        "Sample Series 2",
    ]
    assert manifest["volumes"][1]["page_count"] == 3
    assert manifest["volumes"][0]["page_count"] == 1
    assert sorted(path.name for path in series_dir.iterdir()) == [
        "Sample Series 1.cbz",
        "Sample Series 1.mokuro",
        "Sample Series 1.webp",
        "Sample Series 2.cbz",
        "Sample Series 2.mokuro",
        "Sample Series 2.webp",
        series_json.SERIES_FILE_NAME,
    ]


def test_re_publishing_an_unchanged_volume_does_not_rewrite_the_manifest(bridge):
    _capture_and_finalize(
        bridge, title="Sample Series 1", pages=1, local_dir=bridge.library
    )
    manifest_path = bridge.library / "Sample Series" / series_json.SERIES_FILE_NAME
    before = manifest_path.read_bytes()
    before_mtime = manifest_path.stat().st_mtime_ns

    stream = _capture_and_finalize(
        bridge, title="Sample Series 1", pages=1, local_dir=bridge.library
    )
    frames = _frames(stream)
    assert manifest_path.read_bytes() == before
    assert manifest_path.stat().st_mtime_ns == before_mtime
    assert any(
        frame["stage"] == "series_json" and frame.get("unchanged") for frame in frames
    ), stream


def test_a_corrupt_existing_manifest_is_revived_not_replaced(bridge):
    _capture_and_finalize(
        bridge, title="Sample Series 1", pages=1, local_dir=bridge.library
    )
    manifest_path = bridge.library / "Sample Series" / series_json.SERIES_FILE_NAME
    broken = {
        "version": 2,
        "series_title": "Sample Series",
        "updated_at": "whenever",
        "titles": {"english": "Sample Series"},
        "volumes": [
            {
                "volume_uuid": "foreign",
                "volume_title": "Other Volume",
                "page_count": 7,
                "character_count": 70,
                "mokuro_version": "0.2.2",
            }
        ],
    }
    manifest_path.write_text(json.dumps(broken), encoding="utf-8")

    stream = _capture_and_finalize(
        bridge, title="Sample Series 2", pages=1, local_dir=bridge.library
    )
    assert _frames(stream)[-1]["status"] == "success", stream
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["updated_at"] == series_json.FACTLESS_UPDATED_AT
    assert manifest["titles"] == {"english": "Sample Series"}
    assert [entry["volume_title"] for entry in manifest["volumes"]] == [
        "Other Volume",
        "Sample Series 2",
    ]


def test_a_bunko_destination_skips_the_series_json_write(remote_bridge, monkeypatch):
    """The destination compiles the manifest, so finalize uploads only the volume."""
    monkeypatch.setattr(api, "metadata_is_external", lambda method: True)

    stream = _capture_and_finalize_remote(remote_bridge, title="Sample Series 1", pages=2)
    frames = _frames(stream)

    assert frames[-1]["status"] == "success", stream
    assert remote_bridge.uploads == [
        ("Sample Series 1.webp", "/library/Sample Series", "fail"),
        ("Sample Series 1.mokuro", "/library/Sample Series", "fail"),
        ("Sample Series 1.cbz", "/library/Sample Series", "fail"),
    ]
    assert series_json.SERIES_FILE_NAME not in remote_bridge.store
    external = [
        frame
        for frame in frames
        if frame.get("stage") == "series_json" and frame.get("external")
    ]
    assert external, stream
    assert external[0]["ok"] is True
    assert external[0]["managed_by"] == "mokuro-bunko"


def test_a_destination_that_does_not_own_the_manifest_still_gets_it(
    remote_bridge, monkeypatch
):
    monkeypatch.setattr(api, "metadata_is_external", lambda method: False)

    stream = _capture_and_finalize_remote(remote_bridge, title="Sample Series 1", pages=2)

    assert _frames(stream)[-1]["status"] == "success", stream
    assert [name for name, _dir, _overwrite in remote_bridge.uploads] == [
        "Sample Series 1.webp",
        "Sample Series 1.cbz",
        "Sample Series 1.mokuro",
        series_json.SERIES_FILE_NAME,
    ]
    assert series_json.SERIES_FILE_NAME in remote_bridge.store
