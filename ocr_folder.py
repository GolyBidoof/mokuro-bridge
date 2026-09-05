#!/usr/bin/env python3
"""
mokuro-bridge — OCR a folder of manga pages with zero ceremony.

If you don't care about sessions, polling or the HTTP API, this is the
fastest way to use the bridge:

    # 1. make sure the bridge is running (separate terminal):
    #    ./run.sh        (Windows: python server.py)

    # 2. point it at a folder of page images:
    python3 ocr_folder.py "/path/to/my/manga pages" --title "My Manga 1巻"

It drives the running bridge over HTTP (stdlib only): start a resume session
for the folder, let the bridge OCR every page, then finalize. Results land in
the bridge's output directory by default:

    <MOKURO_BRIDGE_OUTPUT_DIR>/<series>/
        <volume>.cbz  <volume>.mokuro  <volume>.webp

Point reader.mokuro.app at that folder to read it. Run with --help for the
full option list.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = os.environ.get("MOKURO_BRIDGE_URL", "http://127.0.0.1:62642")


def post_form(url: str, fields: dict):
    """POST urlencoded form fields; returns parsed JSON."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def finalize(url: str, session_id: str, upload_method: str, delete: bool) -> dict:
    """POST finalize and stream the NDJSON progress to stdout."""
    body = urllib.parse.urlencode(
        {
            "upload_method": upload_method,
            # Legacy alias kept for older bridges that don't know upload_method yet.
            "upload_to_mega": "true" if upload_method == "mega" else "false",
            "delete_after_upload": "true" if delete else "false",
        }
    ).encode()
    req = urllib.request.Request(f"{url}/session/{session_id}/finalize", data=body)
    final = {}
    with urllib.request.urlopen(req, timeout=3600) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line:
                continue
            msg = json.loads(line)
            stage = msg.get("stage", "?")
            if stage == "wait_ocr":
                continue  # noisy
            text = msg.get("message", "")
            print(f"[{stage}] {text}" if text else f"[{stage}]")
            if stage == "error":
                sys.exit(f"Bridge error: {msg.get('message')}")
            if stage == "done":
                final = msg
    return final


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ocr_folder.py",
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source_dir", help="folder containing the page images")
    parser.add_argument("--title", default=None, help="volume title (default: folder name)")
    parser.add_argument(
        "--upload-method",
        default=None,
        help="upload destination: local (default), mega, or drive",
    )
    parser.add_argument(
        "--upload-mega",
        action="store_true",
        help="[legacy alias] upload to MEGA when done (same as --upload-method mega)",
    )
    parser.add_argument("--keep-files", action="store_true", help="keep bridge working files")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"bridge base URL (default: {DEFAULT_URL})")
    args = parser.parse_args()

    method = args.upload_method or ("mega" if args.upload_mega else "local")
    if method not in ("local", "mega", "drive"):
        sys.exit(f"unknown upload method: {method} (expected local, mega, or drive)")

    source = os.path.abspath(os.path.expanduser(args.source_dir))
    if not os.path.isdir(source):
        sys.exit(f"Not a directory: {source}")
    title = args.title or os.path.basename(source.rstrip("/\\")) or "manga"

    # Friendly check that the bridge is up before doing anything.
    try:
        with urllib.request.urlopen(f"{args.url}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(
            f"Cannot reach mokuro-bridge at {args.url} ({exc}).\n"
            "Start it first:  ./run.sh        (Windows: python server.py)"
        )
    print(f"bridge: {health.get('status')} — mokuro installed: {health.get('mokuro_installed')}")

    # resume syncs the folder into a session and queues OCR for every page.
    snap = post_form(
        f"{args.url}/session/resume", {"title": title, "source_dir": source}
    )
    session_id = snap["session_id"]
    print(
        f"session {session_id}: {snap.get('queued_for_ocr', 0)} pages queued, "
        f"{snap.get('ocr_cached', 0)} already OCR'd"
    )

    result = finalize(
        args.url, session_id, upload_method=method, delete=not args.keep_files
    )
    where = result.get("mega_path") if method == "mega" else result.get("output_dir")
    print(f"\nDone — {result.get('pages')} page(s) OCR'd.")
    print(f"Files are in: {where}")
    if method == "mega":
        print("Open /mokuro-reader in reader.mokuro.app to read.")
    else:
        print("Point reader.mokuro.app at that folder to read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
