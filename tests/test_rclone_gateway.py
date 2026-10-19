"""The shared rclone WebDAV gateway.

These tests use a real rclone binary against a local remote, so no cloud
account or network access is involved. They prove the property the gateway
exists for: the bridge uploads through a loopback connection string while Bunko
is handed the same endpoint, so both share one remote session.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from mokuro_bridge import accounts, rclone_gateway
from mokuro_bridge.providers import rclone

RCLONE_BIN = shutil.which("rclone")
needs_rclone = pytest.mark.skipif(RCLONE_BIN is None, reason="rclone is not installed")


@pytest.fixture
def gateway_env(tmp_path, monkeypatch):
    if RCLONE_BIN is None:
        pytest.skip("rclone is not installed")
    config = tmp_path / "rclone.conf"
    cloud = tmp_path / "cloud"
    (cloud / "lib").mkdir(parents=True)
    env = {**os.environ, "RCLONE_CONFIG": str(config)}
    # An alias remote scopes rclone to the temp directory. A local remote has
    # no root option, so serving one would expose the process working directory.
    subprocess.run(
        [RCLONE_BIN, "config", "create", "demo", "alias", "remote", str(cloud)],
        check=True, capture_output=True, env=env,
    )
    monkeypatch.setenv("RCLONE_CONFIG", str(config))
    monkeypatch.setenv("MOKURO_BRIDGE_RCLONE_DIR", str(tmp_path / "gateway"))
    monkeypatch.setenv("MOKURO_BRIDGE_RCLONE_GATEWAY", "1")
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(rclone, "_remotes_cache", None)
    accounts.save_instance("rclone", "default", root="lib", extra={"remote": "demo:"})
    try:
        yield tmp_path
    finally:
        # `rclone serve` is started in its own session so it survives a bridge
        # restart, which also means an unclosed test gateway outlives pytest.
        # The fixture owns the isolated gateway dir, so stopping here cannot
        # touch a gateway the user actually runs.
        try:
            rclone_gateway.stop_all()
        except Exception:
            pass


@needs_rclone
def test_the_bridge_and_bunko_share_one_gateway(gateway_env):
    gateway = rclone_gateway.ensure("default")
    assert gateway.healthy()
    assert gateway.url.startswith("http://127.0.0.1:")
    assert gateway.bunko_payload()["url"] == gateway.url

    # The bridge's transfers go through the gateway, not the cloud remote.
    remote, config = rclone.transport_for("default")
    assert remote.startswith("mokuro-gateway-")
    assert config == str(rclone_gateway.client_config_path())

    local = gateway_env / "vol.cbz"
    local.write_bytes(b"page-data")
    ok, error, _ = rclone._upload("default", "lib", "lib/series-a", local, None, "overwrite")
    assert ok, error
    assert (gateway_env / "cloud" / "lib" / "series-a" / "vol.cbz").read_bytes() == b"page-data"

    assert rclone._remote_exists("default", "lib/series-a", "vol.cbz") is True
    assert rclone._download("default", "lib/series-a", "vol.cbz") == b"page-data"

    folders = rclone._list_folders("default", "lib")
    assert any(entry["name"] == "series-a" and entry["type"] == "folder" for entry in folders)


@needs_rclone
def test_a_restart_reuses_the_same_endpoint(gateway_env):
    first = rclone_gateway.ensure("default")
    assert rclone_gateway.stop("default") is True
    assert rclone_gateway.status("default")["running"] is False

    second = rclone_gateway.ensure("default")
    assert second.port == first.port
    assert second.username == first.username
    assert second.password == first.password
    assert second.healthy()


@needs_rclone
def test_bunko_is_handed_a_webdav_block(gateway_env, monkeypatch):
    from mokuro_bridge import bunko_agent

    state = bunko_agent.AgentState(
        method="rclone",
        backend="webdav",
        folder="lib",
        storage="",
        config="",
        log="",
        binary="",
        commit="",
        port=0,
    )
    payload = bunko_agent.connect_payload(state)
    assert payload["provider"] == "webdav"
    assert payload["webdav"]["url"].startswith("http://127.0.0.1:")
    assert payload["webdav"]["allow_http"] is True
