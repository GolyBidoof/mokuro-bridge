"""The WebDAV provider, exercised against the repository's own test server.

`tools/dav_server.py` is a dependency-free WebDAV implementation written for
this suite (and for a local click-through demo), so these tests speak the real
protocol over a real socket: PROPFIND, MKCOL, PUT, GET and DELETE. Nothing here
touches a real server, the keychain, or `~/.config`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import accounts, api, config_api, providers, sessions
from mokuro_bridge.providers import webdav

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from dav_server import start_test_server  # noqa: E402  (path added above)


@pytest.fixture
def dav(tmp_path):
    """A running dummy WebDAV server; yields (base_url, storage_root)."""
    root = tmp_path / "davroot"
    server, thread, base_url = start_test_server(root)
    try:
        yield base_url, root
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway account registry, credential file and keyring-free store."""
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(webdav, "WEBDAV_CREDS_FILE", tmp_path / "webdav_credentials.json")
    monkeypatch.setattr(webdav, "_keyring", lambda: None)
    monkeypatch.setattr(
        providers, "_UPLOAD_METHOD_STATE_FILE", tmp_path / "upload_method_default.json"
    )
    for name in (
        "WEBDAV_BASE_URL",
        "WEBDAV_USERNAME",
        "WEBDAV_PASSWORD",
        "MOKURO_BRIDGE_UPLOAD_DEFAULT",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _store(base_url: str, name: str = accounts.DEFAULT_NAME, alias: str = None, payload: bytes = b"pages"):
    return webdav.store_webdav_account(
        name,
        base_url,
        "dav",
        "dav",
        alias=alias,
        label=(None if name == accounts.DEFAULT_NAME else "Second"),
    )


# --- URL handling ----------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "https://cloud.example.com/remote.php/dav/files/me/",
        "https://cloud.example.com/remote.php/dav/files/me",
        "http://127.0.0.1:8099/dav/",
    ],
)
def test_base_urls_are_normalised_to_the_collection(raw):
    assert webdav.clean_base_url(raw).endswith("/me") or webdav.clean_base_url(raw).endswith("/dav")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "cloud.example.com/dav",
        "ftp://cloud.example.com/dav",
        "https://user:secret@cloud.example.com/dav",
    ],
)
def test_invalid_base_urls_are_rejected(raw):
    with pytest.raises(ValueError):
        webdav.clean_base_url(raw)


def test_a_browser_query_or_fragment_is_dropped_not_refused():
    """A URL copied from the address bar often carries ?dir=…; strip it."""
    assert webdav.clean_base_url("https://cloud.example.com/dav?token=abc") == "https://cloud.example.com/dav"
    assert webdav.clean_base_url("https://cloud.example.com/dav#files") == "https://cloud.example.com/dav"


# --- credential store ------------------------------------------------------

def test_stored_account_is_configured_and_hides_the_password(dav, env):
    base_url, _root = dav
    stored = _store(base_url)

    assert stored.id == "webdav"
    assert stored.root_path == "mokuro-reader"
    assert stored.extra == {"url": base_url.rstrip("/"), "username": "dav"}
    assert webdav.configured() is True
    assert webdav.base_url() == base_url.rstrip("/")
    assert webdav.username() == "dav"
    assert webdav.password() == "dav"
    assert webdav.creds_source() == "file"

    metadata = (env / "accounts" / "webdav__default.json").read_text(encoding="utf-8")
    assert "dav" in metadata and "password" not in metadata.lower()
    creds = env / "webdav_credentials.json"
    assert creds.is_file()
    assert oct(creds.stat().st_mode & 0o777) == "0o600"
    assert json.loads(creds.read_text(encoding="utf-8"))["password"] == "dav"


def test_a_named_account_gets_its_own_credential_file(dav, env):
    base_url, _root = dav
    stored = _store(base_url, name="webdav-2", alias="webdav-2")

    assert stored.id == "webdav-2"
    assert webdav.creds_source("webdav-2") == "file"
    assert not (env / "webdav_credentials.json").exists()
    assert (env / "accounts" / "webdav__webdav-2.creds.json").is_file()
    assert webdav.configured("webdav-2") is True


