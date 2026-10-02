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


# ── version parsing and comparison ──────────────────────────────────────


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


# ── check_for_update ────────────────────────────────────────────────────


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


# ── GitHub payload handling ─────────────────────────────────────────────


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


# ── caching, which is what /health reads ────────────────────────────────


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


# ── /health surface ─────────────────────────────────────────────────────


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

    assert set(update.health_fields()) == expected  # pending
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_CHECK", "0")
    assert set(update.health_fields()) == expected  # disabled
    monkeypatch.delenv("MOKURO_BRIDGE_UPDATE_CHECK")
    monkeypatch.setattr(
        update, "_cached",
        {"current": "0.6.0", "latest": "0.7.0", "update_available": True,
         "url": "https://example.invalid", "published_at": "", "error": None},
    )
    assert set(update.health_fields()) == expected  # ok
    monkeypatch.setattr(
        update, "_cached",
        {"current": "0.6.0", "latest": None, "update_available": False,
         "url": "", "published_at": "", "error": "no network"},
    )
    fields = update.health_fields()  # error
    assert set(fields) == expected
    assert fields["update_check"] == "error"
    assert fields["update_error"] == "no network"
    # An unusable URL must not be echoed as one.
    assert fields["update_url"] == update.RELEASES_PAGE_URL


# ── background checks are opt-in ────────────────────────────────────────


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
