"""The version check has to be useful offline and harmless when it fails.

Nothing here touches the network: ``check_for_update`` and ``latest_release``
both take an injectable fetcher, and ``_http_get_json`` is patched where the
payload shape is what matters. The suite runs with no internet in CI.
"""

from __future__ import annotations

import pytest

from mokuro_bridge import update


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """The cache is module-level state; no test may inherit another's."""
    monkeypatch.setattr(update, "_cached", None)
    monkeypatch.setattr(update, "_cached_at", 0.0)
    monkeypatch.setattr(update, "_inflight", False)
    monkeypatch.setattr(update, "_auto", False)
    monkeypatch.setattr(update, "_announced", None)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0.6.0", (0, 6, 0)),
        ("v0.6.0", (0, 6, 0)),
        ("V1.2.3", (1, 2, 3)),
        ("1.2", (1, 2)),
        ("2", (2,)),
        ("  0.6.0  ", (0, 6, 0)),
        # GitHub's latest-release endpoint excludes prereleases, but a tag can
        # still be pointed at by hand; dropping the suffix is the safe read.
        ("0.7.0rc1", (0, 7, 0)),
        ("", None),
        ("nonsense", None),
        (None, None),
    ],
)
def test_parse_version(text, expected):
    assert update.parse_version(text) == expected