def test_environment_seeds_the_first_account(dav, env, monkeypatch):
    base_url, _root = dav
    monkeypatch.setenv("WEBDAV_BASE_URL", base_url)
    monkeypatch.setenv("WEBDAV_USERNAME", "dav")
    monkeypatch.setenv("WEBDAV_PASSWORD", "dav")

    assert webdav.configured() is True
    assert webdav.creds_source() == "env"
    ok, error, resolved = webdav.verify_webdav_creds(base_url, "dav", "dav")
    assert ok and error is None
    assert resolved == base_url.rstrip("/")


def test_a_rejected_password_explains_app_passwords(dav, env):
    base_url, _root = dav
    ok, error, _resolved = webdav.verify_webdav_creds(base_url, "dav", "wrong-password")
    assert ok is False
    assert "app password" in (error or "")

    ok, error, _resolved = webdav.verify_webdav_creds("http://127.0.0.1:9/dav/", "dav", "dav")
    assert ok is False and "Could not reach" in (error or "")


def test_discovery_finds_the_collection_from_the_server_address(dav, env):
    """Pasting the server's own address (not its DAV path) still connects."""
    base_url, _root = dav
    origin = base_url.rsplit("/dav/", 1)[0] + "/"
    ok, error, resolved = webdav.verify_webdav_creds(origin, "dav", "dav", discover=True)
    assert ok is True, error
    assert resolved.rstrip("/") == origin.rstrip("/")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("cloud.example.com", "https://cloud.example.com"),
        ("cloud.example.com/remote.php/dav/files/me", "https://cloud.example.com/remote.php/dav/files/me"),
        ("nas:5005", "http://nas:5005"),
        ("192.168.1.9:5006/dav", "http://192.168.1.9:5006/dav"),
        ("localhost:8099", "http://localhost:8099"),
        ("https://cloud.example.com", "https://cloud.example.com"),
    ],
)
def test_a_missing_scheme_is_guessed(raw, expected):
    assert webdav.normalise_server_address(raw) == expected


def test_discovery_candidates_cover_the_well_known_layouts():
    candidates = webdav.discovery_candidates("https://cloud.example.com/apps/files?dir=/Books", "me")
    assert candidates[0] == "https://cloud.example.com/apps/files"
    assert "https://cloud.example.com/remote.php/dav/files/me" in candidates
    assert "https://cloud.example.com/remote.php/webdav" in candidates
    assert "https://cloud.example.com/apps/files/dav" in candidates
    # A plain server address is tried exactly as typed first (the NAS case).
    assert webdav.discovery_candidates("http://nas:5005/", "me")[0] == "http://nas:5005"


def test_the_certificate_checkbox_turns_verification_off():
    assert webdav._session("dav", "dav").verify is True
    assert webdav._session("dav", "dav", insecure=True).verify is False


def test_stored_insecure_flag_round_trips(dav, env):
    base_url, _root = dav
    stored = webdav.store_webdav_account("webdav", base_url, "dav", "dav", insecure=True)
    assert stored.extra["insecure"] is True
    assert webdav.account_insecure("webdav") is True
    assert webdav.PROVIDER.account(stored).extra["insecure"] is True


# --- upload / exists / download -------------------------------------------

def test_upload_creates_the_folder_chain_and_lands_the_bytes(dav, env):
    base_url, root = dav
    _store(base_url)
    source = env / "Volume 1.cbz"
    source.write_bytes(b"page-bytes")

    progress: list[tuple[int, int, int]] = []
    ok, error, url = providers.upload_file(
        "webdav", source, "mokuro-reader/サンプル作品 1巻", lambda d, t, s: progress.append((d, t, s))
    )

    assert ok is True, error
    landed = root / "mokuro-reader" / "サンプル作品 1巻" / "Volume 1.cbz"
    assert landed.read_bytes() == b"page-bytes"
    assert url == f"{base_url.rstrip('/')}/mokuro-reader/%E3%82%B5%E3%83%B3%E3%83%97%E3%83%AB%E4%BD%9C%E5%93%81%201%E5%B7%BB/Volume%201.cbz"
    assert progress and progress[-1] == (10, 10, 0)
    assert providers.remote_file_exists("webdav", "mokuro-reader/サンプル作品 1巻", "Volume 1.cbz")


