"""The configuration API and the dashboard page it serves."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import accounts, api, config_api, providers, sessions, settings

REMOTE_ROOT = "/Root/mokuro-reader"


@pytest.fixture
def client(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    sessions_dir = work / ".bridge_sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(api, "WORK_DIR", work)
    monkeypatch.setattr(api, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(sessions, "WORK_DIR", work)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "library")
    monkeypatch.setattr(api, "_LOCAL_INGEST_ROOTS", [tmp_path.resolve()])
    monkeypatch.setattr(config_api, "OUTPUT_DIR", tmp_path / "library")
    monkeypatch.setattr(config_api, "WORK_DIR", work)
    monkeypatch.setattr(config_api, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(config_api, "_LOCAL_INGEST_ROOTS", [tmp_path.resolve()])
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(providers, "_UPLOAD_METHOD_STATE_FILE", tmp_path / "default.json")
    monkeypatch.setattr(providers, "_load_remembered_local_dir", lambda: str(tmp_path / "library"))
    monkeypatch.setattr(providers, "method_root", lambda method: REMOTE_ROOT)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.delenv("MOKURO_BRIDGE_UPLOAD_DEFAULT", raising=False)
    with TestClient(api.app) as test_client:
        yield test_client


@pytest.fixture
def remote_listing(monkeypatch):
    calls: list[tuple[str, str]] = []

    def list_remote_folders(method: str, remote_dir: str) -> list[dict]:
        calls.append((method, remote_dir))
        return [
            {"name": "Series One", "path": f"{remote_dir}/Series One", "type": "folder"},
            {"name": "volume.cbz", "path": f"{remote_dir}/volume.cbz", "type": "file", "size": 12},
        ]

    monkeypatch.setattr(providers, "list_remote_folders", list_remote_folders)
    return calls


def _make_account(provider: str = "drive", name: str = "work") -> None:
    accounts.save_instance(provider, name, label=f"{provider} {name}", root="mokuro-reader")


def test_dashboard_serves_the_page_with_the_version_substituted(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "mokuro-bridge dashboard" in body
    assert "{{" not in body, "a template placeholder was left unreplaced"
    assert "Destinations" in body and "Settings" in body
    assert "/config/browse" in body


def test_config_lists_methods_settings_and_paths(client):
    _make_account()
    body = client.get("/config").json()
    ids = [row["id"] for row in body["methods"]]
    assert ids[0] == "local" and "drive:work" in ids
    assert body["default_method"] == "local"
    assert body["writable"] is True
    assert len(body["settings"]) == len(settings.SPECS)
    assert body["settings_file"].endswith("settings.json")
    assert all(
        {"source", "restart", "default", "display"} <= set(row) for row in body["settings"]
    )
    roots = next(r for r in body["settings"] if r["env"] == "MOKURO_BRIDGE_INGEST_ROOTS")
    assert roots["value"] == "" and roots["display"] == "(none)", "an empty value still reads as unset"
    assert body["methods"][0]["provider"] == "local"


def test_local_browse_lists_directories_under_an_allowed_root(client, tmp_path):
    (tmp_path / "series-a").mkdir()
    (tmp_path / "series-b").mkdir()
    (tmp_path / "volume.cbz").write_bytes(b"x")
    body = client.get("/config/browse", params={"method": "local", "path": str(tmp_path)}).json()
    names = [entry["name"] for entry in body["entries"]]
    assert {"series-a", "series-b"} <= set(names)
    assert "volume.cbz" not in names, "files are not offered as output folders"
    assert body["path"] == str(tmp_path.resolve())


def test_local_browse_refuses_a_path_outside_the_allowed_roots(client):
    response = client.get("/config/browse", params={"method": "local", "path": "/etc"})
    assert response.status_code == 400
    assert "local_dir" in response.json()["detail"]


def test_remote_browse_goes_through_the_provider(client, remote_listing):
    _make_account()
    body = client.get(
        "/config/browse", params={"method": "drive:work", "path": f"{REMOTE_ROOT}/library"}
    ).json()
    assert remote_listing == [("drive:work", f"{REMOTE_ROOT}/library")]
    assert [entry["name"] for entry in body["entries"]] == ["Series One", "volume.cbz"]
    assert body["root"] == REMOTE_ROOT


def test_remote_browse_reports_a_provider_failure_as_502(client, monkeypatch):
    _make_account()

    def explode(method, remote_dir):
        raise RuntimeError("listing failed for https://graph.microsoft.com/x?token=SECRET")

    monkeypatch.setattr(providers, "list_remote_folders", explode)
    response = client.get("/config/browse", params={"method": "drive:work", "path": REMOTE_ROOT})
    assert response.status_code == 502
    assert "SECRET" not in response.text


def test_setting_the_default_method_is_remembered(client, monkeypatch):
    _make_account()
    monkeypatch.setattr(providers, "_method_configured", lambda method: True)
    response = client.post("/config/default", data={"method": "drive:work"})
    assert response.json() == {"ok": True, "default_method": "drive:work"}
    assert client.get("/config").json()["default_method"] == "drive:work"


def test_an_unconfigured_destination_cannot_become_the_default(client):
    _make_account()
    response = client.post("/config/default", data={"method": "drive:work"})
    assert response.status_code == 409
    assert "not configured" in response.json()["detail"]


def test_setting_the_default_rejects_an_unknown_method(client):
    response = client.post("/config/default", data={"method": "drive:nope"})
    assert response.status_code == 400
    assert "unknown upload account" in response.json()["detail"]


def test_saving_an_account_root_and_label_persists(client):
    _make_account()
    response = client.post(
        "/config/account",
        data={"method": "drive:work", "root": "mokuro-reader/library", "label": "Drive work"},
    )
    assert response.json()["root"] == "mokuro-reader/library"
    instance = accounts.load_instance("drive", "work")
    assert instance.root == "mokuro-reader/library"
    assert instance.label == "Drive work"


def test_saving_an_account_root_rejects_a_non_portable_path(client):
    _make_account()
    response = client.post("/config/account", data={"method": "drive:work", "root": "../escape"})
    assert response.status_code == 400


def test_saving_settings_persists_and_reports_restart(client):
    response = client.post(
        "/config/settings",
        json={"MOKURO_BRIDGE_FETCH_PORTS": 12, "OCR_CHUNK_SIZE": 4},
    )
    body = response.json()
    assert body["ok"] is True
    assert body["saved"] == {"MOKURO_BRIDGE_FETCH_PORTS": 12, "OCR_CHUNK_SIZE": 4}
    assert body["restart_command"] == config_api._restart_command()
    stored = json.loads(settings.SETTINGS_FILE.read_text())
    assert stored["MOKURO_BRIDGE_FETCH_PORTS"] == 12


def test_saving_settings_reports_a_problem_without_writing(client):
    response = client.post("/config/settings", json={"MOKURO_BRIDGE_FETCH_PORTS": 9999})
    assert response.status_code == 400
    assert "between 0 and 256" in response.json()["problems"][0]
    assert not settings.SETTINGS_FILE.exists()


def test_saving_settings_rejects_an_unknown_key(client):
    response = client.post("/config/settings", json={"NOT_A_SETTING": 1})
    assert response.status_code == 400
    assert "unknown setting" in response.json()["problems"][0]


def test_local_output_and_new_folder_round_trip(client, tmp_path):
    target = tmp_path / "library"
    response = client.post("/config/local-output", data={"path": str(target)})
    assert response.json()["ok"] is True
    created = client.post(
        "/config/new-folder", data={"parent": str(target), "name": "manga"}
    )
    assert created.json()["path"] == str(target / "manga")
    assert (target / "manga").is_dir()


def test_new_folder_refuses_a_nested_name(client, tmp_path):
    response = client.post(
        "/config/new-folder", data={"parent": str(tmp_path), "name": "a/b"}
    )
    assert response.status_code == 400


def test_forget_removes_an_account(client):
    _make_account()
    response = client.post("/config/forget", data={"method": "drive:work"})
    assert response.json()["ok"] is True
    assert accounts.load_instance("drive", "work") is None


def test_forget_refuses_the_local_target(client):
    response = client.post("/config/forget", data={"method": "local"})
    assert response.status_code == 400


@pytest.mark.parametrize(
    "route,payload",
    [
        ("/config/default", {"method": "local"}),
        ("/config/account", {"method": "drive", "root": "mokuro-reader"}),
        ("/config/local-output", {"path": "/tmp"}),
        ("/config/new-folder", {"parent": "/tmp", "name": "x"}),
        ("/config/forget", {"method": "drive:work"}),
    ],
)
def test_writes_from_a_foreign_origin_are_refused(client, route, payload):
    response = client.post(route, data=payload, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "only from this machine" in response.json()["detail"]


def test_saves_from_the_dashboard_origin_are_allowed(client, tmp_path):
    response = client.post(
        "/config/local-output",
        data={"path": str(tmp_path / "library")},
        headers={"Origin": "http://127.0.0.1:62642"},
    )
    assert response.json()["ok"] is True


def test_reads_are_not_origin_gated(client):
    response = client.get("/config", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


def test_dashboard_404_when_the_page_is_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config_api, "DASHBOARD_HTML", tmp_path / "missing.html")
    monkeypatch.setattr(config_api, "_html_cache", None)
    response = client.get("/")
    assert response.status_code == 500
    assert "dashboard page missing" in response.json()["detail"]
