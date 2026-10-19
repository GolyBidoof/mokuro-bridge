"""The rclone destination, tested against a real rclone binary.

Why this provider exists: a self-hosted bridge cannot ship an Azure app ID, and
creating one needs an Entra tenant a personal Microsoft account cannot make.
rclone has its own Microsoft app registration, so its OneDrive sign-in works
with just a personal account. These tests drive the provider through a ``local``
rclone remote (an ordinary folder) so they need no network and no cloud
account, but they do need the rclone binary; they skip when it is absent, and
the pure parsing/path helpers still run everywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mokuro_bridge import accounts, api, config_api, providers, sessions
from mokuro_bridge.providers import rclone

REPO_ROOT = Path(__file__).resolve().parent.parent
RCLONE_BIN = shutil.which("rclone")
needs_rclone = pytest.mark.skipif(RCLONE_BIN is None, reason="rclone is not installed")


# --- pure helpers (no binary needed) --------------------------------------

@pytest.mark.parametrize(
    "line, total, expected",
    [
        ("20 MiB / 20 MiB, 100%, 0 B/s, ETA -", 20 * 1024 * 1024, (20 * 1024 * 1024, 20 * 1024 * 1024, 0)),
        ("1.000 MiB / 4.000 MiB, 25%, 512.000 KiB/s, ETA 6s", 4 * 1024 * 1024, (1024 * 1024, 4 * 1024 * 1024, 512 * 1024)),
        ("0 B / 0 B, 100%, 0 B/s, ETA -", 0, None),
        ("Transferred:    1.2 MiB", 1000, None),
    ],
)
def test_progress_lines_become_byte_deltas(line, total, expected):
    assert rclone._parse_progress(line, total) == expected


def test_rclone_paths_keep_segments_together():
    assert rclone._target("onedrive:", "mokuro-reader/Series A", "vol 1.cbz") == (
        "onedrive:mokuro-reader/Series A/vol 1.cbz"
    )
    assert rclone._target("onedrive:", "/mokuro-reader/", "") == "onedrive:mokuro-reader"
    assert rclone._target("onedrive:", "") == "onedrive:"


def test_remotes_without_a_colon_are_normalised(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(rclone, "_remotes_cache", None)
    stored = accounts.save_instance("rclone", accounts.DEFAULT_NAME, extra={"remote": "demo"})
    assert stored is not None
    assert rclone.remote_for() == "demo:"


# --- integration against a local rclone remote ----------------------------

@pytest.fixture
def rclone_env(tmp_path, monkeypatch):
    """An isolated rclone config with one ``local`` remote rooted at tmp_path."""
    if RCLONE_BIN is None:
        pytest.skip("rclone is not installed")
    config = tmp_path / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(config))
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(rclone, "_remotes_cache", None)
    monkeypatch.setattr(
        providers, "_UPLOAD_METHOD_STATE_FILE", tmp_path / "upload_method_default.json"
    )
    # A local-type rclone remote resolves relative paths against the process
    # working directory, so keep it inside the test's temp tree.
    monkeypatch.chdir(tmp_path)
    made = subprocess.run(
        [RCLONE_BIN, "config", "create", "demo", "local"],
        capture_output=True,
        text=True,
        env={**os.environ, "RCLONE_CONFIG": str(config)},
    )
    assert made.returncode == 0, made.stderr
    return tmp_path


@needs_rclone
def test_an_account_is_usable_once_its_remote_exists(rclone_env, monkeypatch):
    monkeypatch.setattr(rclone, "RCLONE_BINARY", "rclone")
    stored = accounts.save_instance("rclone", accounts.DEFAULT_NAME, extra={"remote": "demo:"})
    assert stored.id == "rclone"
    assert rclone.configured() is True
    assert rclone.creds_source() == "rclone config"
    view = rclone.PROVIDER.account(stored)
    assert view.extra["remote"] == "demo:"
    assert view.current_folder.startswith("mokuro-reader on demo:")


@needs_rclone
def test_upload_exists_download_and_listing_round_trip(rclone_env, monkeypatch):
    accounts.save_instance("rclone", accounts.DEFAULT_NAME, extra={"remote": "demo:"})
    payload = rclone_env / "Volume 1.cbz"
    payload.write_bytes(b"pages" * 2000)

    progress: list[tuple[int, int]] = []
    ok, error, url = providers.upload_file(
        "rclone", payload, "mokuro-reader/サンプル作品", lambda done, total, speed: progress.append((done, total))
    )
    assert ok is True, error
    assert url is None, "rclone has no public URL to report"
    landed = rclone_env / "mokuro-reader" / "サンプル作品" / "Volume 1.cbz"
    assert landed.read_bytes() == payload.read_bytes()
    assert progress and progress[-1][0] == progress[-1][1]

    assert providers.remote_file_exists("rclone", "mokuro-reader/サンプル作品", "Volume 1.cbz") is True
    assert providers.download_file("rclone", "mokuro-reader/サンプル作品", "Volume 1.cbz") == payload.read_bytes()
    assert providers.download_file("rclone", "mokuro-reader/サンプル作品", "absent.json") is None

    entries = providers.list_remote_folders("rclone", "mokuro-reader")
    assert entries == [
        {"name": "サンプル作品", "path": "mokuro-reader/サンプル作品", "type": "folder"}
    ]


@needs_rclone
def test_remote_exists_does_not_create_the_folder(rclone_env, monkeypatch):
    accounts.save_instance("rclone", accounts.DEFAULT_NAME, extra={"remote": "demo:"})
    assert providers.remote_file_exists("rclone", "mokuro-reader/never", "x.cbz") is False
    assert not (rclone_env / "mokuro-reader" / "never").exists()


@needs_rclone
@pytest.mark.parametrize(
    "policy, expected, content",
    [("fail", False, b"old"), ("skip", True, b"old"), ("overwrite", True, b"new")],
)
def test_the_shared_overwrite_policy_applies(rclone_env, monkeypatch, policy, expected, content):
    accounts.save_instance("rclone", accounts.DEFAULT_NAME, extra={"remote": "demo:"})
    original = rclone_env / "Volume 1.cbz"
    original.write_bytes(b"old")
    assert providers.upload_file("rclone", original, "mokuro-reader/Series", None)[0] is True

    replacement = rclone_env / "replacement" / "Volume 1.cbz"
    replacement.parent.mkdir()
    replacement.write_bytes(b"new")
    ok, error, _url = providers.upload_file("rclone", replacement, "mokuro-reader/Series", None, policy)
    assert ok is expected, error
    if policy == "fail":
        assert "already exists" in (error or "")
    assert (rclone_env / "mokuro-reader" / "Series" / "Volume 1.cbz").read_bytes() == content


@needs_rclone
def test_an_account_without_a_remote_fails_without_raising(rclone_env, monkeypatch):
    accounts.save_instance("rclone", accounts.DEFAULT_NAME)
    ok, error, url = providers.upload_file("rclone", rclone_env / "x.bin", "mokuro-reader/Series", None)
    assert ok is False
    assert "rclone" in (error or "")
    assert url is None


# --- the dashboard endpoint ------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
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
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(rclone, "_remotes_cache", None)
    with TestClient(api.app) as test_client:
        yield test_client


@needs_rclone
def test_the_page_can_add_an_rclone_destination(client, rclone_env, monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_env / "rclone.conf"))
    response = client.post(
        "/config/accounts/rclone",
        data={"alias": "cloud", "label": "OneDrive", "remote": "demo:", "root": "library"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "cloud"
    assert body["remote"] == "demo:"
    assert body["root"] == "library"
    assert (rclone_env / "library").is_dir(), "the upload folder is created at setup"
    row = next(m for m in client.get("/config").json()["methods"] if m["id"] == "cloud")
    assert row["provider"] == "rclone"
    assert row["extra"]["remote"] == "demo:"


@needs_rclone
def test_an_unknown_remote_is_refused_with_the_available_ones(client, rclone_env, monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_env / "rclone.conf"))
    response = client.post(
        "/config/accounts/rclone", data={"alias": "cloud", "remote": "nope:"}
    )
    assert response.status_code == 400
    assert "demo:" in response.json()["detail"]


@needs_rclone
def test_the_provider_row_lists_the_remotes(client, rclone_env, monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_env / "rclone.conf"))
    row = next(p for p in client.get("/config").json()["providers"] if p["id"] == "rclone")
    assert row["available"] is True
    assert row["remotes"] == ["demo:"]


def test_the_page_offers_the_rclone_form_and_its_steps():
    page = (REPO_ROOT / "mokuro_bridge" / "web" / "dashboard.html").read_text(encoding="utf-8")
    for marker in (
        'id="add-rclone-remote"',
        "rclone config",
        "rclone has its own Microsoft app registration",
        "add-use-rclone",
    ):
        assert marker in page, f"the dashboard lost the rclone helper {marker!r}"