def test_remote_exists_never_creates_the_folder(dav, env):
    base_url, root = dav
    _store(base_url)
    assert providers.remote_file_exists("webdav", "mokuro-reader/missing-series", "Volume 1.cbz") is False
    assert not (root / "mokuro-reader" / "missing-series").exists()


@pytest.mark.parametrize(
    "policy, expected, content",
    [("fail", False, b"old"), ("skip", True, b"old"), ("overwrite", True, b"new")],
)
def test_the_shared_overwrite_policy_applies(dav, env, policy, expected, content):
    base_url, root = dav
    _store(base_url)
    remote_dir = "mokuro-reader/Series"
    original = env / "Volume 1.cbz"
    original.write_bytes(b"old")
    assert providers.upload_file("webdav", original, remote_dir, None)[0] is True

    replacement = env / "replacement" / "Volume 1.cbz"
    replacement.parent.mkdir()
    replacement.write_bytes(b"new")
    ok, error, _url = providers.upload_file("webdav", replacement, remote_dir, None, policy)

    assert ok is expected, error
    if policy == "fail":
        assert "already exists" in (error or "")
    assert (root / remote_dir / "Volume 1.cbz").read_bytes() == content


def test_download_reads_a_sidecar_and_tolerates_absence(dav, env):
    base_url, root = dav
    _store(base_url)
    series_dir = root / "mokuro-reader" / "Series"
    series_dir.mkdir(parents=True)
    (series_dir / "series.json").write_bytes(b'{"series": []}')

    assert providers.download_file("webdav", "mokuro-reader/Series", "series.json") == b'{"series": []}'
    assert providers.download_file("webdav", "mokuro-reader/Series", "absent.json") is None


def test_listing_uses_propfind_depth_one(dav, env):
    base_url, root = dav
    _store(base_url)
    series = root / "mokuro-reader" / "Series A"
    series.mkdir(parents=True)
    (series / "volume-1.cbz").write_bytes(b"x" * 123)
    (root / "mokuro-reader" / "Zeta").mkdir()

    entries = providers.list_remote_folders("webdav", "mokuro-reader")
    assert entries == [
        {"name": "Series A", "path": "mokuro-reader/Series A", "type": "folder"},
        {"name": "Zeta", "path": "mokuro-reader/Zeta", "type": "folder"},
    ]

    inside = providers.list_remote_folders("webdav", "mokuro-reader/Series A")
    assert inside == [
        {"name": "volume-1.cbz", "path": "mokuro-reader/Series A/volume-1.cbz", "type": "file", "size": 123}
    ]


def test_listing_a_missing_collection_is_empty(dav, env):
    base_url, _root = dav
    _store(base_url)
    assert providers.list_remote_folders("webdav", "mokuro-reader/absent") == []


def test_an_unconfigured_account_fails_without_raising(env):
    ok, error, url = providers.upload_file("webdav", env / "file.bin", "mokuro-reader/Series", None)
    assert ok is False
    assert "setup-upload webdav" in (error or "")
    assert url is None
    assert webdav.configured() is False


# --- account view ----------------------------------------------------------

def test_account_view_reports_the_server_and_username(dav, env):
    base_url, _root = dav
    stored = _store(base_url)
    view = webdav.PROVIDER.account(stored)
    assert view.label == "WebDAV"
    assert view.extra["url"] == base_url.rstrip("/")
    assert view.extra["username"] == "dav"
    assert view.current_folder == f"mokuro-reader on {base_url.rstrip('/')}"


