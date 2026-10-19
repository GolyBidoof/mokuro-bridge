"""Shared test defaults.

The rclone gateway spawns a real `rclone serve webdav` per account. Provider
tests exercise the rclone binary against local remotes and should not start
gateways, so the gateway is off unless a test opts back in. Gateway tests set
MOKURO_BRIDGE_RCLONE_GATEWAY=1 themselves.
"""
import pytest


@pytest.fixture(autouse=True)
def _rclone_gateway_off(monkeypatch):
    monkeypatch.setenv("MOKURO_BRIDGE_RCLONE_GATEWAY", "0")
