"""Which mokuro is installed: upstream PyPI, the faster fork, or neither.

GolyBidoof/mokuro is a performance fork of kha-white/mokuro. It declares the
same distribution name, ``mokuro``, which is what lets a clean replacement
work: pip sees an upgrade of one distribution, removes the old copy, installs
the fork and replaces the ``mokuro`` console script. A differently named
package would coexist instead and the two would share files.

The version string cannot identify the flavor. An editable install of the fork
left ``importlib.metadata.version("mokuro")`` reporting ``0.2.2`` on a machine
whose fork source says ``0.4.0b``, so this module reads the install provenance
instead:

* ``direct_url.json`` in the distribution, which records the URL pip or uv
  installed from. A URL naming ``GolyBidoof/mokuro``, or a ``file:`` URL for a
  local checkout whose git remote names it, is the fork.
* the module path, for the ``MOKURO_REPO`` case where a checkout is placed on
  ``sys.path`` and no distribution metadata describes it. The fork is the only
  mokuro whose ``manga_page_ocr`` exposes ``detect_and_extract`` and whose
  ``mokuro_generator`` exposes ``ocr_batch_size``.

Everything here is stdlib and local. ``detect`` never opens a socket, never
raises, and caches its answer, so ``/health`` can call it on every poll.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from . import log

# The fork. Kept as constants so tests and docs share one spelling.
FORK_REPO = "GolyBidoof/mokuro"
FORK_SLUG = FORK_REPO.lower()
FORK_TAG = "v0.4.0"
FORK_GIT_URL = f"https://github.com/{FORK_REPO}.git"
FORK_RELEASE_PAGE = f"https://github.com/{FORK_REPO}/releases"

# The release wheel. Verified 2026-10-13: the v0.4.0 release lists
# mokuro-0.4.0-py3-none-any.whl (about 6.6 MB, state "uploaded") and the
# download URL below answers HTTP 200. A wheel installs faster than building
# from the tag and replaces the same distribution name the same way, so it is
# preferred. If the asset is ever removed, FORK_GIT_TARGET is the fallback:
# point INSTALL_TARGET at it and the rest of the code is unchanged.
#
# The version is 0.4.0, not 0.4.0b: the fork's "b" was a fork marker rather than
# a pre-release, and a stable number is what lets a plain `pip install --upgrade
# mokuro` prefer it instead of skipping it as a pre-release.
FORK_WHEEL_URL = (
    f"https://github.com/{FORK_REPO}/releases/download/{FORK_TAG}/"
    "mokuro-0.4.0-py3-none-any.whl"
)
FORK_GIT_TARGET = f"mokuro @ git+{FORK_GIT_URL}@{FORK_TAG}"
INSTALL_TARGET = f"mokuro @ {FORK_WHEEL_URL}"

# One switch for the startup notice. It is deliberately separate from
# MOKURO_BRIDGE_UPDATE_CHECK: this notice is about which OCR engine is
# installed, not about whether a newer mokuro-bridge exists, and someone who
# wants the update check on may still not want this line.
NOTICE_SWITCH = "MOKURO_BRIDGE_MOKURO_NOTICE"

SWITCH_COMMAND = "mokuro-bridge --install-mokuro"

# Files that only the fork carries. Read as text, so a missing or unreadable
# file simply means "not the fork".
_FORK_MARKERS = (
    ("manga_page_ocr.py", "detect_and_extract"),
    ("mokuro_generator.py", "ocr_batch_size"),
)

# The verification probe. Self-contained on purpose: it imports only the stdlib
# and mokuro, so it works from any working directory and in an isolated pipx or
# uv tool environment where the mokuro_bridge package itself may not be on the
# path of a bare ``python -c``. The working directory is dropped from sys.path
# first, so ``python -c`` run inside a mokuro-bridge checkout verifies the
# installed environment rather than a mokuro directory that happens to sit next
# to it.
PROBE_SCRIPT = (
    "import json, importlib.metadata as md, sys\n"
    "sys.path[:] = [p for p in sys.path if p not in ('', '.')]\n"
    "dist = md.distribution('mokuro')\n"
    "raw = dist.read_text('direct_url.json') or ''\n"
    "url = json.loads(raw).get('url', '') if raw else ''\n"
    "import mokuro\n"
    "print(json.dumps({\n"
    "    'version': dist.version,\n"
    "    'url': url,\n"
    "    'path': getattr(mokuro, '__file__', None),\n"
    "}))\n"
)

_TRUTHY = ("1", "true", "yes", "on")
_cached: Optional[dict[str, Any]] = None



def _distribution():
    """The installed ``mokuro`` distribution, or None. Never raises."""
    try:
        import importlib.metadata as metadata

        return metadata.distribution("mokuro")
    except Exception:  # PackageNotFoundError, a broken dist-info, anything
        return None


def _direct_url(dist) -> Optional[str]:
    """The URL this distribution was installed from, or None.

    A plain ``pip install mokuro`` from PyPI has no ``direct_url.json`` in its
    dist-info, so None here means "came from an index", which is the upstream
    case. A malformed file is also None: an unreadable provenance record must
    not crash a health probe.

    None is not proof of upstream by itself. A checkout placed on ``sys.path``
    (the ``MOKURO_REPO`` case) carries its own ``mokuro.egg-info``, which
    shadows the real dist-info and has no ``direct_url.json``; that is why the
    module path check exists alongside this one.
    """
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    url = payload.get("url") if isinstance(payload, dict) else None
    return str(url) if url else None


def _module_path() -> Optional[Path]:
    """The file behind the imported ``mokuro`` package, or None."""
    try:
        from . import ocr

        package = ocr._mokuro_pkg
    except Exception:
        return None
    location = getattr(package, "__file__", None) if package is not None else None
    return Path(location) if location else None


def _package_dir(module_path) -> Optional[Path]:
    """The ``mokuro`` package directory for a module file or directory."""
    if module_path is None:
        return None
    path = Path(module_path)
    return path if path.is_dir() else path.parent


def _looks_like_fork(package_dir) -> bool:
    """True when a mokuro package directory carries the fork's OCR API.

    The two source files are small and are only read once per process (the
    answer is cached), so this stays cheap enough for a health poll.
    """
    if package_dir is None:
        return False
    package_dir = Path(package_dir)
    for filename, marker in _FORK_MARKERS:
        try:
            text = (package_dir / filename).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if marker in text:
            return True
    return False


def _file_url_path(url: str) -> Optional[Path]:
    """The local path behind a ``file:`` URL, or None for anything else."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    # A file URL spells a Windows drive as /C:/..., and Path() keeps that leading slash,
    # so the path names nothing, the checkout walk finds no .git, and an installed fork
    # reports itself as upstream.
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _checkout_is_fork(path) -> bool:
    """True when the git checkout at *path* (or above it) is the fork.

    Reads ``.git/config`` directly rather than shelling out to git, so this
    works with no git binary and cannot hang. The nearest ``.git`` wins: a
    checkout nested inside another repository is described by its own remote.
    """
    if path is None:
        return False
    path = Path(path)
    candidates = [path, *list(path.parents)[:8]]
    for candidate in candidates:
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            config = dot_git / "config"
        elif dot_git.is_file():
            # A worktree or submodule points at a git dir elsewhere.
            try:
                pointer = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return False
            if not pointer.lower().startswith("gitdir:"):
                return False
            git_dir = Path(pointer.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = candidate / git_dir
            config = git_dir / "config"
        else:
            continue
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return FORK_SLUG in text.lower()
    return False


def _url_is_fork(url: Optional[str]) -> bool:
    """True when an install URL names the fork, directly or via a checkout."""
    if not url:
        return False
    if FORK_SLUG in url.lower():
        return True
    local = _file_url_path(url)
    return local is not None and _checkout_is_fork(local)


def classify_flavor(
    *,
    distribution_present: bool,
    direct_url: Optional[str] = None,
    package_dir=None,
    module_present: bool = False,
) -> str:
    """One of ``absent``, ``fork``, ``upstream`` or ``unknown``.

    Split out from ``detect`` so the decision can be tested against each
    provenance shape without this machine. ``unknown`` is the honest answer
    when something importable exists that no distribution metadata describes,
    or when a local checkout was installed but the module could not be found
    to confirm what it is.
    """
    if _url_is_fork(direct_url) or _looks_like_fork(package_dir):
        return "fork"
    if not distribution_present and not module_present:
        return "absent"
    if not distribution_present:
        return "unknown"
    if direct_url and _file_url_path(direct_url) is not None and not module_present:
        return "unknown"
    return "upstream"


def detect(*, refresh: bool = False) -> dict[str, Any]:
    """Describe the installed mokuro. Cached, local only, never raises.

    The keys are stable whatever the state, the same way ``update.health_fields``
    always returns the same set: a client reads a field rather than probing for
    one. ``version`` is the distribution metadata and is deliberately not used
    to decide the flavor.
    """
    global _cached
    if _cached is not None and not refresh:
        return dict(_cached)

    dist = _distribution()
    distribution_present = dist is not None
    version = None
    direct_url = None
    if dist is not None:
        try:
            version = dist.version
        except Exception:
            version = None
        direct_url = _direct_url(dist)

    module_path = _module_path()
    package_dir = _package_dir(module_path)
    module_present = module_path is not None

    flavor = classify_flavor(
        distribution_present=distribution_present,
        direct_url=direct_url,
        package_dir=package_dir,
        module_present=module_present,
    )
    result: dict[str, Any] = {
        "flavor": flavor,
        "installed": distribution_present or module_present,
        "distribution": distribution_present,
        "version": version,
        "direct_url": direct_url,
        "module_path": str(module_path) if module_path else None,
    }
    _cached = result
    return dict(result)


def health_fields() -> dict[str, Any]:
    """The ``/health`` surface for the engine flavor. Always the same key."""
    return {"mokuro_flavor": detect()["flavor"]}


# One line, once, at startup: the faster engine exists and the single command
# that switches to it. Nothing is installed here, and a failure of any kind is
# silence.


def notice_enabled() -> bool:
    """False when MOKURO_BRIDGE_MOKURO_NOTICE=0."""
    raw = os.environ.get(NOTICE_SWITCH)
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


def notice_line(result: Optional[dict[str, Any]] = None) -> Optional[str]:
    """The one-line nudge, or None when there is nothing to say.

    None when the notice is switched off, when the fork is already installed,
    and when no engine is installed at all: an absent engine is what the
    startup banner already reports, and the fork is a choice among two engines
    rather than a replacement for nothing.
    """
    if not notice_enabled():
        return None
    if result is None:
        result = detect()
    if result.get("flavor") not in ("upstream", "unknown"):
        return None
    return (
        f"the faster mokuro fork is available: run {SWITCH_COMMAND} "
        f"to replace this engine with {FORK_REPO} {FORK_TAG}"
    )


def announce(result: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Log the notice line if there is one. Never raises. Returns the line."""
    try:
        line = notice_line(result)
        if line is None:
            return None
        log.info("mokuro", line)
        return line
    except Exception:  # noqa: BLE001 - a notice must never break startup
        return None



def probe_command(executable: Optional[str] = None) -> str:
    """The shell line that imports mokuro and prints its provenance as JSON."""
    import shlex
    import sys

    return f"{shlex.quote(executable or sys.executable)} -c {shlex.quote(PROBE_SCRIPT)}"


def parse_probe(output: str) -> dict[str, Any]:
    """Read the probe's JSON line and add a ``flavor``. Never raises.

    The subprocess has already imported mokuro by the time this runs, so the
    module present bit is true: a parse failure means the import failed, and
    the caller treats that as a failed install.
    """
    result: dict[str, Any] = {"flavor": "unknown"}
    if not output:
        return result
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        package_dir = _package_dir(payload.get("path"))
        result.update(payload)
        result["flavor"] = classify_flavor(
            distribution_present=True,
            direct_url=payload.get("url") or None,
            package_dir=package_dir,
            module_present=bool(package_dir),
        )
        return result
    return result
