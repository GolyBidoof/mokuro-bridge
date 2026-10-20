"""The Bunko installer/agent: config, connect payload, control conversation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mokuro_bridge import bunko_agent


@pytest.fixture
def bunko_home(tmp_path, monkeypatch):
    home = tmp_path / "bunko"
    monkeypatch.setenv("MOKURO_BRIDGE_BUNKO_DIR", str(home))
    return home


def test_config_is_cloud_mode_with_local_ocr_off(bunko_home):
    text = bunko_agent.config_yaml(
        base_path=bunko_home / "storage", port=12345, backend="mega", folder="mokuro-reader"
    )
    assert 'host: "127.0.0.1"' in text
    assert "port: 12345" in text
    assert "serve_readers: false" in text
    assert "backend: mega" in text
    assert 'folder: "mokuro-reader"' in text
    assert 'backend: "skip"' in text
    assert "local_processing: false" in text
    assert "auto: false" in text


def test_webdav_config_carries_url_and_user(bunko_home):
    text = bunko_agent.config_yaml(
        base_path=bunko_home / "storage",
        port=1234,
        backend="webdav",
        webdav={"url": "https://dav.example/mokuro", "username": "bridge", "allow_http": False},
    )
    assert "backend: webdav" in text
    assert 'url: "https://dav.example/mokuro"' in text
    assert 'username: "bridge"' in text
    assert "allow_http: false" in text


def test_folder_comes_from_the_bridge_root(monkeypatch):
    monkeypatch.setattr(bunko_agent, "method_root", lambda method: "/Root/mokuro-reader")
    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "mega")
    assert bunko_agent._folder_for("mega") == "mokuro-reader"


def test_rclone_folder_is_the_whole_account_root(monkeypatch):
    """The shared gateway serves the whole remote, so Bunko's folder is the root path.

    Only the last segment would mount `<remote>:mokuro-reader` while the bridge
    uploads to `<remote>:/Root/mokuro-reader`, i.e. a different (and empty) library.
    """
    monkeypatch.setattr(
        bunko_agent, "method_root", lambda method: "/Root/mokuro-reader/archive"
    )
    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "rclone")
    assert bunko_agent._folder_for("mega") == "Root/mokuro-reader/archive"


def test_rclone_folder_falls_back_when_the_root_is_empty(monkeypatch):
    monkeypatch.setattr(bunko_agent, "method_root", lambda method: "/")
    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "rclone")
    assert bunko_agent._folder_for("mega") == bunko_agent.BUNKO_LIBRARY_FOLDER


def test_mega_connect_payload_uses_the_bridge_account(bunko_home, monkeypatch):
    monkeypatch.setattr(
        bunko_agent.providers,
        "instance_for",
        lambda method: SimpleNamespace(provider="mega", name="default", root_path="/Root/mokuro-reader"),
    )
    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "mega")
    monkeypatch.setattr(
        "mokuro_bridge.providers.mega._get_mega_creds",
        lambda name: ("user@example.com", "secret"),
    )
    state = bunko_agent.AgentState(
        method="mega", backend="mega", folder="mokuro-reader",
        storage=str(bunko_home / "storage"), config=str(bunko_home / "config.yaml"),
        log=str(bunko_home / "bunko.log"), binary="/bin/true", commit="x",
    )
    payload = bunko_agent.connect_payload(state)
    assert payload["provider"] == "mega"
    assert payload["mega"] == {"email": "user@example.com", "password": "secret", "totp": None}


class _Response:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, states):
        self._states = list(states)
        self.posts: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, path, json=None):
        self.posts.append((path, json or {}))
        return _Response({"session": 1})

    def get(self, path):
        return _Response(self._states.pop(0) if self._states else {"state": "connected"})

    def close(self):
        self.closed = True


def test_finish_connect_polls_until_connected_and_creates_the_folder():
    client = _FakeClient(
        [
            {"state": "checking", "session": 1},
            {"state": "connected", "session": 1, "check": {"found": False, "folder": "mokuro-reader"}},
        ]
    )
    seen = []
    result = bunko_agent.finish_connect(
        client, payload={"provider": "mega"}, on_state=seen.append, sleep=lambda _s: None
    )
    assert result["state"] == "connected"
    assert any(path.endswith("/create") for path, _body in client.posts)
    assert seen[0]["state"] == "checking"


def test_finish_connect_reports_a_failure():
    client = _FakeClient([{"state": "failed", "session": 1, "error": "wrong password"}])
    with pytest.raises(bunko_agent.BunkoAgentError, match="wrong password"):
        bunko_agent.finish_connect(client, payload={"provider": "mega"}, sleep=lambda _s: None)


def test_state_round_trips(bunko_home):
    state = bunko_agent.AgentState(
        method="mega", backend="mega", folder="mokuro-reader",
        storage=str(bunko_home / "storage"), config=str(bunko_home / "config.yaml"),
        log=str(bunko_home / "bunko.log"), binary="/bin/true", commit="abc",
        port=1234, pid=999,
    )
    state.save()
    loaded = bunko_agent.load_state()
    assert loaded == state
    if os.name != "nt":
        assert (bunko_home / bunko_agent.STATE_FILE).stat().st_mode & 0o777 == 0o600


def test_mokuro_bunko_bin_skips_the_build(bunko_home, tmp_path):
    binary = tmp_path / "bunko"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    os.environ["MOKURO_BUNKO_BIN"] = str(binary)
    try:
        info = bunko_agent.install(log=lambda _m: None)
    finally:
        os.environ.pop("MOKURO_BUNKO_BIN", None)
    assert info.binary == str(binary)
    assert info.usable()


def test_setup_writes_the_config_signs_in_and_starts(bunko_home, monkeypatch):
    binary = bunko_home / "bin" / "mokuro-bunko"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "mega")
    monkeypatch.setattr(bunko_agent, "method_root", lambda method: "/Root/mokuro-reader")
    monkeypatch.setattr(
        bunko_agent, "install",
        lambda **kwargs: bunko_agent.InstallInfo(binary=str(binary), commit="abc", label="test"),
    )
    monkeypatch.setattr(bunko_agent, "ensure_admin", lambda state, **kwargs: setattr(state, "admin_user", "bridge-admin"))
    monkeypatch.setattr(bunko_agent, "free_port", lambda: 12345)
    monkeypatch.setattr(bunko_agent, "start_server", lambda state, **kwargs: state)
    monkeypatch.setattr(bunko_agent, "wait_for_control", lambda storage, **kwargs: {"port": 12345, "token": "t"})
    monkeypatch.setattr(
        bunko_agent, "connect_payload",
        lambda state, **kwargs: {"provider": "mega", "folder": state.folder, "mega": {"email": "u", "password": "p", "totp": None}},
    )
    client = _FakeClient([{"state": "connected", "session": 1, "check": {"found": True}}])
    monkeypatch.setattr(bunko_agent, "_control_client", lambda control, client_factory=None: client)
    monkeypatch.setattr(bunko_agent, "_wait_for_marker", lambda state, log=None: False)

    state = bunko_agent.setup("mega", log=lambda _m: None, wait_for_marker=True)

    config = Path(state.config).read_text(encoding="utf-8")
    assert "backend: mega" in config
    assert "serve_readers: false" in config
    assert state.connected_at
    assert state.port == 12345
    assert client.posts and client.posts[0][0].endswith("/library/connect")
    assert client.closed


def test_resolve_ref_accepts_a_commit_without_network():
    commit = "a" * 40
    assert bunko_agent.resolve_ref(commit, log=lambda _m: None) == commit


def test_resolve_ref_asks_github_for_a_branch(bunko_home, monkeypatch):
    monkeypatch.setattr(bunko_agent, "_ls_remote", lambda ref: "b" * 40)
    assert bunko_agent.resolve_ref("0.8/remote-storage", log=lambda _m: None) == "b" * 40


def test_install_reuses_the_newest_commit_without_rebuilding(bunko_home, monkeypatch):
    saved = bunko_agent.InstallInfo(
        binary=str(bunko_home / "bunko"), commit="c" * 40, label="dev",
    )
    monkeypatch.setattr(bunko_agent, "installed", lambda: saved)
    monkeypatch.setattr(bunko_agent, "resolve_ref", lambda ref=None, log=None: "c" * 40)

    def refuse(*_args, **_kwargs):
        raise AssertionError("nothing should be cloned or built")

    info = bunko_agent.install(log=lambda _m: None, runner=refuse)
    assert info is saved


def test_update_reports_an_unchanged_commit(bunko_home, monkeypatch):
    state = bunko_agent.AgentState(
        method="mega", backend="mega", folder="mokuro-reader",
        storage=str(bunko_home / "storage"), config=str(bunko_home / "config.yaml"),
        log=str(bunko_home / "bunko.log"), binary="/bin/true", commit="d" * 40,
    )
    monkeypatch.setattr(bunko_agent, "load_state", lambda: state)
    monkeypatch.setattr(
        bunko_agent, "install",
        lambda **kwargs: bunko_agent.InstallInfo(binary="/bin/true", commit="d" * 40, label="dev"),
    )
    info, changed = bunko_agent.update(log=lambda _m: None)
    assert info.commit == "d" * 40
    assert changed is False


def test_setup_refuses_local_before_installing(monkeypatch, bunko_home):
    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: None)
    monkeypatch.setattr(
        bunko_agent, "install",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("install must not run")),
    )
    with pytest.raises(bunko_agent.BunkoAgentError, match="cloud destination"):
        bunko_agent.setup("local", log=lambda _m: None)


def test_setup_fails_fast_when_the_destination_has_no_credentials(monkeypatch, bunko_home):
    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "mega")
    monkeypatch.setattr(bunko_agent, "method_root", lambda method: "/Root/mokuro-reader")
    monkeypatch.setattr(
        bunko_agent, "connect_payload",
        lambda state, **kwargs: (_ for _ in ()).throw(bunko_agent.BunkoAgentError("MEGA credentials not configured")),
    )
    monkeypatch.setattr(
        bunko_agent, "install",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("install must not run")),
    )
    with pytest.raises(bunko_agent.BunkoAgentError, match="credentials not configured"):
        bunko_agent.setup("mega", log=lambda _m: None)
    assert not bunko_agent.config_path().exists()


def test_connect_payload_wraps_missing_mega_credentials(bunko_home, monkeypatch):
    monkeypatch.setattr(
        bunko_agent.providers,
        "instance_for",
        lambda method: SimpleNamespace(provider="mega", name="default", root_path="/Root/mokuro-reader"),
    )
    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "mega")
    monkeypatch.setattr(
        "mokuro_bridge.providers.mega._get_mega_creds",
        lambda name: (_ for _ in ()).throw(RuntimeError("MEGA credentials not configured")),
    )
    state = bunko_agent.AgentState(
        method="mega", backend="mega", folder="mokuro-reader",
        storage=str(bunko_home / "storage"), config=str(bunko_home / "config.yaml"),
        log=str(bunko_home / "bunko.log"), binary="", commit="",
    )
    with pytest.raises(bunko_agent.BunkoAgentError, match="credentials not configured"):
        bunko_agent.connect_payload(state)


# --- resilience to Bunko restarting itself ----------------------------------


def test_the_control_client_re_resolves_a_restarted_bunko(tmp_path, monkeypatch):
    """`library connect` can restart Bunko onto a new control port."""
    import httpx

    controls = [{"port": 1111, "token": "a"}, {"port": 2222, "token": "b"}]
    monkeypatch.setattr(
        bunko_agent, "wait_for_control", lambda storage, **kwargs: controls.pop(0)
    )

    class _Failing:
        def __init__(self):
            self.closed = False

        def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        def close(self):
            self.closed = True

    class _Healthy:
        def __init__(self):
            self.closed = False

        def post(self, path, **_kwargs):
            return _Response({"session": 1})

        def get(self, path):
            return _Response({"state": "connected"})

        def close(self):
            self.closed = True

    first, second = _Failing(), _Healthy()
    built: list[int] = []

    def _factory(control, client_factory=None):
        built.append(int(control["port"]))
        return first if int(control["port"]) == 1111 else second

    monkeypatch.setattr(bunko_agent, "_control_client", _factory)

    client = bunko_agent._ControlClient(tmp_path, sleep=lambda _s: None)
    response = client.post("/app/api/library/connect", json={})
    assert response.status_code == 200
    assert built == [1111, 2222], "the client must re-read .control.json after a restart"
    assert first.closed and not second.closed
    assert client.control["port"] == 2222

    client.close()
    assert second.closed


def test_setup_survives_a_restart_and_adopts_the_new_pid(bunko_home, monkeypatch):
    """A rerun of --setup-bunko must not die on the old control port or pid."""
    import httpx

    binary = bunko_home / "bin" / "mokuro-bunko"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setattr(bunko_agent, "method_provider", lambda method: "mega")
    monkeypatch.setattr(bunko_agent, "method_root", lambda method: "/Root/mokuro-reader")
    monkeypatch.setattr(
        bunko_agent, "install",
        lambda **kwargs: bunko_agent.InstallInfo(binary=str(binary), commit="abc", label="test"),
    )
    monkeypatch.setattr(
        bunko_agent, "ensure_admin",
        lambda state, **kwargs: setattr(state, "admin_user", "bridge-admin"),
    )
    monkeypatch.setattr(bunko_agent, "free_port", lambda: 12345)
    monkeypatch.setattr(bunko_agent, "start_server", lambda state, **kwargs: state)
    controls = [{"port": 1111, "token": "a"}, {"port": 2222, "token": "b"}]
    monkeypatch.setattr(
        bunko_agent, "wait_for_control", lambda storage, **kwargs: controls.pop(0)
    )
    monkeypatch.setattr(
        bunko_agent, "connect_payload",
        lambda state, **kwargs: {"provider": "mega", "folder": state.folder},
    )

    class _Failing:
        def __init__(self):
            self.closed = False

        def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        def close(self):
            self.closed = True

    def _factory(control, client_factory=None):
        if int(control["port"]) == 1111:
            return _Failing()
        return _FakeClient([{"state": "connected", "session": 1, "check": {"found": True}}])

    monkeypatch.setattr(bunko_agent, "_control_client", _factory)
    monkeypatch.setattr(bunko_agent, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(bunko_agent, "_pid_on_port", lambda port: 4242)
    monkeypatch.setattr(bunko_agent, "_wait_for_marker", lambda state, log=None: False)

    state = bunko_agent.setup("mega", log=lambda _m: None, wait_for_marker=True)

    assert state.connected_at
    assert state.pid == 4242, "the restarted pid must replace the dead one"
    saved = json.loads(Path(state.config).with_name(bunko_agent.STATE_FILE).read_text())
    assert saved["pid"] == 4242


def test_a_restarted_bunko_reads_as_running_with_a_stale_pid(tmp_path, monkeypatch):
    """--bunko-status and autostart must not trust a dead recorded pid alone."""
    state = bunko_agent.AgentState(
        method="mega", backend="mega", folder="mokuro-reader",
        storage=str(tmp_path), config=str(tmp_path / "config.yaml"),
        log=str(tmp_path / "bunko.log"), binary="/bin/true", commit="x",
        port=12345, pid=999999,
    )
    (tmp_path / ".control.json").write_text(
        json.dumps({"port": 4444, "token": "t"}), encoding="utf-8"
    )
    monkeypatch.setattr(bunko_agent, "_pid_alive", lambda pid: False)

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        bunko_agent.socket, "create_connection", lambda addr, timeout=None: _Connection()
    )

    assert bunko_agent.server_running(state) is True
    assert "running" in bunko_agent.status_line(state)
