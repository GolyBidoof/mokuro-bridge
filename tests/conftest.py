"""Shared test defaults.

The rclone gateway spawns a real `rclone serve webdav` per account. Provider
tests exercise the rclone binary against local remotes and should not start
gateways, so the gateway is off unless a test opts back in. Gateway tests set
MOKURO_BRIDGE_RCLONE_GATEWAY=1 themselves.
"""
import pytest

from mokuro_bridge import api, config


@pytest.fixture(autouse=True)
def _rclone_gateway_off(monkeypatch):
    monkeypatch.setenv("MOKURO_BRIDGE_RCLONE_GATEWAY", "0")


@pytest.fixture(autouse=True)
def _sticky_local_dir_state_in_tmp(tmp_path, monkeypatch):
    """Keep the remembered local output folder out of the real work dir.

    `config._LOCAL_DIR_STATE_FILE` is a module global built from WORK_DIR at
    import, and `/config/local-output` writes it through the config module's
    own `_remember_local_dir`. A test that does not patch it therefore rewrites
    the developer's real `local_dir_default.json` with a pytest temp path,
    which then shows up as `current_folder` for the `local` destination in
    `/upload-methods` and `/health`, and as the target of a later local
    finalize. Redirect it per test; api.py holds its own copy for the
    forget-on-invalid branch.
    """
    state = tmp_path / "local_dir_default.json"
    monkeypatch.setattr(config, "_LOCAL_DIR_STATE_FILE", state)
    monkeypatch.setattr(api, "_LOCAL_DIR_STATE_FILE", state)
