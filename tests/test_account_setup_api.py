"""Browser account setup: MEGA form, OneDrive device code, Google OAuth.

Hermetic: no network, no keychain, no real `~/.config/mokuro-bridge`. Every
provider seam (megatools verify/store, msal flow, Google flow and client check)
is replaced with a double, and secret paths point into ``tmp_path``.
"""

from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import accounts, api, config_api, providers, sessions, settings
from mokuro_bridge.providers import drive as drive_provider
from mokuro_bridge.providers import mega as mega_provider
from mokuro_bridge.providers import onedrive as onedrive_provider


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
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.delenv("MOKURO_BRIDGE_UPLOAD_DEFAULT", raising=False)
    monkeypatch.delenv("MEGA_EMAIL", raising=False)
    monkeypatch.delenv("MEGA_PASSWORD", raising=False)
    for name in ("DRIVE_CLIENT_ID", "DRIVE_CLIENT_SECRET", "DRIVE_CLIENT_SECRET_FILE"):
        monkeypatch.delenv(name, raising=False)

    # Secret stores: everything writes under tmp_path, nothing touches the keychain.
    monkeypatch.setattr(mega_provider, "MEGA_CREDS_FILE", tmp_path / "mega_credentials.env")
    monkeypatch.setattr(mega_provider, "WORK_DIR", work)
    monkeypatch.setattr(drive_provider, "DRIVE_CREDS_FILE", tmp_path / "drive_credentials.json")
    monkeypatch.setattr(onedrive_provider, "ONEDRIVE_TOKEN_FILE", tmp_path / "onedrive_token.json")
    monkeypatch.setattr(onedrive_provider, "ONEDRIVE_CLIENT_ID", "test-client-id")

    # Optional libraries are absent in the test environment; the page-visible
    # capability flags are what the routes consult.
    monkeypatch.setattr(config_api, "_module_available", lambda module: True)
    monkeypatch.setattr(config_api.shutil, "which", lambda name: f"/usr/bin/{name}")

    monkeypatch.setattr(mega_provider, "_keychain_mega_account", lambda: None)
    monkeypatch.setattr(mega_provider, "_mega_verify_creds", lambda email, password: (True, None))
    monkeypatch.setattr(mega_provider, "_store_mega_creds_os", _no_os_store)
    monkeypatch.setattr(mega_provider, "_keychain_mega_account", lambda: None)
    # The MEGA credential source check would probe the OS store; report "none"
    # so the store helper writes its 0600 fallback file instead.
    monkeypatch.setattr(mega_provider, "_mega_creds_source", lambda name: None)
    monkeypatch.setattr(drive_provider, "_drive_client_valid", lambda client_id, secret: True)

    monkeypatch.setattr(providers, "_method_configured", lambda method: True)
    with TestClient(api.app) as test_client:
        yield test_client


def _no_os_store(email: str, password: str, stale=None) -> str:
    raise RuntimeError("no OS credential store in this test")