@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("0.7.0", "0.6.0", True),
        ("v0.7.0", "0.6.0", True),
        ("0.6.1", "0.6.0", True),
        ("0.7", "0.6.9", True),
        ("1.0.0", "0.99.99", True),
        ("0.6.0", "0.6.0", False),
        ("0.6.0", "0.7.0", False),
        ("0.5.9", "0.6.0", False),
        # A comparison that cannot be made must never nag the user to update.
        ("garbage", "0.6.0", False),
        ("", "0.6.0", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert update.is_newer(candidate, current) is expected


def _release(version="0.7.0", **overrides):
    payload = {
        "version": version,
        "tag": f"v{version}",
        "name": f"mokuro-bridge v{version}",
        "url": f"https://example.invalid/v{version}",
        "published_at": "2026-09-27T00:00:00Z",
        "notes": "",
    }
    payload.update(overrides)
    return payload


def test_a_newer_release_is_reported():
    result = update.check_for_update("0.6.0", fetcher=lambda **_kw: _release())
    assert result["update_available"] is True
    assert result["latest"] == "0.7.0"
    assert result["current"] == "0.6.0"
    assert result["url"] == "https://example.invalid/v0.7.0"
    assert result["error"] is None


def test_the_running_version_is_not_reported_as_an_update():
    result = update.check_for_update("0.6.0", fetcher=lambda **_kw: _release("0.6.0"))
    assert result["update_available"] is False
    assert result["error"] is None


def test_an_older_release_is_not_reported_as_an_update():
    result = update.check_for_update("0.6.0", fetcher=lambda **_kw: _release("0.5.0"))
    assert result["update_available"] is False


def test_a_failed_fetch_is_reported_rather_than_raised():
    def boom(**_kwargs):
        raise update.UpdateError("could not reach GitHub: no route to host")

    result = update.check_for_update("0.6.0", fetcher=boom)
    assert result["update_available"] is False
    assert result["latest"] is None
    assert "no route to host" in result["error"]


def test_an_unexpected_exception_is_contained_too():
    """A bug in the checker must not take the server down with it."""

    def boom(**_kwargs):
        raise RuntimeError("kaboom")

    result = update.check_for_update("0.6.0", fetcher=boom)
    assert result["update_available"] is False
    assert "kaboom" in result["error"]


def test_the_default_fetcher_is_used_when_none_is_given(monkeypatch):
    seen = {}

    def fake_latest_release(timeout=None):
        seen["timeout"] = timeout
        return _release()

    monkeypatch.setattr(update, "latest_release", fake_latest_release)
    result = update.check_for_update("0.6.0")
    assert result["latest"] == "0.7.0"
    assert "timeout" in seen


def test_latest_release_normalises_the_tag(monkeypatch):
    monkeypatch.setattr(
        update,
        "_http_get_json",
        lambda url, timeout: {
            "tag_name": "v0.7.0",
            "name": "mokuro-bridge v0.7.0 - TLS fetch proxy",
            "html_url": "https://example.invalid/rel",
            "published_at": "2026-09-27T00:00:00Z",
            "body": "notes",
        },
    )
    release = update.latest_release()
    assert release["version"] == "0.7.0"
    assert release["tag"] == "v0.7.0"
    assert release["url"] == "https://example.invalid/rel"
    assert release["notes"] == "notes"


def test_latest_release_rejects_a_payload_with_no_usable_tag(monkeypatch):
    monkeypatch.setattr(
        update, "_http_get_json", lambda url, timeout: {"tag_name": "nightly"}
    )
    with pytest.raises(update.UpdateError, match="no usable version tag"):
        update.latest_release()


def test_refresh_caches_a_result_and_reuses_it_within_the_ttl(monkeypatch):
    calls = []

    def fake_check(current=None, **kwargs):
        calls.append(current)
        return {
            "current": current or update.__version__,
            "latest": "0.7.0",
            "update_available": True,
            "url": "https://example.invalid",
            "published_at": "",
            "error": None,
        }

    monkeypatch.setattr(update, "check_for_update", fake_check)
    first = update.refresh(force=True)
    second = update.refresh()  # within the TTL: must not refetch
    assert first == second
    assert len(calls) == 1
    assert update.cached() == first


def test_force_bypasses_the_ttl(monkeypatch):
    calls = []

    def fake_check(current=None, **kwargs):
        calls.append(1)
        return {"current": "0.6.0", "latest": None, "update_available": False,
                "url": "", "published_at": "", "error": None}

    monkeypatch.setattr(update, "check_for_update", fake_check)
    update.refresh(force=True)
    update.refresh(force=True)
    assert len(calls) == 2


def test_a_failed_check_is_cached_so_health_stops_retrying(monkeypatch):
    calls = []

    def fake_check(current=None, **kwargs):
        calls.append(1)
        return {"current": "0.6.0", "latest": None, "update_available": False,
                "url": "", "published_at": "", "error": "no network"}

    monkeypatch.setattr(update, "check_for_update", fake_check)
    update.refresh(force=True)
    update.refresh()
    assert len(calls) == 1, "a cached failure must not be retried every call"
    assert update.health_fields()["update_check"] == "error"


def test_health_fields_report_disabled_when_the_env_turns_checks_off(monkeypatch):
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_CHECK", "0")
    fields = update.health_fields()
    assert fields["update_check"] == "disabled"
    assert fields["latest_version"] is None
    assert fields["update_available"] is False


def test_health_fields_are_pending_before_the_first_check():
    fields = update.health_fields()
    assert fields["update_check"] == "pending"
    assert fields["latest_version"] is None
    assert fields["update_available"] is False


def test_health_fields_report_a_known_newer_version(monkeypatch):
    monkeypatch.setattr(
        update, "_cached",
        {"current": "0.6.0", "latest": "0.7.0", "update_available": True,
         "url": "https://example.invalid", "published_at": "", "error": None},
    )
    monkeypatch.setattr(update, "_cached_at", 1.0)
    fields = update.health_fields()
    assert fields["update_check"] == "ok"
    assert fields["latest_version"] == "0.7.0"
    assert fields["update_available"] is True


def test_health_fields_never_touch_the_network(monkeypatch):
    """Importing the API must not be enough to make a request."""

    def explode(*_args, **_kwargs):
        raise AssertionError("/health must not fetch the release list")

    monkeypatch.setattr(update, "latest_release", explode)
    monkeypatch.setattr(update, "_http_get_json", explode)
    assert update.health_fields()["update_check"] == "pending"


def test_health_fields_always_return_the_same_keys(monkeypatch):
    """A client reads a field, it does not probe for one.

    /health already works this way for fetchProxyPorts, and a key that appears
    only after the first check would be a trap for the userscript.
    """
    expected = {"update_check", "latest_version", "update_available",
                "update_url", "update_error"}

    assert set(update.health_fields()) == expected
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_CHECK", "0")
    assert set(update.health_fields()) == expected
    monkeypatch.delenv("MOKURO_BRIDGE_UPDATE_CHECK")
    monkeypatch.setattr(
        update, "_cached",
        {"current": "0.6.0", "latest": "0.7.0", "update_available": True,
         "url": "https://example.invalid", "published_at": "", "error": None},
    )
    assert set(update.health_fields()) == expected
    monkeypatch.setattr(
        update, "_cached",
        {"current": "0.6.0", "latest": None, "update_available": False,
         "url": "", "published_at": "", "error": "no network"},
    )
    fields = update.health_fields()
    assert set(fields) == expected
    assert fields["update_check"] == "error"
    assert fields["update_error"] == "no network"
    # An unusable URL must not be echoed as one.
    assert fields["update_url"] == update.RELEASES_PAGE_URL


def test_background_checks_are_off_until_the_cli_arms_them(monkeypatch):
    """A bare `import mokuro_bridge.api` must not start a network thread."""
    started = []
    monkeypatch.setattr(
        update, "_refresh_in_background", lambda **kw: started.append(kw)
    )
    update.maybe_refresh_async()
    assert started == [], "checks ran before enable_auto_checks()"

    update.enable_auto_checks()
    update.maybe_refresh_async()
    assert started, "the armed path must actually schedule a check"


def test_the_env_kill_switch_overrides_the_armed_state(monkeypatch):
    started = []
    monkeypatch.setattr(
        update, "_refresh_in_background", lambda **kw: started.append(kw)
    )
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_CHECK", "0")
    update.enable_auto_checks()
    update.maybe_refresh_async()
    assert started == []


def test_ttl_and_timeout_read_the_environment(monkeypatch):
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_TTL_S", "30")
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_TIMEOUT_S", "2.5")
    assert update.ttl_s() == 30.0
    assert update.timeout_s() == 2.5


def test_nonsense_environment_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_TTL_S", "soon")
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_TIMEOUT_S", "-4")
    assert update.ttl_s() == update.DEFAULT_TTL_S
    assert update.timeout_s() == update.DEFAULT_TIMEOUT_S


def test_check_update_says_the_build_is_ahead_of_the_newest_release(monkeypatch, capsys):
    """While preparing a release the running build is newer than the last tag.

    Calling the older tag "latest" reads as a downgrade and hides that the check
    worked, so the wording has to name the situation instead.
    """
    from mokuro_bridge import cli

    monkeypatch.setattr(cli, "__version__", "0.7.5")
    monkeypatch.setattr(
        cli.update, "refresh", lambda **_kwargs: {"latest": "0.7.4", "update_available": False}
    )

    assert cli._check_update() == 0
    out = capsys.readouterr().out
    assert "0.7.4" in out
    assert "ahead of it" in out


def test_check_update_still_says_up_to_date_when_versions_match(monkeypatch, capsys):
    from mokuro_bridge import cli

    monkeypatch.setattr(cli, "__version__", "0.7.4")
    monkeypatch.setattr(
        cli.update, "refresh", lambda **_kwargs: {"latest": "0.7.4", "update_available": False}
    )

    assert cli._check_update() == 0
    assert "up to date" in capsys.readouterr().out


def _checked(latest="0.9.0", **overrides):
    payload = {
        "current": "0.8.0",
        "latest": latest,
        "update_available": True,
        "url": "https://example.invalid",
        "published_at": "",
        "error": None,
    }
    payload.update(overrides)
    return payload


def test_the_notice_names_the_new_version_and_the_one_command(monkeypatch, capsys):
    monkeypatch.setattr(update, "_cached", _checked("0.9.0"))
    line = update.announce_update()
    out = capsys.readouterr().out
    assert line == "a newer version 0.9.0 is available: run mokuro-bridge --update"
    assert "run mokuro-bridge --update" in out
    assert out.count("0.9.0") == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,  # nothing has been checked yet
        _checked(error="could not reach GitHub: no route to host"),
        _checked(update_available=False),
        _checked(latest=None),
    ],
)
def test_the_notice_is_silent_without_a_usable_newer_release(monkeypatch, capsys, payload):
    monkeypatch.setattr(update, "_cached", payload)
    assert update.notice_line() is None
    assert update.announce_update() is None
    assert capsys.readouterr().out == ""


