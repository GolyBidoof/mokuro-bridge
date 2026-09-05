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

# Quiet transformers (model-load banners, "generation flags not valid", …)
# before anything imports it — must be set before transformers is imported.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

from mokuro_bridge import APP_NAME, __version__  # noqa: F401  (module-level compat)
from mokuro_bridge.api import app
from mokuro_bridge.config import (
    OUTPUT_DIR,
    WORK_DIR,
)
from mokuro_bridge.ocr import _MOKURO_REPO, _fork_supported, _mokuro_pkg
from mokuro_bridge.providers import (
    _UPLOAD_METHODS,
    _default_upload_method,
    _run_setup_drive,
    _run_setup_mega,
    _run_setup_onedrive,
)


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog=APP_NAME, description=__doc__.splitlines()[0])
    parser.add_argument(
        "--setup-upload",
        metavar="METHOD",
        default=None,
        help="Interactively configure/authenticate an upload method "
        "(available: mega, drive, onedrive). e.g. --setup-upload drive",
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

    setup_method = args.setup_upload or ("mega" if args.setup_mega else None)
    if setup_method:
        if setup_method == "mega":
            _run_setup_mega()
        elif setup_method == "drive":
            _run_setup_drive()
        elif setup_method == "onedrive":
            _run_setup_onedrive()
        elif setup_method in _UPLOAD_METHODS:
            print(f"error: upload method '{setup_method}' has no setup wizard yet")
            raise SystemExit(2)
        else:
            print(
                f"error: unknown upload method '{setup_method}' "
                f"(available: {', '.join(_UPLOAD_METHODS)})"
            )
            raise SystemExit(2)
        return

    import uvicorn

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
    print("=" * 60)

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,  # keep the console clean — mokuro-bridge logs its own progress
        # Reload wipes in-memory sessions mid-scrape. Opt in with UVICORN_RELOAD=1.
        reload=os.environ.get("UVICORN_RELOAD", "0").lower() in ("1", "true", "yes"),
        reload_excludes=["mokuro/*", "**/mokuro/**", "**/__pycache__/**"],
    )

if __name__ == "__main__":
    _main()
