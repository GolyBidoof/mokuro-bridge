"""
mokuro-bridge — local OCR pipeline for manga captures → reader.mokuro.app.

A store-agnostic local HTTP service: any capture client (browser userscript,
headless scraper, curl, …) POSTs page images of a volume into a *session*;
the bridge OCRs them with mokuro and assembles the three artifacts
reader.mokuro.app consumes — <volume>.cbz, <volume>.mokuro, <volume>.webp —
into a per-series folder. Output can be kept locally or uploaded to MEGA.

Pipeline:
  POST /session/start              → create volume folder + session
  POST /session/{id}/page          → upload page bytes, queue OCR
  POST /session/{id}/page-local    → same-machine path ingest, queue OCR
  GET  /session/{id}/status        → capture/OCR progress
  GET  /sessions                   → list active sessions (multi-title)
  POST /session/{id}/finalize      → wait OCR, write .mokuro, pack, local and/or
                                     MEGA output (NDJSON progress stream)

OCR flushes in chunks (default 8 pages): detect each page, then one batched
recognize_text over all crops. Idle flush after ~1.5s if the chunk isn't full.
Env: OCR_CHUNK_SIZE, OCR_IDLE_FLUSH_S

Everything else is configurable through environment variables — see
MOKURO_BRIDGE_* / MEGA_* below and the README. MEGA upload is optional and
off by default; without it results land under the local output directory.

Requires: Python 3.10+, the stock mokuro package (`pip install mokuro`).
Optional: megatools + MEGA credentials for uploads.
"""

from __future__ import annotations

import os
import sys

# Quiet transformers (model-load banners, "generation flags not valid", …)
# before anything imports it — must be set before transformers is imported.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from . import APP_NAME, __version__

try:
    from .api import app
except ModuleNotFoundError as exc:
    # The commonest first-run failure is installing into one Python and running
    # another, which a bare "No module named 'fastapi'" hides completely.
    _missing = getattr(exc, "name", None) or "a dependency"
    raise SystemExit(
        f"\n  {APP_NAME} cannot start: {_missing} is not installed for this Python.\n"
        f"\n  interpreter: {sys.executable}\n"
        f"  install with: {sys.executable} -m pip install -r requirements.txt\n"
        "\n  If you already installed the requirements, pip targeted a different\n"
        "  Python. Prefer `python3 -m pip` over a bare `pip`, and activate the\n"
        "  virtualenv in every new terminal.\n"
    ) from None
from .config import (
    OUTPUT_DIR,
    WORK_DIR,
)
from .ocr import _MOKURO_REPO, _fork_supported, _mokuro_pkg
from . import accounts
from . import mokuro_flavor
from . import update
from .providers import (
    _UPLOAD_METHODS,
    _build_upload_methods,
    _default_upload_method,
    _method_current_folder,
    forget_account,
    setup_runner,
)


def _list_uploads() -> None:
    """Print every configured upload account (and whether it is usable)."""
    rows = []
    for method in _build_upload_methods().values():
        if method.id == "local":
            continue
        rows.append((
            method.id,
            method.name,
            "yes" if method.configured else "no",
            method.extra.get("creds_source") or "—",
            _method_current_folder(method.id),
        ))
    if not rows:
        print("no remote upload accounts configured")
        print("add one with: mokuro-bridge --setup-upload mega|drive|onedrive|webdav")
        return
    width = max(len(r[0]) for r in rows)
    print(f"{'ACCOUNT'.ljust(width)}  {'READY':5}  {'CREDS':11}  REMOTE ROOT")
    for account_id, _name, ready, creds, folder in rows:
        print(f"{account_id.ljust(width)}  {ready:5}  {creds:11}  {folder}")
    print()
    print(f"default: {_default_upload_method()}")


def _remove_upload(target: str) -> int:
    """Forget one upload account: metadata, its secret files, its keychain item."""
    try:
        result = forget_account(target)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    for note in result["notes"]:
        print(f"note: {note}")
    if not result["removed"]:
        print(f"nothing to remove for {result['id']}")
        return 0
    print(f"removed {result['id']}:")
    for path in result["removed"]:
        print(f"  {path}")
    return 0