def test_the_env_kill_switch_silences_the_notice(monkeypatch, capsys):
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_CHECK", "0")
    monkeypatch.setattr(update, "_cached", _checked("0.9.0"))
    assert update.notice_line() is None
    assert update.announce_update() is None
    assert capsys.readouterr().out == ""


def test_the_same_version_is_not_announced_twice(monkeypatch, capsys):
    monkeypatch.setattr(update, "_cached", _checked("0.9.0"))
    assert update.announce_update() is not None
    assert update.announce_update() is None
    assert capsys.readouterr().out.count("mokuro-bridge --update") == 1


def test_the_background_refresh_announces_a_newer_release(monkeypatch, capsys):
    monkeypatch.setattr(update, "refresh", lambda **_kwargs: _checked("0.9.0"))
    result = update._run_background_refresh(force=True)
    assert result["latest"] == "0.9.0"
    assert "run mokuro-bridge --update" in capsys.readouterr().out


def test_a_failing_logger_does_not_raise(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("console gone")

    monkeypatch.setattr(update, "_cached", _checked("0.9.0"))
    monkeypatch.setattr(update.log, "info", boom)
    assert update.announce_update() is None


def test_check_update_names_the_single_update_command(monkeypatch, capsys):
    from mokuro_bridge import cli

    monkeypatch.setattr(cli, "__version__", "0.7.4")
    monkeypatch.setattr(
        cli.update, "refresh",
        lambda **_kwargs: {"latest": "0.7.5", "update_available": True, "url": "https://example.invalid"},
    )

    assert cli._check_update() == 1
    assert "update with: mokuro-bridge --update" in capsys.readouterr().out


PIPX_PYTHON = "/home/dev/.local/pipx/venvs/mokuro-bridge/bin/python"
UV_PYTHON = "/home/dev/.local/share/uv/tools/mokuro-bridge/bin/python"
VENV_PYTHON = "/home/dev/.venv/bin/python"


@pytest.mark.parametrize(
    "executable,prefix,expected",
    [
        # the shapes each tool leaves behind, posable independently of this machine
        (PIPX_PYTHON, "/home/dev/.local/pipx/venvs/mokuro-bridge", "pipx"),
        (UV_PYTHON, "/home/dev/.local/share/uv/tools/mokuro-bridge", "uv"),
        (VENV_PYTHON, "/home/dev/.venv", "pip"),
        ("/usr/bin/python3.11", "/usr", "pip"),
    ],
)
def test_install_method_is_read_from_the_paths(executable, prefix, expected):
    from mokuro_bridge import cli

    found = cli._install_method(
        executable, prefix, repo_root=None, env={}, externally_managed=False
    )
    assert found == expected


def test_a_checkout_wins_over_any_package_manager_path(tmp_path):
    from mokuro_bridge import cli

    # Even an interpreter that looks like a pipx venv is a checkout when the
    # package is being run from one: ./run.sh must not be pipx-upgraded.
    found = cli._install_method(PIPX_PYTHON, "/home/dev", repo_root=tmp_path, env={})
    assert found == "git"


def test_uv_is_preferred_when_a_path_carries_both_markers():
    from mokuro_bridge import cli

    both = "/home/dev/.local/uv/tools/pipx-edge/mokuro-bridge/bin/python"
    found = cli._install_method(
        both, "/home/dev/.local/uv/tools/pipx-edge/mokuro-bridge",
        repo_root=None, env={}, externally_managed=False,
    )
    assert found == "uv"


def test_a_moved_tool_directory_is_named_by_its_variable():
    from mokuro_bridge import cli

    uv = cli._install_method("/usr/bin/python3", "/usr", repo_root=None, env={"UV_TOOL_DIR": "/srv/uv"})
    pipx = cli._install_method("/usr/bin/python3", "/usr", repo_root=None, env={"PIPX_HOME": "/srv/pipx"})
    assert (uv, pipx) == ("uv", "pipx")


def test_a_system_python_pip_refuses_is_not_guessed():
    from mokuro_bridge import cli

    found = cli._install_method(
        "/usr/bin/python3.11", "/usr", repo_root=None, env={}, externally_managed=True
    )
    assert found == "unknown"


@pytest.mark.parametrize(
    "method,expected",
    [
        ("pipx", ["pipx upgrade mokuro-bridge"]),
        ("uv", ["uv tool upgrade mokuro-bridge"]),
        ("pip", ["/opt/py/bin/python3 -m pip install --upgrade mokuro-bridge"]),
        ("unknown", []),
    ],
)
def test_each_install_method_maps_to_its_exact_command(method, expected):
    from mokuro_bridge import cli

    assert cli._update_commands(method, executable="/opt/py/bin/python3") == expected


def test_update_runs_only_the_detected_method(monkeypatch, capsys):
    from mokuro_bridge import cli

    ran = []
    monkeypatch.setattr(cli, "_install_method", lambda *a, **k: "pipx")
    monkeypatch.setattr(cli, "_run_shell", lambda command: ran.append(command) or 0)
    monkeypatch.setattr(cli, "_restart_hint", lambda: None)

    assert cli._run_update() == 0
    assert "about to run: pipx upgrade mokuro-bridge" in capsys.readouterr().out
    assert ran == ["pipx upgrade mokuro-bridge"]


def test_an_unknown_install_prints_the_candidates_and_runs_nothing(monkeypatch, capsys):
    from mokuro_bridge import cli

    ran = []
    monkeypatch.setattr(cli, "_install_method", lambda *a, **k: "unknown")
    monkeypatch.setattr(cli, "_run_shell", lambda command: ran.append(command) or 0)
    monkeypatch.setattr(cli, "_restart_hint", lambda: None)

    assert cli._run_update() == 2
    out = capsys.readouterr().out
    assert "pipx upgrade mokuro-bridge" in out
    assert "uv tool upgrade mokuro-bridge" in out
    assert "pip install --upgrade mokuro-bridge" in out
    assert ran == [], "nothing may run for an install that could not be detected"


def test_a_failed_upgrade_is_reported_with_its_status(monkeypatch, capsys):
    from mokuro_bridge import cli

    monkeypatch.setattr(cli, "_install_method", lambda *a, **k: "uv")
    monkeypatch.setattr(cli, "_run_shell", lambda command: 3)
    monkeypatch.setattr(cli, "_restart_hint", lambda: None)

    assert cli._run_update() == 3
    assert "update failed (3) at: uv tool upgrade mokuro-bridge" in capsys.readouterr().out
