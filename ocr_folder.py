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


def post_json(url: str, fields: dict):
    """POST urlencoded form fields; returns parsed JSON."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def finalize(url: str, session_id: str, upload_method: str, delete: bool, local_dir: str = "") -> dict:
    """POST finalize and stream the NDJSON progress to stdout."""
    fields = {
        "upload_method": upload_method,
        # Legacy alias kept for older bridges that don't know upload_method yet.
        "upload_to_mega": "true" if upload_method == "mega" else "false",
        "delete_after_upload": "true" if delete else "false",
    }
    if upload_method == "local" and local_dir:
        fields["local_dir"] = local_dir
    body = urllib.parse.urlencode(fields).encode()
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


def list_methods(url: str) -> None:
    """Print the bridge's configured upload methods and current folders."""
    info = get_json(f"{url}/upload-methods")
    print("Configured upload methods:")
    for m in info.get("methods", []):
        flag = "default" if m.get("default") else ("configured" if m.get("configured") else "not configured")
        print(f"  {m.get('id'):<9} {flag:<14} folder: {m.get('current_folder') or '—'}")
    print(f"default: {info.get('upload_method_default')}  selected: {info.get('upload_method_selected')}")


# Method ids the bridge can know about, even before we can ask it.
_KNOWN_METHODS = ("local", "mega", "drive", "onedrive", "webdav")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ocr_folder.py",
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source_dir", nargs="?", help="folder containing the page images")
    parser.add_argument("--title", default=None, help="volume title (default: folder name)")
    parser.add_argument(
        "--upload-method",
        default=None,
        help=f"upload destination: one of {', '.join(_KNOWN_METHODS)} (default: local)",
    )
    parser.add_argument(
        "--upload-mega",
        action="store_true",
        help="[legacy alias] upload to MEGA when done (same as --upload-method mega)",
    )
    parser.add_argument(
        "--local-dir",
        default="",
        help="when --upload-method local: write into this folder instead of the "
        "bridge's default output dir (e.g. --local-dir ~/my-manga)",
    )
    parser.add_argument(
        "--list-methods",
        action="store_true",
        help="list the upload methods the bridge has configured, then exit",
    )
    parser.add_argument("--keep-files", action="store_true", help="keep bridge working files")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"bridge base URL (default: {DEFAULT_URL})")
    args = parser.parse_args()

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

    if args.list_methods:
        try:
            list_methods(args.url)
        except (urllib.error.URLError, OSError) as exc:
            sys.exit(f"Cannot list methods from {args.url}: {exc}")
        return 0

    if not args.source_dir:
        parser.error("source_dir is required (or pass --list-methods)")

    method = args.upload_method or ("mega" if args.upload_mega else "local")
    if method not in _KNOWN_METHODS:
        sys.exit(f"unknown upload method: {method} (expected one of: {', '.join(_KNOWN_METHODS)})")
    if args.upload_mega and args.upload_method:
        sys.exit("use either --upload-method or --upload-mega, not both")

    source = os.path.abspath(os.path.expanduser(args.source_dir))
    if not os.path.isdir(source):
        sys.exit(f"Not a directory: {source}")
    title = args.title or os.path.basename(source.rstrip("/\\")) or "manga"

    # resume syncs the folder into a session and queues OCR for every page.
    snap = post_json(
        f"{args.url}/session/resume", {"title": title, "source_dir": source}
    )
    session_id = snap["session_id"]
    print(
        f"session {session_id}: {snap.get('queued_for_ocr', 0)} pages queued, "
        f"{snap.get('ocr_cached', 0)} already OCR'd"
    )

    result = finalize(
        args.url, session_id, upload_method=method,
        delete=not args.keep_files, local_dir=args.local_dir,
    )
    print(f"\nDone — {result.get('pages')} page(s) OCR'd.")
    if method == "local":
        print(f"Files are in: {result.get('output_dir')}")
        print("Point reader.mokuro.app at that folder to read.")
    else:
        print(f"Files are in: {result.get('remote_path') or result.get('mega_path')}")
        print("Open /mokuro-reader in reader.mokuro.app to read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