def _repo_root():
    """The git checkout this file lives in, or None for an installed wheel."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() else None


# Lets a caller say "not a checkout" to _install_method; None means "detect".
_AUTO = object()


def _is_externally_managed(prefix=None) -> bool:
    """True for a system Python whose packages pip refuses to touch (PEP 668).

    Homebrew and Debian mark their interpreter this way, and
    ``pip install --upgrade`` there stops with "externally-managed-environment".
    That is the one plain-install case where guessing would be wrong, so the
    caller prints the candidate commands instead of running one.
    """
    import sysconfig
    from pathlib import Path

    if prefix is None:
        prefix = sys.prefix
    if prefix != getattr(sys, "base_prefix", prefix):
        return False  # a virtualenv always owns its own site-packages
    stdlib = sysconfig.get_path("stdlib")
    return bool(stdlib) and (Path(stdlib) / "EXTERNALLY-MANAGED").exists()


def _install_method(executable=None, prefix=None, *, repo_root=_AUTO,
                    env=None, externally_managed=None) -> str:
    """How this copy was installed: "git", "uv", "pipx", "pip" or "unknown".

    The upgrade command differs per method, and guessing wrong is worse than
    saying nothing: ``pip install --upgrade`` inside a pipx venv fights pipx,
    while ``uv tool upgrade`` on a wheel pip installed by hand finds no tool
    environment. The markers are the paths each tool leaves behind, and
    *executable*/*prefix* are injectable so the tests can pose a pipx venv, a
    uv tool path or a plain site-packages interpreter without this machine.

    Order matters when several tools exist: a git checkout wins, because a
    checkout run with ``./run.sh`` must never be upgraded by a package
    manager, and uv tool is checked before pipx so a path carrying both
    markers still resolves the same way every time.
    """
    from pathlib import Path

    if repo_root is _AUTO:
        repo_root = _repo_root()
    if repo_root is not None:
        return "git"

    executable = executable or sys.executable
    prefix = prefix or sys.prefix
    env = os.environ if env is None else env
    parts = {part.lower() for part in Path(executable).parts}
    parts |= {part.lower() for part in Path(prefix).parts}
    if "uv" in parts and "tools" in parts:
        return "uv"
    if "pipx" in parts:
        return "pipx"
    # A tool directory can be moved with its variable, so that is the only
    # marker left when the default path is not in use.
    if env.get("UV_TOOL_DIR"):
        return "uv"
    if env.get("PIPX_HOME") or env.get("PIPX_LOCAL_VENVS"):
        return "pipx"
    if externally_managed is None:
        externally_managed = _is_externally_managed(prefix)
    return "unknown" if externally_managed else "pip"


def _update_commands(method: str, *, executable=None) -> list:
    """The exact shell lines that upgrade an install of *method*.

    Empty for "unknown": an install that could not be told apart gets the
    candidate commands printed for a human to choose, never a guessed runner.
    """
    import shlex

    python = shlex.quote(executable or sys.executable)
    if method == "git":
        root = _repo_root()
        if root is None:
            return []
        return [
            f"git -C {shlex.quote(str(root))} pull --ff-only",
            f"{python} -m pip install -r {shlex.quote(str(root / 'requirements.txt'))}",
        ]
    if method == "pipx":
        return ["pipx upgrade mokuro-bridge"]
    if method == "uv":
        return ["uv tool upgrade mokuro-bridge"]
    if method == "pip":
        return [f"{python} -m pip install --upgrade mokuro-bridge"]
    return []


def _candidate_update_commands(*, executable=None) -> list:
    """Every upgrade line that could apply, for the cannot-tell fallback."""
    return [
        *_update_commands("pipx"),
        *_update_commands("uv"),
        *_update_commands("pip", executable=executable),
    ]


def _restart_hint() -> None:
    """Print the one command that restarts a service this install belongs to.

    mokuro-bridge is often started by launchd or systemd, and nothing here
    restarts it: bouncing a unit from inside the process that the unit runs is
    surprising, and a restart mid-OCR loses work.
    """
    from pathlib import Path

    plist = Path.home() / "Library" / "LaunchAgents" / "com.mokuro-bridge.plist"
    if plist.exists():
        print("restart the bridge with: launchctl kickstart -k gui/$(id -u)/com.mokuro-bridge")
        return
    unit = Path.home() / ".config" / "systemd" / "user" / "mokuro-bridge.service"
    if unit.exists():
        print("restart the bridge with: systemctl --user restart mokuro-bridge")
        return
    print("restart the bridge to pick up the update: stop it and run mokuro-bridge again")


def _run_shell(command: str, *, capture: bool = False):
    """Run one already-quoted line, returning its exit status.

    With *capture* the return is ``(status, output)``: verifying a mokuro
    install has to read the subprocess's own report, and keeping one runner
    means one place that knows how a command line is split and run.
    """
    import shlex
    import subprocess

    try:
        completed = subprocess.run(
            shlex.split(command), check=False, capture_output=capture, text=capture
        )
    except OSError as exc:
        print(f"could not run: {command} ({exc})")
        return (1, "") if capture else 1
    if capture:
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode


def _run_update() -> int:
    """Upgrade this install to the newest release, then say how to restart.

    The user asked for this explicitly, so it acts, unlike the startup notice.
    It prints every command before running any of them, and it runs nothing
    that was not detected: an install that cannot be told apart exits 2 with
    the candidate commands instead.
    """
    method = _install_method()
    commands = _update_commands(method)
    if not commands:
        print("could not tell how this copy was installed, so nothing was run; upgrade with one of:")
        for command in _candidate_update_commands():
            print(f"  {command}")
        return 2
    for command in commands:
        print(f"about to run: {command}")
    for command in commands:
        code = _run_shell(command)
        if code != 0:
            print(f"update failed ({code}) at: {command}")
            return code if code > 0 else 1
    print(f"updated mokuro-bridge ({method}); restart it to pick up the new version")
    _restart_hint()
    return 0


def _mokuro_install_commands(method: str, *, executable=None, target=None) -> list:
    """The exact shell lines that replace mokuro with the fork for *method*.

    Empty for a method this refuses to touch. The uninstall comes first on
    purpose: the fork declares the same distribution name, ``mokuro``, so pip
    only treats it as the replacement (and swaps the console script) when the
    old copy is gone first. An install of a differently named package would
    coexist and the two would share files.

    Each method gets the tool that owns its environment. A uv tool environment
    has no pip of its own and ``pip install`` in a pipx venv fights pipx's own
    bookkeeping, so those two use ``uv tool install --with`` and
    ``pipx runpip`` rather than a guessed ``python -m pip``.
    """
    import shlex

    python = shlex.quote(executable or sys.executable)
    quoted = shlex.quote(target or mokuro_flavor.INSTALL_TARGET)
    if method in ("pip", "git"):
        return [
            f"{python} -m pip uninstall -y mokuro",
            f"{python} -m pip install --upgrade {quoted}",
        ]
    if method == "pipx":
        return [
            "pipx runpip mokuro-bridge uninstall -y mokuro",
            f"pipx runpip mokuro-bridge install --upgrade {quoted}",
        ]
    if method == "uv":
        # --force is what makes this a replacement rather than an addition: uv
        # recreates the tool environment, so the old mokuro cannot linger.
        return [f"uv tool install --force --with {quoted} mokuro-bridge"]
    return []


def _print_mokuro_state(label: str, state: dict) -> None:
    """One block describing a detected or probed mokuro install."""
    version = state.get("version") or "none"
    present = "yes" if (state.get("distribution") or state.get("path")) else "no"
    print(
        f"{label}: mokuro distribution={present} "
        f"flavor={state.get('flavor')} version={version}"
    )
    if state.get("direct_url"):
        print(f"  installed from: {state['direct_url']}")
    if state.get("module_path") or state.get("path"):
        print(f"  module:         {state.get('module_path') or state.get('path')}")


def _needs_onboarding() -> bool:
    """Whether this looks like a first run: no managed account, settings or state.

    A credential found in the OS store alone does not count. That case is how a
    machine that once ran the terminal wizard looks, and it should still get the
    click-through setup page so the user can see what exists and reconnect it.
    """
    try:
        from . import accounts as _accounts

        # list_instances() also yields the implicit per-provider defaults; only
        # files mean the user (or the setup page) actually created an account.
        directory = _accounts.accounts_dir()
        if directory.is_dir() and any(directory.glob("*.json")):
            return False
    except Exception:
        pass
    try:
        from . import settings as _settings

        if _settings.load(refresh=True):
            return False
    except Exception:
        pass
    return True


def _reset_credentials() -> int:
    """Delete every saved bridge credential and bit of setup state, then report."""
    import shutil

    from . import accounts as _accounts
    from . import providers as _providers
    from . import settings as _settings
    from .config import (
        DRIVE_CREDS_FILE,
        MEGA_CREDS_FILE,
        ONEDRIVE_TOKEN_FILE,
        WEBDAV_CREDS_FILE,
        WORK_DIR,
    )
    from .creds import _delete_mega_creds_keychain, _keychain_mega_account

    removed: list[str] = []

    account = _keychain_mega_account()
    if account and _delete_mega_creds_keychain(account):
        removed.append(f"MEGA keychain item for {account}")
    try:
        import keyring

        for key in ("email", account):
            if key and keyring.get_password("mokuro-bridge", key):
                keyring.delete_password("mokuro-bridge", key)
                removed.append(f"keychain entry {key}")
    except Exception:
        pass
    try:
        removed += [f"keychain: {item}" for item in _providers.webdav.forget_default_keychain()]
    except Exception:
        pass

    for path in (
        MEGA_CREDS_FILE,
        DRIVE_CREDS_FILE,
        ONEDRIVE_TOKEN_FILE,
        WEBDAV_CREDS_FILE,
        _settings.SETTINGS_FILE,
        WORK_DIR / "local_dir_default.json",
        WORK_DIR / "upload_method_default.json",
    ):
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass
    for directory in (_accounts.accounts_dir(), WORK_DIR / ".bridge_sessions"):
        try:
            if directory.is_dir():
                shutil.rmtree(directory)
                removed.append(str(directory))
        except OSError:
            pass

    print("mokuro-bridge credentials reset.")
    if removed:
        for item in removed:
            print(f"  removed {item}")
    else:
        print("  nothing to remove; already clean.")
    print("")
    print("Start mokuro-bridge again for the setup guide.")
    return 0


def _run_install_mokuro() -> int:
    """Replace the installed mokuro with the faster fork, then verify it.

    Exit 0 installed and verified, 1 the install or the verification failed, 2
    refused: an externally managed interpreter (PEP 668) or a mokuro-bridge
    install this cannot safely change. Nothing runs without the explicit flag,
    and every command is printed before it is run.
    """
    before = mokuro_flavor.detect(refresh=True)
    _print_mokuro_state("before", before)
    if before["flavor"] == "fork":
        print("the fast mokuro fork is already installed; nothing to do")
        return 0

    if _is_externally_managed():
        print("refusing: this interpreter is externally managed (PEP 668), so pip must not")
        print("install mokuro into it. Create a virtualenv, or install the bridge with")
        print(f"pipx or uv, and run {mokuro_flavor.SWITCH_COMMAND} there. To do it by hand:")
        print(f"  {mokuro_flavor.INSTALL_TARGET}")
        return 2

    method = _install_method(externally_managed=False)
    commands = _mokuro_install_commands(method)
    if not commands:
        print("refusing: could not tell how this mokuro-bridge was installed, so there is")
        print("no environment this can safely change. Install the fork by hand with:")
        print(f"  {mokuro_flavor.INSTALL_TARGET}")
        return 2

    print(f"mokuro-bridge install method: {method}")
    for command in commands:
        print(f"about to run: {command}")
    for command in commands:
        code = _run_shell(command)
        if code != 0:
            print(f"install failed ({code}) at: {command}")
            return code if code > 0 else 1

    # A separate interpreter is the only honest check: this process already
    # imported whatever mokuro was there when it started.
    verify = mokuro_flavor.probe_command()
    print(f"verifying: {verify}")
    status, output = _run_shell(verify, capture=True)
    after = mokuro_flavor.parse_probe(output)
    if status != 0:
        print(f"verification failed ({status}): could not import mokuro")
        if output.strip():
            print(output.strip())
        return 1
    _print_mokuro_state("after", after)
    if after.get("flavor") != "fork":
        print("mokuro was installed, but the import does not report the fork; leaving it as is")
        return 1
    print("done: the fast mokuro fork is installed and importable")
    print("restart the bridge to load it: stop the service and run mokuro-bridge again")
    return 0


def _check_update() -> int:
    """Report whether a newer release exists. Exit 0 up to date, 1 available, 2 failed.

    An explicit run overrides MOKURO_BRIDGE_UPDATE_CHECK=0: that variable turns
    off the *background* check, and asking for one by hand is unambiguous.
    """
    print(f"current: {__version__}")
    result = update.refresh(force=True)
    if result.get("error"):
        print(f"error:   {result['error']}")
        print(f"release: {update.RELEASES_PAGE_URL}")
        return 2

    latest = result.get("latest") or "?"
    if not result.get("update_available"):
        # A build ahead of the newest published release is the normal state while
        # preparing one. Calling the older tag "latest" reads as a downgrade, so
        # name the situation instead.
        if latest != "?" and update.is_newer(__version__, latest):
            print(f"latest:  {latest} (this build, {__version__}, is ahead of it)")
        else:
            print(f"latest:  {latest} (up to date)")
        return 0

    print(f"latest:  {latest} (newer than {__version__})")
    print(f"release: {result.get('url')}")
    print("update with: mokuro-bridge --update")
    return 1


def _check_series_reference() -> int:
    """0 pin current, 1 upstream moved, 2 the check could not run."""
    from . import series_reference

    problems = series_reference.verify()
    if problems:
        print("pin is not usable:")
        for problem in problems:
            print(f"  {problem}")
        return 2
    pin = series_reference.load_pin()
    for upstream in pin.get("upstream", []):
        print(f"{upstream['repo']} @ {upstream['commit'][:12]} ({upstream.get('license')})")
    report = series_reference.check(pin)
    print(f"pinned files checked: {report['checked']}")
    for entry in report["mismatched"]:
        print(f"  MISMATCH {entry['repo']}:{entry['path']} pinned={entry['sha256'][:12]} actual={entry['actual'][:12]}")
    for entry in report["moved"]:
        print(f"  MOVED    {entry['repo']} pinned={entry['pinned'][:12]} head={entry['head'][:12]}")
    for entry in report["drift"]:
        print(f"  DRIFT    {entry['repo']}:{entry['path']} changed since the pin")
    for error in report["errors"]:
        print(f"  ERROR    {error}")
    if not (report["mismatched"] or report["errors"] or report["drift"] or report["moved"]):
        print("the pin matches upstream exactly; nothing to refresh")
    if report["errors"] or report["mismatched"]:
        return 2
    return 1 if (report["drift"] or report["moved"]) else 0


def _refresh_series_reference() -> int:
    from . import series_reference

    print(f"pin file: {series_reference.PIN_PATH}")
    try:
        refreshed = series_reference.refresh()
    except Exception as error:
        print(f"error: {error}")
        return 2
    for upstream in refreshed.get("upstream", []):
        print(f"{upstream['repo']} -> {upstream['commit']}")
        for entry in upstream.get("files", []):
            print(f"  {entry['role']:<18} {entry['sha256'][:12]} {entry['bytes']:>8}  {entry['path']}")
    print("The port in mokuro_bridge/series_json.py and the parity fixture in")
    print("tests/fixtures/series_json/ describe the OLD pin: review the diff and update")
    print("both before releasing, then run: python -m pytest tests/test_series_json.py")
    return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog=APP_NAME, description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--setup-upload",
        metavar="METHOD",
        default=None,
        help="Interactively configure/authenticate an upload account "
        "(mega, drive, onedrive, webdav, optionally with an account name such "
        "as 'mega:work'). Running it again with a new name adds a second "
        "account. e.g. --setup-upload drive --name main",
    )
    parser.add_argument(
        "--name",
        default=None,
        metavar="ACCOUNT",
        help="Account name for --setup-upload (default: ask, offering the "
        "first free name). Use a new name to add another account; the target "
        "is then addressed as '<provider>:<name>'.",
    )
    parser.add_argument(
        "--root",
        default="",
        metavar="REMOTE_DIR",
        help="Remote root folder for the account being set up (default: the "
        "provider's usual root, e.g. /Root/mokuro-reader for MEGA).",
    )
    parser.add_argument(
        "--label",
        default="",
        metavar="TEXT",
        help="Human label shown in --list-uploads / /health for this account.",
    )
    parser.add_argument(
        "--list-uploads",
        action="store_true",
        help="List configured upload accounts and exit.",
    )
    parser.add_argument(
        "--remove-upload",
        default=None,
        metavar="ACCOUNT",
        help="Forget an upload account (e.g. 'mega:work'): metadata, its "
        "credential file(s) and its OS keychain item. The default account is "
        "addressed by the bare provider name ('mega').",
    )
    parser.add_argument(
        "--reset-credentials",
        action="store_true",
        help="Delete saved accounts and credentials, including the MEGA keychain "
        "item, settings, remembered folders and session state. For a clean "
        "first run; your finished volumes are not touched.",
    )
    parser.add_argument(
        "--setup-mega",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--setup-bunko",
        nargs="?",
        const="",
        default=None,
        metavar="METHOD",
        help="Install the newest mokuro-bunko development build (the 0.8 remote-"
        "storage branch head, built from source), sign it "
        "in to the bridge's default upload account and start it, so Bunko "
        "compiles series.json in the cloud and the bridge stops writing it. "
        "Pass an account id such as 'mega' or 'mega:work'; the default upload "
        "account is used when omitted. The first run builds Bunko with cargo "
        "and takes several minutes.",
    )
    parser.add_argument(
        "--bunko-status",
        action="store_true",
        help="Print whether mokuro-bunko is installed, configured and running.",
    )
    parser.add_argument(
        "--stop-bunko",
        action="store_true",
        help="Stop the mokuro-bunko instance started by --setup-bunko.",
    )
    parser.add_argument(
        "--bunko-logs",
        action="store_true",
        help="Print the tail of the mokuro-bunko log.",
    )
    parser.add_argument(
        "--update-bunko",
        action="store_true",
        help="Move the installed mokuro-bunko to the newest development commit, "
        "rebuilding it when the branch head moved, and restart the running "
        "instance. MOKURO_BUNKO_REF pins a branch, tag or commit; "
        "MOKURO_BUNKO_OFFLINE=1 keeps what is installed.",
    )
    parser.add_argument("--host", default=os.environ.get("MOKURO_BRIDGE_HOST") or "127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MOKURO_BRIDGE_PORT") or "62642"),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the setup page in a browser on a first run.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print paths, engine and proxy details at startup.",
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="Check GitHub for a newer release, print how to install it and "
        "exit. Exit codes: 0 up to date, 1 update available, 2 check failed.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Upgrade this install to the newest release, then print how to "
        "restart the service. Detects pipx, uv tool, a plain pip install and a "
        "git checkout; prints the candidate commands instead of running one "
        "when it cannot tell. Exit 0 when upgraded, 2 when not attempted.",
    )
    parser.add_argument(
        "--install-mokuro",
        action="store_true",
        help="Replace the installed mokuro with the faster GolyBidoof fork and "
        "verify it imports. Reports the current distribution, version and "
        "flavor first, prints every command before running it, and refuses "
        "(exit 2) on an externally managed interpreter or an install it cannot "
        "safely change. Exit codes: 0 installed and verified, 1 install or "
        "verification failed, 2 refused.",
    )
    parser.add_argument(
        "--check-series-reference",
        action="store_true",
        help="Re-fetch the pinned upstream files that define series.json. "
        "Exit codes: 0 matching and current, 1 upstream moved, 2 check failed.",
    )
    parser.add_argument(
        "--refresh-series-reference",
        action="store_true",
        help="Re-pin those files at their branches' HEADs. Review the diff: the "
        "port and the parity fixture describe the previous pin.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Live terminal status display: health, destinations, queue and "
        "current upload, refreshed in place. Keys: r refresh, c dashboard URL, "
        "? help, q quit.",
    )
    parser.add_argument(
        "--status-url",
        default="",
        metavar="URL",
        help="Bridge to watch with --status (default http://127.0.0.1:62642, or "
        "MOKURO_BRIDGE_HOST/MOKURO_BRIDGE_PORT).",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="Refresh interval for --status (default 2).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args()

    if args.install_mokuro:
        raise SystemExit(_run_install_mokuro())
    if args.status:
        from . import status_tui
        raise SystemExit(status_tui.main(
            sys.argv[1:],
            host=args.host,
            port=args.port,
            status_url=args.status_url,
            status_interval=args.status_interval,
        ))
    if args.check_series_reference:
        raise SystemExit(_check_series_reference())
    if args.refresh_series_reference:
        raise SystemExit(_refresh_series_reference())
    if args.update:
        raise SystemExit(_run_update())
    if args.check_update:
        raise SystemExit(_check_update())
    if args.list_uploads:
        _list_uploads()
        return
    if args.reset_credentials:
        raise SystemExit(_reset_credentials())
    if args.remove_upload:
        raise SystemExit(_remove_upload(args.remove_upload))

    if args.bunko_status:
        from . import bunko_agent

        state = bunko_agent.load_state()
        print(bunko_agent.status_line(state))
        running = bool(state and bunko_agent._pid_alive(state.pid))
        raise SystemExit(0 if running else 1)
    if args.stop_bunko:
        from . import bunko_agent

        state = bunko_agent.load_state()
        if state is None:
            print("mokuro-bunko: not configured")
            raise SystemExit(1)
        bunko_agent.stop_server(state)
        state.save()
        print(bunko_agent.status_line(state))
        return
    if args.bunko_logs:
        from . import bunko_agent

        log_file = bunko_agent.log_path()
        if not log_file.is_file():
            print(f"no Bunko log at {log_file}")
            raise SystemExit(1)
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-80:]))
        return
    if args.setup_bunko is not None:
        from . import bunko_agent

        try:
            state = bunko_agent.setup(args.setup_bunko or None, log=print)
        except bunko_agent.BunkoAgentError as error:
            print(f"error: {error}")
            raise SystemExit(2)
        print(bunko_agent.status_line(state))
        return
    if args.update_bunko:
        from . import bunko_agent

        if bunko_agent.load_state() is None:
            print("mokuro-bunko: not configured (run mokuro-bridge --setup-bunko)")
            raise SystemExit(1)
        try:
            info, changed = bunko_agent.update(log=print)
        except bunko_agent.BunkoAgentError as error:
            print(f"error: {error}")
            raise SystemExit(2)
        print(
            f"mokuro-bunko: {'updated to' if changed else 'already at'} "
            f"{info.commit[:9]} ({info.label})"
        )
        print(bunko_agent.status_line())
        return

    setup_method = args.setup_upload or ("mega" if args.setup_mega else None)
    if setup_method:
        try:
            provider, name = accounts.parse_method_id(setup_method)
        except ValueError:
            print(
                f"error: unknown upload method '{setup_method}' "
                f"(available: {', '.join(accounts.PROVIDERS)}, plus an optional "
                "':<name>')"
            )
            raise SystemExit(2)
        explicit = (args.name or "").strip().lower()
        if explicit and name != accounts.DEFAULT_NAME and explicit != name:
            print(
                f"error: two different account names given: '{name}' "
                f"(in {setup_method}) and '{explicit}' (in --name)"
            )
            raise SystemExit(2)
        chosen = explicit or (name if name != accounts.DEFAULT_NAME else "")
        runner = setup_runner(provider)
        if runner is None:
            print(f"error: no setup wizard is registered for '{provider}'")
            raise SystemExit(2)
        runner(chosen or None, root=args.root, label=args.label)
        return

    import uvicorn

    from . import fetchproxy

    configured = [m.id for m in _UPLOAD_METHODS.values() if m.configured]
    remote_configured = [m.id for m in _UPLOAD_METHODS.values() if m.configured and m.id != "local"]
    first_run = _needs_onboarding()
    upload_line = f"{_default_upload_method()} (default)"
    if configured:
        upload_line += ", " + ", ".join(c for c in configured if c != _default_upload_method())
    verbose = args.verbose or os.environ.get("MOKURO_BRIDGE_VERBOSE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    page_url = f"http://{display_host}:{args.port}/"
    print("=" * 60)
    print(f"  {APP_NAME} {__version__} is running")
    print("=" * 60)
    print()
    print("  Open this page to set things up and watch the bridge:")
    print(f"    {page_url}")
    print()
    if first_run:
        print("  First time? The page walks you through connecting a destination.")
        print()
    print("  Keep this window open while you use the bridge.")
    print("  Press Ctrl-C to stop.")
    if verbose:
        print()
        print(f"  work dir:   {WORK_DIR}")
        print(f"  output dir: {OUTPUT_DIR}")
        print(f"  upload:     {upload_line}")
        if _MOKURO_REPO is not None:
            print(f"  mokuro:     {_MOKURO_REPO} (fork API: {_fork_supported()})")
        elif _mokuro_pkg is not None:
            print(f"  mokuro:     {getattr(_mokuro_pkg, '__file__', '?')} (fork API: {_fork_supported()})")
        else:
            print("  mokuro:     NOT INSTALLED — run: pip install mokuro")
        if update.checks_enabled():
            print("  updates:    checked in the background (see /health)")
        else:
            print("  updates:    disabled (MOKURO_BRIDGE_UPDATE_CHECK=0)")

    reload_on = os.environ.get("UVICORN_RELOAD", "0").lower() in ("1", "true", "yes")

    # Extra listening ports, each worth 6 more browser sockets. See
    # mokuro_bridge/fetchproxy.py for why the port count is what matters.
    proxy_sockets: list = []
    proxy_ports: list[int] = []
    if fetchproxy.FETCH_PORTS > 0 and not reload_on:
        # Two descriptors per proxied page (client + upstream), so ask for well
        # over 256 before opening the ports.
        _fd_limit = _raise_fd_limit(max(8192, fetchproxy.FETCH_PORTS * 16 + 2048))
        requested = [args.port + i for i in range(1, fetchproxy.FETCH_PORTS + 1)]
        proxy_sockets, proxy_ports = fetchproxy.bind_sockets(args.host, requested)
        fetchproxy.ACTIVE_PORTS = proxy_ports
    if verbose and proxy_ports:
        print(
            f"  fetch proxy: {len(proxy_ports)} extra port(s) "
            f"{proxy_ports[0]}-{proxy_ports[-1]}  →  "
            f"{6 * len(proxy_ports)} browser sockets for the downloader"
        )
        print(
            "  proxy hosts: "
            + ("any public host the caller names"
               if fetchproxy.UPSTREAM_PATTERNS == ["*"]
               else ", ".join(fetchproxy.UPSTREAM_PATTERNS))
        )
        print(f"  fd limit:   {_fd_limit} (raised for the proxy)")
    elif verbose and fetchproxy.FETCH_PORTS > 0 and reload_on:
        print("  fetch proxy: off (UVICORN_RELOAD=1 is incompatible with extra ports)")
    if os.environ.get("MOKURO_BRIDGE_BUNKO_AUTOSTART", "1").strip().lower() not in ("0", "false", "no", "off"):
        try:
            from . import bunko_agent

            _bunko_state = bunko_agent.ensure_running(
                log=lambda message: print(f"  bunko:      {message}") if verbose else None
            )
            if _bunko_state is not None and verbose:
                print(f"  bunko:      {bunko_agent.status_line(_bunko_state)}")
        except Exception as error:  # optional; never keep the bridge down
            if verbose:
                print(f"  bunko:      not started ({error})")

    # One line when the engine is the slower upstream build or one this cannot
    # name, naming the single command that switches to the faster fork. It
    # reads local metadata only, so it cannot delay startup, and it is off with
    # MOKURO_BRIDGE_MOKURO_NOTICE=0. See mokuro_bridge/mokuro_flavor.py.
    mokuro_flavor.announce()

    # A version check is never a reason to delay startup: this only arms the
    # daemon thread that /health reads from, and it is skipped entirely when
    # MOKURO_BRIDGE_UPDATE_CHECK=0.
    if update.checks_enabled():
        update.enable_auto_checks()
        update.maybe_refresh_async()

    opening_disabled = args.no_browser or os.environ.get(
        "MOKURO_BRIDGE_OPEN_BROWSER", ""
    ).strip().lower() in ("0", "false", "no", "off")
    if (
        first_run
        and not opening_disabled
        and sys.stdout.isatty()
        and sys.stdin.isatty()
    ):
        import threading
        import webbrowser

        setup_url = page_url + "?wizard=1"
        print("  Opening the setup guide in your browser…")
        print()
        threading.Timer(1.2, lambda: webbrowser.open(setup_url)).start()

    if not proxy_sockets:
        uvicorn.run(
            "mokuro_bridge.api:app",
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,  # keep the console clean — mokuro-bridge logs its own progress
            # Reload wipes in-memory sessions mid-scrape. Opt in with UVICORN_RELOAD=1.
            reload=reload_on,
            reload_excludes=["mokuro/*", "**/mokuro/**", "**/__pycache__/**"],
        )
        return

    _serve_extra_ports(args.host, args.port, proxy_ports, proxy_sockets, app)


def _raise_fd_limit(target: int) -> int:
    """Raise RLIMIT_NOFILE toward *target*, returning the resulting soft limit.

    Every proxied page holds a descriptor on *each* side of the bridge (browser
    -> bridge, bridge -> CDN), so 48 ports x 6 sockets is ~600 descriptors at
    once. macOS hands a launchd agent a soft limit of 256, which caps the whole
    scheme at roughly 96 concurrent and surfaces as ECONNRESET in the browser
    and "OSError: Too many open files" on accept() — with no obvious hint that
    a ulimit is the cause. Raising it here covers launchd, run.sh and manual
    runs alike, unlike editing the plist.
    """
    try:
        import resource
    except ImportError:  # non-POSIX
        return 0
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        ceiling = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if ceiling > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
            soft = ceiling
        return soft
    except (OSError, ValueError):
        return 0  # best effort — the bridge still runs, just with fewer sockets


def _serve_extra_ports(host, port, proxy_ports, proxy_sockets, main_app) -> None:
    """Run the bridge and the fetch proxy side by side in one event loop."""
    import asyncio
    import contextlib
    import signal

    import uvicorn

    from . import fetchproxy

    servers = [
        uvicorn.Server(uvicorn.Config(
            main_app, host=host, port=port,
            log_level="warning", access_log=False,
        )),
        uvicorn.Server(uvicorn.Config(
            fetchproxy.build_app(proxy_ports),
            log_level="warning", access_log=False,
        )),
    ]

    # uvicorn's capture_signals() installs a handler per server and restores the
    # previous one on exit, so with two servers the second wins and Ctrl-C would
    # stop only that one — the process would hang. Disable it on both and drive
    # should_exit ourselves.
    for server in servers:
        server.capture_signals = contextlib.nullcontext

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        def _stop(*_args) -> None:
            for server in servers:
                server.should_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows proactor loop, or not the main thread

        await asyncio.gather(servers[0].serve(), servers[1].serve(sockets=proxy_sockets))

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

# Kept so anything importing the old private name still works.
_main = main