# --- the dashboard endpoint ------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch, env):
    work = tmp_path / "work"
    work.mkdir()
    (work / ".bridge_sessions").mkdir()
    monkeypatch.setattr(api, "WORK_DIR", work)
    monkeypatch.setattr(api, "SESSIONS_DIR", work / ".bridge_sessions")
    monkeypatch.setattr(sessions, "WORK_DIR", work)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", work / ".bridge_sessions")
    monkeypatch.setattr(api, "OUTPUT_DIR", tmp_path / "library")
    monkeypatch.setattr(config_api, "OUTPUT_DIR", tmp_path / "library")
    monkeypatch.setattr(config_api, "WORK_DIR", work)
    monkeypatch.setattr(config_api, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(api, "_LOCAL_INGEST_ROOTS", [tmp_path.resolve()])
    monkeypatch.setattr(config_api, "_LOCAL_INGEST_ROOTS", [tmp_path.resolve()])
    with TestClient(api.app) as test_client:
        yield test_client


def test_the_page_can_add_a_webdav_account(client, dav):
    base_url, _root = dav
    response = client.post(
        "/config/accounts/webdav",
        data={"alias": "house", "label": "NAS", "url": base_url, "username": "dav", "password": "dav"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "house"
    assert body["server"] == base_url.rstrip("/")

    config = client.get("/config").json()
    row = next(m for m in config["methods"] if m["id"] == "house")
    assert row["provider"] == "webdav"
    assert row["state"] == "connected"
    assert row["extra"]["url"] == base_url.rstrip("/")
    assert row["extra"]["username"] == "dav"

    providers_row = next(p for p in config["providers"] if p["id"] == "webdav")
    assert providers_row["browser_setup"] == "form"
    assert providers_row["available"] is True


def test_a_wrong_password_is_a_form_error_nothing_is_stored(client, dav, env):
    base_url, _root = dav
    response = client.post(
        "/config/accounts/webdav",
        data={"alias": "nas", "url": base_url, "username": "dav", "password": "nope"},
    )
    assert response.status_code == 400
    assert "nothing was stored" in response.json()["detail"]
    assert providers.instance_for("webdav") is None or not webdav.configured()
    assert not (env / "webdav_credentials.json").exists()


def test_a_bad_url_is_rejected_before_any_request(client, env):
    response = client.post(
        "/config/accounts/webdav",
        data={"alias": "nas", "url": "ftp://example.com/dav", "username": "dav", "password": "dav"},
    )
    assert response.status_code == 400
    assert "must start with http:// or https://" in (response.json()["detail"] or "")


def test_connecting_from_the_bare_server_address_creates_the_folder(client, dav):
    """A beginner pastes the address of their server, not a /dav/ path."""
    base_url, root = dav
    origin = base_url.rsplit("/dav/", 1)[0] + "/"
    response = client.post(
        "/config/accounts/webdav",
        data={"alias": "nas", "url": origin, "username": "dav", "password": "dav"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["server"].rstrip("/") == origin.rstrip("/")
    assert body["folder_ready"] is True
    assert (root / "mokuro-reader").is_dir(), "the upload folder is created at setup time"
    assert body["insecure"] is False


def test_the_self_signed_checkbox_is_stored_and_reported(client, dav):
    base_url, _root = dav
    response = client.post(
        "/config/accounts/webdav",
        data={
            "alias": "nas",
            "url": base_url,
            "username": "dav",
            "password": "dav",
            "insecure": "on",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["insecure"] is True
    row = next(m for m in client.get("/config").json()["methods"] if m["id"] == "nas")
    assert row["extra"]["insecure"] is True


def test_reconnect_keeps_the_stored_password_when_the_field_is_empty(client, dav):
    base_url, root = dav
    first = client.post(
        "/config/accounts/webdav",
        data={"alias": "nas", "url": base_url, "username": "dav", "password": "dav"},
    )
    assert first.status_code == 200, first.text

    again = client.post(
        "/config/accounts/webdav",
        data={"alias": "nas", "url": base_url, "username": "dav", "password": ""},
    )
    assert again.status_code == 200, again.text

    source = root / "probe.bin"
    source.write_bytes(b"x")
    from mokuro_bridge import providers as provider_registry

    assert provider_registry.upload_file("nas", source, "mokuro-reader/Series", None)[0] is True


def test_the_dashboard_form_has_the_first_time_setup_helpers():
    """The page, not just the API, is what a beginner meets first."""
    page = (REPO_ROOT / "mokuro_bridge" / "web" / "dashboard.html").read_text(encoding="utf-8")
    for marker in (
        'id="add-dav-preset"',
        "WEBDAV_PRESETS",
        "Where do I find this?",
        'id="add-dav-insecure"',
        'id="add-dav-root"',
        "normaliseDavAddress",
        "Accept a self-signed certificate",
    ):
        assert marker in page, f"the dashboard lost the WebDAV setup helper {marker!r}"
