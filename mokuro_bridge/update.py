"""Is there a newer mokuro-bridge release than the one running?

The bridge is distributed two ways: a git checkout (``./run.sh``, the launchd
agent) and a wheel from PyPI (``pipx install mokuro-bridge``, ``uv pip
install``). Both ask the same question, and GitHub Releases answers it for
both: every tag already gets a release carrying its changelog body, and the
endpoint needs no token for a public repo.

Two rules shape this module.

*Nothing here may raise into the caller.* A laptop on a train has no DNS, a
corporate proxy eats the request, and GitHub occasionally rate-limits. "Could
not reach github" must never be the reason the bridge fails to start or a
``/health`` probe 500s. Every public entry point returns a value carrying an
``error`` field instead, and the one exception (``UpdateError``) is caught by
``check_for_update``.

*Nothing here may block a request.* ``/health`` is polled by the userscript
while a volume is downloading, so it reads a cache and never opens a socket.
The network call happens on a daemon thread started by the CLI at startup, or
explicitly via ``python server.py --check-update``.

Stdlib at module level, on purpose: this must import on a base install, and
tests/test_imports.py walks every module in this package. The one third-party
import (httpx, for the request itself) is lazy, inside the function that needs
it.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Optional

from . import APP_NAME, __version__

# The canonical repo. Kept as a module constant so tests can point the fetcher
# at a stub server without patching the global urllib opener.
REPO = "GolyBidoof/mokuro-bridge"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{REPO}/releases"

# GitHub rejects a request with no User-Agent (403), and it is good manners to
# say which client is asking.
USER_AGENT = f"{APP_NAME}/{__version__}"

DEFAULT_TIMEOUT_S = 5.0
# Six hours: a version check is not time-critical, and a long TTL keeps a
# userscript hammering /health from turning into a GitHub rate limit.
DEFAULT_TTL_S = 6 * 60 * 60.0

_TRUTHY = ("1", "true", "yes", "on")
_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)(.*)$")


class UpdateError(RuntimeError):
    """The release list could not be fetched or understood."""


# ── version comparison ──────────────────────────────────────────────────


def parse_version(text: str) -> Optional[tuple[int, ...]]:
    """``"v0.6.0"`` -> ``(0, 6, 0)``; ``None`` when nothing numeric is found.

    A trailing suffix is deliberately dropped, so ``"0.7.0rc1"`` compares as
    ``(0, 7, 0)``. GitHub's ``releases/latest`` endpoint already excludes
    prereleases, so this only matters when someone points the check at a tag
    by hand, and reporting a prerelease as "available" is the safer error.
    """
    if not text:
        return None
    match = _VERSION_RE.match(str(text).strip())
    if not match:
        return None
    numeric, _suffix = match.groups()
    try:
        return tuple(int(part) for part in numeric.split("."))
    except ValueError:
        return None


def is_newer(candidate: str, current: str) -> bool:
    """True when *candidate* is a strictly newer version than *current*.

    Components are compared numerically and missing ones count as zero, so
    ``0.7`` is newer than ``0.6.9``. Unparseable input is never "newer": a
    failed comparison must not nag the user to update.
    """
    left = parse_version(candidate)
    right = parse_version(current)
    if left is None or right is None:
        return False
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right


# ── configuration ───────────────────────────────────────────────────────


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def checks_enabled() -> bool:
    """False when the user set MOKURO_BRIDGE_UPDATE_CHECK=0 (offline installs)."""
    return _env_flag("MOKURO_BRIDGE_UPDATE_CHECK", True)


def timeout_s() -> float:
    return _env_float("MOKURO_BRIDGE_UPDATE_TIMEOUT_S", DEFAULT_TIMEOUT_S)


def ttl_s() -> float:
    return _env_float("MOKURO_BRIDGE_UPDATE_TTL_S", DEFAULT_TTL_S)


# ── fetching ────────────────────────────────────────────────────────────


def _http_get_json(url: str, timeout: float) -> dict[str, Any]:
    """GET *url* and decode a JSON object. Raises UpdateError on any failure.

    httpx rather than urllib: it is already a base dependency (the fetch
    accelerator needs it), it honours the usual proxy variables, and it
    verifies TLS against certifi's bundle. A python.org macOS build ships no CA
    store of its own, so urllib fails there with CERTIFICATE_VERIFY_FAILED
    until someone runs "Install Certificates.command"; httpx just works.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a base dependency
        raise UpdateError("httpx is not installed, so the update check cannot run") from exc

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    try:
        response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.TimeoutException as exc:
        raise UpdateError(f"GitHub did not answer within {timeout:g}s") from exc
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not reach GitHub: {exc}") from exc
    except OSError as exc:  # no route, DNS failure, blocked socket
        raise UpdateError(f"could not reach GitHub: {exc}") from exc

    # Named separately because they are the two failures a user can act on:
    # 403 is the unauthenticated rate limit, 404 means the repo/tag moved.
    if response.status_code == 403:
        raise UpdateError("GitHub returned HTTP 403 (rate limited?)")
    if response.status_code == 404:
        raise UpdateError("no releases found for this repository")
    if response.status_code >= 400:
        raise UpdateError(f"GitHub returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise UpdateError("GitHub sent a response that is not JSON") from exc
    if not isinstance(payload, dict):
        raise UpdateError("GitHub sent an unexpected payload")
    return payload


def latest_release(*, timeout: Optional[float] = None, url: str = RELEASES_LATEST_URL) -> dict[str, Any]:
    """The newest published release as ``{version, tag, name, url, published_at}``.

    Raises UpdateError when the release cannot be read or carries no usable
    tag. *url* is a parameter so the test suite can point this at a stub.
    """
    if timeout is None:
        timeout = timeout_s()
    payload = _http_get_json(url, timeout)

    tag = str(payload.get("tag_name") or "").strip()
    version = parse_version(tag)
    if version is None:
        raise UpdateError(f"release has no usable version tag (got {tag!r})")
    normalised = ".".join(str(part) for part in version)

    return {
        "version": normalised,
        "tag": tag,
        "name": str(payload.get("name") or tag).strip(),
        "url": str(payload.get("html_url") or RELEASES_PAGE_URL).strip(),
        "published_at": str(payload.get("published_at") or "").strip(),
        "notes": str(payload.get("body") or "").strip(),
    }


def check_for_update(
    current: Optional[str] = None,
    *,
    timeout: Optional[float] = None,
    fetcher: Any = None,
) -> dict[str, Any]:
    """Compare the running version against the newest release. Never raises.

    Returns a dict with ``current``, ``latest`` (None when unknown),
    ``update_available``, ``url``, ``published_at`` and ``error`` (None on
    success). Callers print it, or read it into ``/health``.
    """
    current = current or __version__
    result: dict[str, Any] = {
        "current": current,
        "latest": None,
        "update_available": False,
        "url": RELEASES_PAGE_URL,
        "published_at": "",
        "error": None,
    }
    fetch = fetcher or latest_release
    try:
        release = fetch(timeout=timeout)
    except UpdateError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - a check must never break the caller
        result["error"] = f"update check failed: {exc}"
        return result

    result["latest"] = release.get("version")
    result["url"] = release.get("url") or RELEASES_PAGE_URL
    result["published_at"] = release.get("published_at") or ""
    result["update_available"] = is_newer(release.get("version") or "", current)
    return result


# ── cache, for /health ──────────────────────────────────────────────────
#
# /health reads this cache and never opens a socket. The cache is filled by an
# explicit --check-update, or by the daemon thread the CLI starts at boot.
# Auto-checks stay off until enable_auto_checks() is called, so importing the
# API (as the test suite does) can never touch the network.

_lock = threading.Lock()
_cached: Optional[dict[str, Any]] = None
_cached_at: float = 0.0
_inflight = False
_auto = False


def enable_auto_checks() -> None:
    """Allow background refreshes. Called by the CLI, not by the API import."""
    global _auto
    _auto = True


def auto_checks_enabled() -> bool:
    return _auto and checks_enabled()


def cache_age_s() -> Optional[float]:
    """Seconds since the cached result was fetched, or None when empty."""
    with _lock:
        if _cached is None:
            return None
        return max(0.0, time.monotonic() - _cached_at)


def cached() -> Optional[dict[str, Any]]:
    """A copy of the last completed check, or None. Never blocks on network."""
    with _lock:
        return dict(_cached) if _cached is not None else None


def refresh(*, force: bool = False, timeout: Optional[float] = None) -> dict[str, Any]:
    """Run a check, honouring the TTL, and cache it. Never raises.

    Returns the cached result when it is still fresh and *force* is not set.
    """
    global _cached, _cached_at
    if not force:
        age = cache_age_s()
        if age is not None and age < ttl_s():
            current = cached()
            if current is not None:
                return current

    result = check_for_update(timeout=timeout)
    # Cache successes and failures alike: a failed check must not mean every
    # later /health call retries the network.
    with _lock:
        _cached = result
        _cached_at = time.monotonic()
    return result


def _refresh_in_background(*, force: bool = False) -> None:
    """Run ``refresh`` on a daemon thread, at most one at a time."""
    global _inflight
    # Read the age *before* taking _lock: cache_age_s() takes _lock itself, and
    # threading.Lock is not reentrant. Holding it across that call deadlocked
    # the whole process on startup - before uvicorn had bound its port, so the
    # bridge never listened and the CLI printed its banner and then nothing,
    # with no traceback, on every run that had update checks enabled.
    if not force:
        age = cache_age_s()
        if age is not None and age < ttl_s():
            return
    with _lock:
        if _inflight:
            return
        _inflight = True

    def _work() -> None:
        global _inflight
        try:
            refresh(force=force)
        finally:
            with _lock:
                _inflight = False

    threading.Thread(target=_work, name="mokuro-bridge-update-check", daemon=True).start()


def maybe_refresh_async(*, force: bool = False) -> None:
    """Kick off a background check if updates are on. Safe to call per request."""
    if not auto_checks_enabled():
        return
    _refresh_in_background(force=force)


def health_fields() -> dict[str, Any]:
    """Cache-only update state for ``/health``. Never performs network I/O.

    Every key is always present, whatever the state, the same way ``/health``
    always carries ``fetchProxyPorts``: a client should be able to read a field
    without first checking that this run happened to include it.
    ``update_check`` is one of ``disabled`` (turned off by env), ``pending``
    (nothing cached yet), ``ok`` or ``error``.
    """
    fields: dict[str, Any] = {
        "update_check": "disabled" if not checks_enabled() else "pending",
        "latest_version": None,
        "update_available": False,
        "update_url": RELEASES_PAGE_URL,
        "update_error": None,
    }
    if not checks_enabled():
        return fields

    result = cached()
    if result is None:
        return fields

    error = result.get("error")
    fields["update_check"] = "error" if error is not None else "ok"
    fields["latest_version"] = result.get("latest")
    fields["update_available"] = bool(result.get("update_available"))
    fields["update_url"] = result.get("url") or RELEASES_PAGE_URL
    fields["update_error"] = error
    return fields