def _wait_for_status(client, route: str, flow_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(route, params={"flow_id": flow_id}).json()
        if body.get("status") != "pending":
            return body
        time.sleep(0.01)
    return body


# --- MEGA ------------------------------------------------------------------

def test_mega_setup_adds_the_first_account_as_the_default(client):
    response = client.post(
        "/config/accounts/mega",
        data={"alias": "mega", "email": "me@example.com", "password": "secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["method"] == "mega"
    assert body["root"] == accounts.default_root("mega")
    instance = accounts.load_instance("mega")
    assert instance.tracked is True
    assert instance.email == "me@example.com"
    # No OS store in this environment: the 0600 fallback file was written.
    assert (mega_provider.MEGA_CREDS_FILE).is_file()
    assert oct(mega_provider.MEGA_CREDS_FILE.stat().st_mode & 0o777) == "0o600"

def test_mega_setup_adds_a_named_account_and_persists_its_alias(client):
    response = client.post(
        "/config/accounts/mega",
        data={
            "alias": "mega-beta",
            "label": "Backup",
            "email": "beta@example.com",
            "password": "secret",
            "root": "/Root/backup",
        },
    )
    assert response.status_code == 200
    assert response.json()["method"] == "mega-beta"
    assert response.json()["root"] == "/Root/backup"
    instance = accounts.load_instance("mega", "mega-beta")
    assert instance.id == "mega-beta"
    assert instance.label == "Backup"
    raw = json.loads(accounts.meta_path("mega", "mega-beta").read_text(encoding="utf-8"))
    assert raw["alias"] == "mega-beta"

def test_mega_setup_reconnecting_an_existing_account_keeps_its_settings(client):
    first = client.post(
        "/config/accounts/mega",
        data={
            "alias": "mega-beta",
            "label": "Beta",
            "email": "a@example.com",
            "password": "x",
            "root": "/Root/beta",
        },
    ).json()
    second = client.post(
        "/config/accounts/mega",
        data={"alias": "mega-beta", "email": "new@example.com", "password": "y"},
    )
    assert second.status_code == 200
    assert second.json()["method"] == "mega-beta"
    assert second.json()["root"] == first["root"], "reconnecting keeps the chosen folder"
    instance = accounts.load_instance("mega", "mega-beta")
    assert instance.email == "new@example.com", "the new credentials were stored"
    assert instance.label == "Beta"

def test_mega_setup_rejects_an_alias_of_another_provider(client):
    client.post(
        "/config/accounts/mega",
        data={"alias": "mega-beta", "email": "a@example.com", "password": "x"},
    )
    accounts.save_instance("drive", "personal", alias="personal")
    response = client.post(
        "/config/accounts/mega",
        data={"alias": "personal", "email": "b@example.com", "password": "x"},
    )
    assert response.status_code == 400
    assert "belongs to the drive provider" in response.json()["detail"]

def test_mega_setup_reports_bad_credentials_without_storing(client, monkeypatch):
    monkeypatch.setattr(
        mega_provider, "_mega_verify_creds", lambda email, password: (False, "Login failed for https://mega.nz/x?token=SECRET")
    )
    response = client.post(
        "/config/accounts/mega",
        data={"alias": "mega-2", "email": "a@example.com", "password": "wrong"},
    )
    assert response.status_code == 400
    assert "MEGA login failed" in response.json()["detail"]
    assert "SECRET" not in response.text
    assert accounts.load_instance("mega", "mega-2") is None

def test_mega_setup_explains_a_missing_megatools(client, monkeypatch):
    monkeypatch.setattr(config_api.shutil, "which", lambda name: None)
    response = client.post(
        "/config/accounts/mega",
        data={"alias": "mega-2", "email": "a@example.com", "password": "x"},
    )
    assert response.status_code == 400
    assert "megatools" in response.json()["detail"]

def test_mega_setup_refuses_to_shadow_a_legacy_default(client, monkeypatch):
    monkeypatch.setattr(mega_provider, "_mega_creds_source", lambda name: "file")
    response = client.post(
        "/config/accounts/mega",
        data={"alias": "mega", "email": "a@example.com", "password": "x"},
    )
    assert response.status_code == 400
    assert "already configured for the first account" in response.json()["detail"]


# --- OneDrive --------------------------------------------------------------

class _FakeMslApp:
    def __init__(self, result: dict, on_acquire=None):
        self.result = result
        self.on_acquire = on_acquire
        self.acquired = False

    def acquire_token_by_device_flow(self, flow):
        self.acquired = True
        if self.on_acquire:
            self.on_acquire()
        return self.result


DEVICE_FLOW = {
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://microsoft.com/devicelogin",
    "expires_in": 900,
    "interval": 5,
    "message": "To sign in, use code ABCD-EFGH",
}


def _install_device_flow(monkeypatch, result=None, on_acquire=None):
    app = _FakeMslApp(result if result is not None else {"access_token": "token"}, on_acquire)
    monkeypatch.setattr(
        onedrive_provider,
        "start_device_flow",
        lambda name: (app, object(), dict(DEVICE_FLOW)),
    )
    return app


def _fake_finish(app, cache, flow, name, *, alias=None, label=None, root=None):
    token = onedrive_provider._onedrive_token_path(name)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("{}", encoding="utf-8")
    return accounts.save_instance("onedrive", name, alias=alias, label=label)


def test_onedrive_start_returns_the_user_code(client, monkeypatch):
    _install_device_flow(monkeypatch)
    monkeypatch.setattr(onedrive_provider, "finish_device_flow", _fake_finish)
    response = client.post(
        "/config/accounts/onedrive/start", data={"alias": "onedrive-uni", "label": "Uni"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["user_code"] == "ABCD-EFGH"
    assert body["verification_uri"] == "https://microsoft.com/devicelogin"
    assert body["expires_in"] == 900 and body["interval_s"] == 5
    assert _wait_for_status(client, "/config/accounts/onedrive/status", body["flow_id"])["status"] == "complete"

def test_onedrive_completion_saves_the_account(client, monkeypatch):
    _install_device_flow(monkeypatch)
    monkeypatch.setattr(onedrive_provider, "finish_device_flow", _fake_finish)
    started = client.post(
        "/config/accounts/onedrive/start", data={"alias": "onedrive-uni"}
    ).json()
    status = _wait_for_status(client, "/config/accounts/onedrive/status", started["flow_id"])
    assert status == {"status": "complete", "method": "onedrive-uni"}
    instance = accounts.load_instance("onedrive", "onedrive-uni")
    assert instance.id == "onedrive-uni"

def test_onedrive_failure_is_reported_as_an_error_status(client, monkeypatch):
    def fail(app, cache, flow, name, *, alias=None, label=None, root=None):
        raise RuntimeError("sign-in failed: https://graph.microsoft.com/x?token=SECRET")

    _install_device_flow(monkeypatch)
    monkeypatch.setattr(onedrive_provider, "finish_device_flow", fail)
    started = client.post(
        "/config/accounts/onedrive/start", data={"alias": "onedrive-2"}
    ).json()
    status = _wait_for_status(client, "/config/accounts/onedrive/status", started["flow_id"])
    assert status["status"] == "error"
    assert "SECRET" not in status["detail"]

def test_onedrive_start_without_a_client_id_is_friendly(client, monkeypatch):
    def missing(name):
        raise ValueError(
            "OneDrive sign-in needs an Azure application (client) ID in ONEDRIVE_CLIENT_ID"
        )

    monkeypatch.setattr(onedrive_provider, "start_device_flow", missing)
    response = client.post(
        "/config/accounts/onedrive/start", data={"alias": "onedrive-2"}
    )
    assert response.status_code == 400
    assert "ONEDRIVE_CLIENT_ID" in response.json()["detail"]

def test_unknown_flow_id_reports_an_error_status(client):
    body = client.get("/config/accounts/onedrive/status", params={"flow_id": "nope"}).json()
    assert body["status"] == "error"
    assert "unknown or expired" in body["detail"]


# --- Google Drive ----------------------------------------------------------

class _FakeCredentials:
    def to_json(self) -> str:
        return json.dumps({"refresh_token": "refresh", "token": "access"})


class _FakeFlow:
    def __init__(self, client_config, scopes, redirect_uri):
        self.client_config = client_config
        self.scopes = scopes
        self.redirect_uri = redirect_uri
        self.credentials = None
        self.fail = False

    def authorization_url(self, **kwargs):
        return (
            "https://accounts.google.com/o/oauth2/v2/auth?state=state-123",
            "state-123",
        )

    def fetch_token(self, code):
        if self.fail:
            raise RuntimeError("token exchange failed for https://oauth2.googleapis.com/token?token=SECRET")
        self.credentials = _FakeCredentials()


@pytest.fixture
def fake_drive(monkeypatch):
    state = {"fail": False}

    def start(client_config, redirect_uri):
        flow = _FakeFlow(client_config, ["drive.file"], redirect_uri)
        flow.fail = state["fail"]
        return flow, "https://accounts.google.com/o/oauth2/v2/auth?state=state-123", "state-123"

    monkeypatch.setattr(drive_provider, "resolve_oauth_client", lambda cid="", secret="": ({"installed": {"client_id": "test.apps.googleusercontent.com", "client_secret": "s"}}, "posted"))
    monkeypatch.setattr(drive_provider, "start_oauth_flow", start)
    return state


def test_drive_start_returns_an_authorization_url(client, fake_drive):
    response = client.post(
        "/config/accounts/drive/start", data={"alias": "drive-next", "label": "Next"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["auth_url"].startswith("https://accounts.google.com/")
    assert body["redirect_uri"].endswith("/config/oauth/drive/callback")

def test_drive_callback_stores_credentials_and_completes_the_flow(client, fake_drive):
    started = client.post(
        "/config/accounts/drive/start",
        data={"alias": "drive-next", "client_id": "test.apps.googleusercontent.com", "client_secret": "s"},
    ).json()
    assert client.get("/config/accounts/drive/status", params={"flow_id": started["flow_id"]}).json()["status"] == "pending"
    callback = client.get(
        "/config/oauth/drive/callback",
        params={"code": "auth-code", "state": "state-123"},
    )
    assert callback.status_code == 200
    assert "connected" in callback.text.lower()
    assert "window.close" in callback.text
    creds_path = accounts.secret_path("drive", "drive-next", "creds.json")
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    assert creds["refresh_token"] == "refresh"
    instance = accounts.load_instance("drive", "drive-next")
    assert instance.id == "drive-next"
    assert client.get(
        "/config/accounts/drive/status", params={"flow_id": started["flow_id"]}
    ).json() == {"status": "complete", "method": "drive-next"}

def test_drive_callback_on_an_unknown_state_is_friendly(client, fake_drive):
    response = client.get(
        "/config/oauth/drive/callback", params={"code": "x", "state": "forged"}
    )
    assert response.status_code == 400
    assert "no longer valid" in response.text
    assert "Traceback" not in response.text

def test_drive_callback_reports_a_token_exchange_failure(client, fake_drive):
    fake_drive["fail"] = True
    started = client.post(
        "/config/accounts/drive/start", data={"alias": "drive-next"}
    ).json()
    response = client.get(
        "/config/oauth/drive/callback", params={"code": "bad", "state": "state-123"}
    )
    assert response.status_code == 400
    assert "did not finish" in response.text
    assert "SECRET" not in response.text
    status = client.get(
        "/config/accounts/drive/status", params={"flow_id": started["flow_id"]}
    ).json()
    assert status["status"] == "error" and "SECRET" not in status["detail"]

def test_drive_start_rejects_an_unrecognized_oauth_client(client, monkeypatch, fake_drive):
    monkeypatch.setattr(drive_provider, "_drive_client_valid", lambda client_id, secret: False)
    response = client.post(
        "/config/accounts/drive/start",
        data={"alias": "drive-next", "client_id": "x.apps.googleusercontent.com", "client_secret": "s"},
    )
    assert response.status_code == 400
    assert "invalid_client" in response.json()["detail"]

def test_drive_start_accepts_a_client_secrets_file_without_preflight(client, monkeypatch):
    monkeypatch.setattr(
        drive_provider, "resolve_oauth_client", lambda cid="", secret="": ({"installed": {"client_id": "x"}}, "secret-file")
    )
    called = {"preflight": False}

    def preflight(client_id, secret):
        called["preflight"] = True
        return True

    monkeypatch.setattr(drive_provider, "_drive_client_valid", preflight)
    monkeypatch.setattr(
        drive_provider,
        "start_oauth_flow",
        lambda cfg, uri: (_FakeFlow(cfg, [], uri), "https://accounts.google.com/auth", "state-1"),
    )
    response = client.post("/config/accounts/drive/start", data={"alias": "drive-next"})
    assert response.status_code == 200
    assert called["preflight"] is False, "a Google-issued client file needs no network check"


def test_onedrive_reconnect_reuses_the_existing_account(client, monkeypatch):
    accounts.save_instance("onedrive", "onedrive-uni", alias="onedrive-uni", label="Uni")
    _install_device_flow(monkeypatch)
    monkeypatch.setattr(onedrive_provider, "finish_device_flow", _fake_finish)
    started = client.post(
        "/config/accounts/onedrive/start", data={"alias": "onedrive-uni"}
    )
    assert started.status_code == 200
    status = _wait_for_status(client, "/config/accounts/onedrive/status", started.json()["flow_id"])
    assert status == {"status": "complete", "method": "onedrive-uni"}
    assert accounts.load_instance("onedrive", "onedrive-uni").label == "Uni"

def test_drive_reconnect_overwrites_the_same_account(client, fake_drive):
    accounts.save_instance("drive", "drive-next", alias="drive-next", label="Next", root="library")
    started = client.post(
        "/config/accounts/drive/start", data={"alias": "drive-next", "client_secret": "s", "client_id": "x.apps.googleusercontent.com"}
    )
    assert started.status_code == 200
    response = client.get(
        "/config/oauth/drive/callback", params={"code": "code", "state": "state-123"}
    )
    assert response.status_code == 200
    instance = accounts.load_instance("drive", "drive-next")
    assert instance.label == "Next" and instance.root == "library"


# --- origin gate and alias selection ---------------------------------------

@pytest.mark.parametrize(
    "route,payload",
    [
        ("/config/accounts/mega", {"alias": "mega-2", "email": "a@b.c", "password": "x"}),
        ("/config/accounts/onedrive/start", {"alias": "onedrive-2"}),
        ("/config/accounts/drive/start", {"alias": "drive-next"}),
        ("/config/accounts/update", {"method": "drive", "alias": "drive-next"}),
    ],
)
def test_new_account_writes_from_a_foreign_origin_are_refused(client, route, payload):
    response = client.post(route, data=payload, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "only from this machine" in response.json()["detail"]
