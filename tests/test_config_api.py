"""The configuration API and the dashboard page it serves."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import accounts, api, config_api, providers, sessions, settings
from mokuro_bridge.providers import drive as drive_provider
from mokuro_bridge.providers import mega as mega_provider
from mokuro_bridge.providers import onedrive as onedrive_provider

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
    # Identity derivation reads credential stores; keep it inside tmp_path and
    # never touch the real keychain or ~/.config from a test.
    monkeypatch.setattr(mega_provider, "MEGA_CREDS_FILE", tmp_path / "mega_credentials.env")
    monkeypatch.setattr(mega_provider, "_keychain_mega_account", lambda: None)
    monkeypatch.setattr(drive_provider, "DRIVE_CREDS_FILE", tmp_path / "drive_credentials.json")
    monkeypatch.setattr(onedrive_provider, "ONEDRIVE_TOKEN_FILE", tmp_path / "onedrive_token.json")
    monkeypatch.delenv("MEGA_EMAIL", raising=False)
    monkeypatch.delenv("MEGA_PASSWORD", raising=False)
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
    assert ids[0] == "local" and "drive-work" in ids
    assert body["default_method"] == "local"
    assert body["writable"] is True
    assert len(body["settings"]) == len(settings.SPECS)
    assert body["settings_file"].endswith("settings.json")
    assert all(
        {"source", "restart", "default", "display", "pinned", "env_set", "file_value"}
        <= set(row)
        for row in body["settings"]
    )
    roots = next(r for r in body["settings"] if r["env"] == "MOKURO_BRIDGE_INGEST_ROOTS")
    assert roots["value"] == "" and roots["display"] == "(none)", "an empty value still reads as unset"
    assert body["methods"][0]["provider"] == "local"
    local_row = body["methods"][0]
    assert local_row["alias"] == "local" and local_row["state"] == "connected"
    assert local_row["needs_reauth"] is False

def test_config_describes_every_provider_for_the_ui(client):
    body = client.get("/config").json()
    providers = {row["id"]: row for row in body["providers"]}
    assert set(providers) == {"mega", "drive", "onedrive", "webdav", "rclone"}
    for row in providers.values():
        assert {"id", "label", "available", "browser_setup", "setup_hint",
                "missing", "alias_suggestion"} <= set(row)
        assert isinstance(row["missing"], list) and isinstance(row["available"], bool)
        assert row["alias_suggestion"] in {row["id"], f"{row['id']}-2", f"{row['id']}-3"}
    assert providers["mega"]["browser_setup"] == "form"
    assert providers["drive"]["browser_setup"] == "oauth"
    assert providers["onedrive"]["browser_setup"] == "device_code"
    assert providers["onedrive"]["setup_hint"] == "mokuro-bridge --setup-upload onedrive"
    assert providers["webdav"]["browser_setup"] == "form"
    assert providers["webdav"]["setup_hint"] == "mokuro-bridge --setup-upload webdav"

def test_config_methods_carry_alias_provider_and_state(client):
    _make_account()
    methods = {row["id"]: row for row in client.get("/config").json()["methods"]}
    work = methods["drive-work"]
    assert work["alias"] == "drive-work"
    assert work["provider"] == "drive"
    assert work["provider_label"] == "Google Drive"
    assert work["account"] == "work"
    assert work["needs_reauth"] is False, "no credential material exists yet"
    assert work["state"] == "not_configured", "an account with no creds is not connected"
    assert {"configured", "default", "root", "current_folder", "creds_source",
            "label", "name", "extra"} <= set(work)


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
    assert remote_listing == [("drive-work", f"{REMOTE_ROOT}/library")], "aliases hit the provider"
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
    assert response.json() == {"ok": True, "default_method": "drive-work"}
    assert client.get("/config").json()["default_method"] == "drive-work"


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


def test_saving_settings_persists_and_reports_restart(client, tmp_path):
    manga = tmp_path / "manga"
    manga.mkdir()
    response = client.post(
        "/config/settings",
        json={"MOKURO_BRIDGE_FETCH_PORTS": 12, "MOKURO_BRIDGE_INGEST_ROOTS": str(manga)},
    )
    body = response.json()
    assert body["ok"] is True
    assert body["saved"] == {
        "MOKURO_BRIDGE_FETCH_PORTS": 12,
        "MOKURO_BRIDGE_INGEST_ROOTS": str(manga),
    }
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


def test_dashboard_is_read_from_disk_on_every_request(client, monkeypatch, tmp_path):
    page = tmp_path / "dashboard.html"
    page.write_text("<html><body>{{APP}} one</body></html>", encoding="utf-8")
    monkeypatch.setattr(config_api, "DASHBOARD_HTML", page)
    assert "one" in client.get("/").text
    page.write_text("<html><body>{{APP}} two</body></html>", encoding="utf-8")
    assert "two" in client.get("/").text, "editing the page must not need a restart"

def test_renaming_an_alias_keeps_root_and_legacy_spelling(client):
    _make_account()
    response = client.post(
        "/config/accounts/update",
        data={"method": "drive:work", "alias": "drive-next", "label": "Drive next"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["method"] == "drive-next"
    renamed = accounts.load_instance("drive", "work")
    assert renamed.id == "drive-next"
    assert renamed.root == "mokuro-reader", "the root is preserved"
    assert renamed.label == "Drive next"
    # Both the new alias and the historical id still resolve.
    assert client.post("/config/account", data={"method": "drive:work", "root": "x"}).status_code == 200
    assert client.post("/config/account", data={"method": "drive-next", "label": "again"}).status_code == 200

def test_renaming_onto_a_taken_alias_is_409(client):
    _make_account("drive", "one")
    _make_account("drive", "two")
    accounts.save_instance("drive", "one", alias="drive-one")
    accounts.save_instance("drive", "two", alias="drive-two")
    response = client.post(
        "/config/accounts/update", data={"method": "drive-one", "alias": "drive-two"}
    )
    assert response.status_code == 409
    assert "already used" in response.json()["detail"]

def test_renaming_onto_a_derived_legacy_alias_is_409(client):
    _make_account("drive", "main")  # derived alias: drive-main
    _make_account("drive", "work")
    accounts.save_instance("drive", "work", alias="drive-work")
    response = client.post(
        "/config/accounts/update", data={"method": "drive-work", "alias": "drive-main"}
    )
    assert response.status_code == 409
    assert "already used" in response.json()["detail"]


def test_relabelling_leaves_the_alias_alone(client):
    _make_account()
    response = client.post(
        "/config/accounts/update", data={"method": "drive:work", "label": ""}
    )
    assert response.status_code == 200
    assert response.json()["method"] == "drive-work"
    assert response.json()["label"] == ""
    assert accounts.load_instance("drive", "work").id == "drive-work"

def test_renaming_the_default_moves_the_sticky_default_with_it(client, monkeypatch):
    _make_account()
    accounts.save_instance("drive", "work", alias="drive-work")
    monkeypatch.setattr(providers, "_method_configured", lambda method: True)
    assert client.post("/config/default", data={"method": "drive-work"}).status_code == 200
    response = client.post(
        "/config/accounts/update", data={"method": "drive-work", "alias": "drive-beta"}
    )
    assert response.status_code == 200
    assert response.json()["default_method"] == "drive-beta"
    assert client.get("/config").json()["default_method"] == "drive-beta"

def test_forget_accepts_an_alias(client):
    _make_account()
    accounts.save_instance("drive", "work", alias="drive-work")
    response = client.post("/config/forget", data={"method": "drive-work"})
    assert response.json()["ok"] is True
    assert accounts.load_instance("drive", "work") is None

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
    response = client.get("/")
    assert response.status_code == 500
    assert "dashboard page missing" in response.json()["detail"]


# --- locally derivable account identity (extra.email) ----------------------

def _methods_by_id(client) -> dict:
    return {row["id"]: row for row in client.get("/config").json()["methods"]}


def test_config_reports_the_mega_username_from_the_legacy_creds_file(client):
    mega_provider.MEGA_CREDS_FILE.write_text(
        "# mokuro-bridge MEGA credentials\nMEGA_EMAIL=me@example.com\nMEGA_PASSWORD=topsecret\n",
        encoding="utf-8",
    )
    row = _methods_by_id(client)["mega"]
    assert row["extra"]["email"] == "me@example.com"
    assert "topsecret" not in client.get("/config").text

def test_config_prefers_mega_account_metadata_over_the_credentials_file(client):
    mega_provider.MEGA_CREDS_FILE.write_text(
        "MEGA_EMAIL=file@example.com\nMEGA_PASSWORD=topsecret\n", encoding="utf-8"
    )
    accounts.save_instance("mega", "work", extra={"email": "meta@example.com"})
    rows = _methods_by_id(client)
    assert rows["mega-work"]["extra"]["email"] == "meta@example.com"
    # The legacy default still falls back to the file.
    assert rows["mega"]["extra"]["email"] == "file@example.com"

def test_config_reports_a_named_megas_username_from_its_own_creds_file(client):
    accounts.save_instance("mega", "mega-2", alias="mega-2")
    accounts.secret_path("mega", "mega-2", "credentials.env").write_text(
        "MEGA_EMAIL=named@example.com\nMEGA_PASSWORD=topsecret\n", encoding="utf-8"
    )
    row = _methods_by_id(client)["mega-2"]
    assert row["extra"]["email"] == "named@example.com"
    assert "topsecret" not in client.get("/config").text

def test_config_reports_the_onedrive_username_from_the_token_cache(client):
    onedrive_provider.ONEDRIVE_TOKEN_FILE.write_text(
        json.dumps(
            {
                "Account": {
                    "uid.utid-login.microsoftonline.com": {
                        "home_account_id": "uid.utid",
                        "username": "uni@example.com",
                    }
                },
                "AccessToken": {"k": {"secret": "ACCESS-TOKEN-SECRET"}},
                "RefreshToken": {"k": {"secret": "REFRESH-TOKEN-SECRET"}},
            }
        ),
        encoding="utf-8",
    )
    response = client.get("/config")
    row = {row["id"]: row for row in response.json()["methods"]}["onedrive"]
    assert row["extra"]["email"] == "uni@example.com"
    assert "ACCESS-TOKEN-SECRET" not in response.text
    assert "REFRESH-TOKEN-SECRET" not in response.text

def test_config_falls_back_to_the_onedrive_id_token_username(client):
    onedrive_provider.ONEDRIVE_TOKEN_FILE.write_text(
        json.dumps(
            {
                "Account": {"uid.utid": {"home_account_id": "uid.utid"}},
                "IdToken": {"k": {"preferred_username": "school@example.com"}},
            }
        ),
        encoding="utf-8",
    )
    assert _methods_by_id(client)["onedrive"]["extra"]["email"] == "school@example.com"

def test_config_tolerates_a_corrupt_onedrive_cache(client):
    onedrive_provider.ONEDRIVE_TOKEN_FILE.write_text("{not json", encoding="utf-8")
    response = client.get("/config")
    assert response.status_code == 200
    assert "email" not in _methods_by_id(client)["onedrive"]["extra"]

def test_config_reports_a_drive_email_only_when_the_file_already_has_one(client):
    _make_account()
    drive_provider.DRIVE_CREDS_FILE.write_text(
        json.dumps({"refresh_token": "REFRESH-TOKEN-SECRET", "account": "me@example.com"}),
        encoding="utf-8",
    )
    response = client.get("/config")
    row = {row["id"]: row for row in response.json()["methods"]}["drive"]
    assert row["extra"]["email"] == "me@example.com"
    assert "REFRESH-TOKEN-SECRET" not in response.text

def test_config_omits_a_drive_email_that_is_not_recorded(client):
    _make_account()
    drive_provider.DRIVE_CREDS_FILE.write_text(
        json.dumps({"refresh_token": "REFRESH-TOKEN-SECRET", "account": ""}),
        encoding="utf-8",
    )
    response = client.get("/config")
    row = {row["id"]: row for row in response.json()["methods"]}["drive"]
    assert "email" not in row["extra"], "Drive does not invent an identity"
    assert "REFRESH-TOKEN-SECRET" not in response.text

def test_config_without_any_identity_source_omits_email_and_does_not_crash(client):
    response = client.get("/config")
    assert response.status_code == 200
    methods = {row["id"]: row for row in response.json()["methods"]}
    assert "email" not in methods["mega"]["extra"]
    assert "email" not in methods["drive"]["extra"]
    assert "email" not in methods["onedrive"]["extra"]
