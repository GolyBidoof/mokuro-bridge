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

from mokuro_bridge import APP_NAME, __version__  # noqa: F401  (module-level compat)

try:
    from mokuro_bridge.api import app
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
from mokuro_bridge.config import (
    OUTPUT_DIR,
    WORK_DIR,
)
from mokuro_bridge.ocr import _MOKURO_REPO, _fork_supported, _mokuro_pkg
from mokuro_bridge import accounts
from mokuro_bridge.creds import _delete_mega_creds_keychain
from mokuro_bridge.providers import (
    _UPLOAD_METHODS,
    _build_upload_methods,
    _default_upload_method,
    _method_current_folder,
    _run_setup_drive,
    _run_setup_mega,
    _run_setup_onedrive,
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
        print("add one with: python server.py --setup-upload mega|drive|onedrive")
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
        provider, name = accounts.parse_method_id(target)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    instance = accounts.load_instance(provider, name)
    label = instance.id if instance is not None else target
    email = instance.email if instance is not None else ""
    # Emails other accounts still rely on — deleting the shared keychain item
    # would break them (see accounts.sibling_emails).
    siblings = accounts.sibling_emails(provider, name) if provider == "mega" else set()

    removed = accounts.delete_instance(provider, name)
    legacy = accounts.legacy_secret_path(provider, name)
    if legacy is not None:
        removed += accounts.remove_paths([legacy])

    if provider == "mega":
        if email and email in siblings:
            print(
                f"note: {email} is still used by another MEGA account — its "
                "keychain item was left in place."
            )
        elif email:
            if _delete_mega_creds_keychain(email):
                removed.append(f"keychain:mega.nz/{email}")
        else:
            print(
                "note: no account file records a MEGA email, so any keychain "
                "item was left alone. Delete it in Keychain Access "
                "(service mega.nz) if you want it gone."
            )

    if not removed:
        print(f"nothing to remove for {label}")
        return 0
    print(f"removed {label}:")
    for path in removed:
        print(f"  {path}")
    return 0


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog=APP_NAME, description=__doc__.splitlines()[0])
    parser.add_argument(
        "--setup-upload",
        metavar="METHOD",
        default=None,
        help="Interactively configure/authenticate an upload account "
        "(mega, drive, onedrive, optionally with an account name such as "
        "'mega:work'). Running it again with a new name adds a second account. "
        "e.g. --setup-upload drive --name main",
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
        "--setup-mega",
        action="store_true",
        help="[alias for --setup-upload mega] Interactively store MEGA "
        "credentials in the OS keychain / credential store (macOS Keychain, "
        "Windows Credential Manager, Linux Secret Service) or, failing that, "
        "a 0600 credentials file.",
    )
    parser.add_argument("--host", default=os.environ.get("MOKURO_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MOKURO_BRIDGE_PORT", "62642")),
    )
    args = parser.parse_args()

    if args.list_uploads:
        _list_uploads()
        return
    if args.remove_upload:
        raise SystemExit(_remove_upload(args.remove_upload))

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
        runner = {
            "mega": _run_setup_mega,
            "drive": _run_setup_drive,
            "onedrive": _run_setup_onedrive,
        }[provider]
        runner(chosen or None, root=args.root, label=args.label)
        return

    import uvicorn

    from mokuro_bridge import fetchproxy

    configured = [m.id for m in _UPLOAD_METHODS.values() if m.configured]
    upload_line = f"{_default_upload_method()} (default)"
    if configured:
        upload_line += ", " + ", ".join(c for c in configured if c != _default_upload_method())
    print("=" * 60)
    print(f"  {APP_NAME} v{__version__} on http://{args.host}:{args.port}")
    print(f"  work dir:   {WORK_DIR}")
    print(f"  output dir: {OUTPUT_DIR}")
    print(f"  upload:     {upload_line}")
    if _MOKURO_REPO is not None:
        print(f"  mokuro:     {_MOKURO_REPO} (fork API: {_fork_supported()})")
    elif _mokuro_pkg is not None:
        print(f"  mokuro:     {getattr(_mokuro_pkg, '__file__', '?')} (fork API: {_fork_supported()})")
    else:
        print("  mokuro:     NOT INSTALLED — run: pip install mokuro")

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
    if proxy_ports:
        print(
            f"  fetch proxy: {len(proxy_ports)} extra port(s) "
            f"{proxy_ports[0]}–{proxy_ports[-1]}  →  "
            f"{6 * len(proxy_ports)} browser sockets for the downloader"
        )
        print(
            "  proxy hosts: "
            + ("any public host the caller names"
               if fetchproxy.UPSTREAM_PATTERNS == ["*"]
               else ", ".join(fetchproxy.UPSTREAM_PATTERNS))
        )
        print(f"  fd limit:   {_fd_limit} (raised for the proxy)")
    elif fetchproxy.FETCH_PORTS > 0 and reload_on:
        print("  fetch proxy: off (UVICORN_RELOAD=1 is incompatible with extra ports)")
    print("=" * 60)

    if not proxy_sockets:
        uvicorn.run(
            "server:app",
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

    from mokuro_bridge import fetchproxy

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


if __name__ == "__main__":
    _main()
