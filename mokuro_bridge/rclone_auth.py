"""Configure cloud accounts through rclone, driven from the browser.

The dashboard should not send anyone to a terminal. These helpers create the
rclone remotes the destinations need:

* MEGA and WebDAV are plain form data: create the remote, check it, done.
* Drive and OneDrive are OAuth. `rclone authorize` runs in a background
  process, prints a loopback consent URL, and then prints the token on stdout.
  The page shows the URL, polls for the token, and finishes by creating the
  remote. rclone refreshes those tokens itself from then on.

Secrets live in rclone's own configuration, never in the bridge's account
files. The bridge only records which remote backs an account.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Optional

from . import accounts
from .config import RCLONE_BINARY, RCLONE_ROOT_NAME

_SERVICES = {
    "mega": {"label": "MEGA", "blurb": "Email, password and an optional two-factor code."},
    "drive": {"label": "Google Drive", "blurb": "Sign in to Google in one consent tab."},
    "onedrive": {"label": "OneDrive", "blurb": "Sign in with a Microsoft account, no Azure setup."},
    "webdav": {"label": "WebDAV", "blurb": "Server address plus its own username and password."},
}

_AUTH_URL_RE = re.compile(r"(https?://(?:127\.0\.0\.1|localhost):\d+/\S+)")
_TOKEN_RE = re.compile(r"(\{.*\})", re.S)
AUTHORIZE_TIMEOUT_S = 300.0


class RcloneAuthError(RuntimeError):
    """A user-facing setup failure."""


def binary() -> Optional[str]:
    name = os.environ.get("RCLONE_BINARY", "").strip() or RCLONE_BINARY
    return shutil.which(name)


def _require_binary() -> str:
    found = binary()
    if found is None:
        raise RcloneAuthError(
            "rclone is not installed. Install it with `brew install rclone` "
            "(macOS), `winget install Rclone.Rclone` (Windows) or "
            "`apt install rclone` (Debian/Ubuntu), then reload the page."
        )
    return found


def _detail(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[-400:]


def _run(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_require_binary(), *args], capture_output=True, text=True, timeout=timeout
    )


def remote_name(alias: str) -> str:
    """A bridge-owned remote name, namespaced so user remotes are untouched."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(alias or ""))
    return f"mb-{safe or secrets.token_hex(4)}"


def obscure(password: str) -> str:
    result = _run(["obscure", password], timeout=20.0)
    value = (result.stdout or "").strip()
    if result.returncode != 0 or not value:
        raise RcloneAuthError("rclone could not obscure the password: " + _detail(result))
    return value


def _create(remote: str, kind: str, params: dict[str, str]) -> None:
    args = ["config", "create", remote, kind]
    for key, value in params.items():
        args += [key, value]
    result = _run(args, timeout=60.0)
    if result.returncode != 0:
        raise RcloneAuthError(f"rclone could not save the {kind} remote: " + _detail(result))


def check_remote(remote: str) -> tuple[bool, Optional[str]]:
    from .providers import rclone as rclone_provider

    return rclone_provider.check_remote(
        remote if remote.endswith(":") else f"{remote}:", refresh=True
    )


def create_mega(
    remote_alias: str,
    email: str,
    password: str,
    totp: str = "",
    *,
    root: str = "",
    check: bool = True,
) -> tuple[str, dict]:
    """Create a MEGA remote. Returns (remote, account extra)."""
    email = str(email or "").strip()
    if not email or not password:
        raise RcloneAuthError("MEGA needs both the email and the password")
    remote = remote_name(remote_alias)
    params = {"user": email, "pass": obscure(password)}
    code = str(totp or "").strip()
    if code:
        params["2fa"] = code
    _create(remote, "mega", params)
    _refresh_provider_caches()
    if check:
        ok, error = check_remote(remote)
        if not ok:
            # A remote that cannot sign in is worse than none: remove it.
            _remove(remote)
            raise RcloneAuthError(error or "MEGA sign-in failed")
    return remote, {"email": email}


def create_webdav(
    remote_alias: str,
    url: str,
    username: str,
    password: str,
    *,
    root: str = "",
    check: bool = True,
) -> tuple[str, dict]:
    """Create a WebDAV remote. Returns (remote, account extra)."""
    url = str(url or "").strip()
    if not url:
        raise RcloneAuthError("WebDAV needs the server address")
    remote = remote_name(remote_alias)
    params = {
        "url": url,
        "vendor": "other",
        "user": str(username or ""),
        "pass": obscure(str(password or "")),
    }
    _create(remote, "webdav", params)
    _refresh_provider_caches()
    if check:
        ok, error = check_remote(remote)
        if not ok:
            _remove(remote)
            raise RcloneAuthError(error or "the WebDAV server refused those credentials")
    return remote, {"url": url, "username": str(username or "")}


def _refresh_provider_caches() -> None:
    from .providers import rclone as rclone_provider

    rclone_provider.refresh_caches()


def _remove(remote: str) -> None:
    try:
        _run(["config", "delete", remote], timeout=20.0)
    except Exception:
        pass


_FLOWS: dict[str, dict] = {}
_FLOWS_LOCK = threading.Lock()


def start_authorize(kind: str) -> dict:
    """Start `rclone authorize <kind>` and return {flow_id, url, state}."""
    if kind not in ("drive", "onedrive"):
        raise RcloneAuthError(f"{kind} does not use a consent tab")
    flow_id = secrets.token_hex(8)
    flow = {
        "id": flow_id,
        "kind": kind,
        "state": "waiting",
        "url": "",
        "token": "",
        "error": "",
        "started": time.time(),
        "process": None,
    }
    with _FLOWS_LOCK:
        _FLOWS[flow_id] = flow
    threading.Thread(target=_run_authorize, args=(flow,), name=f"rclone-authorize-{kind}", daemon=True).start()
    # Give rclone a moment to print the consent URL before the page reads it.
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and not flow["url"] and flow["state"] == "waiting":
        time.sleep(0.15)
    return {"flow_id": flow_id, "url": flow["url"], "state": flow["state"]}


def _run_authorize(flow: dict) -> None:
    try:
        process = subprocess.Popen(
            [_require_binary(), "authorize", flow["kind"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as error:
        flow["state"] = "error"
        flow["error"] = str(error)
        return
    flow["process"] = process
    deadline = time.monotonic() + AUTHORIZE_TIMEOUT_S
    output: list[str] = []
    try:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip("\n")
            output.append(line)
            del output[:-40]
            if not flow["url"]:
                found = _AUTH_URL_RE.search(line)
                if found:
                    flow["url"] = found.group(1)
            if flow["state"] == "waiting" and line.strip().startswith("{"):
                match = _TOKEN_RE.search("\n".join(output[-12:]))
                if match:
                    try:
                        token = json.loads(match.group(1))
                    except ValueError:
                        token = None
                    if isinstance(token, dict):
                        flow["token"] = json.dumps(token)
                        flow["state"] = "ready"
                        break
            if time.monotonic() > deadline:
                flow["state"] = "error"
                flow["error"] = "the sign-in window expired; start it again"
                break
        else:
            if flow["state"] == "waiting":
                flow["state"] = "error"
                flow["error"] = _tail(output) or "rclone authorize exited without a token"
    except Exception as error:
        flow["state"] = "error"
        flow["error"] = str(error)
    finally:
        if process.poll() is None:
            process.terminate()
        if flow["state"] == "waiting":
            flow["state"] = "error"
            flow["error"] = flow["error"] or "sign-in did not finish"


def _tail(lines: list[str]) -> str:
    return "\n".join(lines[-4:])[-400:]


def authorize_status(flow_id: str) -> dict:
    with _FLOWS_LOCK:
        flow = _FLOWS.get(flow_id)
        if flow is None:
            raise RcloneAuthError("that sign-in is no longer waiting; start it again")
        return {
            "state": flow["state"],
            "url": flow["url"],
            "token": flow["token"] if flow["state"] == "ready" else "",
            "error": flow["error"],
        }


def finish_authorize(flow_id: str, remote_alias: str, *, root: str = "") -> tuple[str, dict]:
    """Create the remote from a finished consent flow."""
    status = authorize_status(flow_id)
    if status["state"] != "ready" or not status["token"]:
        raise RcloneAuthError(status["error"] or "the sign-in is not finished yet")
    with _FLOWS_LOCK:
        flow = _FLOWS[flow_id]
        kind = flow["kind"]
    remote = remote_name(remote_alias)
    params = {"token": status["token"]}
    if kind == "onedrive":
        params["drive_type"] = "personal"
    _create(remote, kind, params)
    _refresh_provider_caches()
    ok, error = check_remote(remote)
    if not ok:
        _remove(remote)
        raise RcloneAuthError(error or f"{kind} sign-in could not be used")
    with _FLOWS_LOCK:
        _FLOWS.pop(flow_id, None)
    return remote, {"email": ""}


def remote_summary(remote: str, *, refresh: bool = False) -> str:
    """A one-line, non-secret description of a remote for the status list."""
    from .providers import rclone as rclone_provider

    remote = remote if remote.endswith(":") else f"{remote}:"
    try:
        dump = rclone_provider.config_dump(refresh=refresh)
    except Exception:
        return ""
    entry = dump.get(remote.rstrip(":")) or {}
    kind = str(entry.get("type") or "")
    if kind == "mega":
        user = str(entry.get("user") or "")
        return f"MEGA{(' · ' + user) if user else ''}"
    if kind == "webdav":
        url = str(entry.get("url") or "")
        return f"WebDAV{(' · ' + url) if url else ''}"
    if kind == "drive":
        return "Google Drive · signed in"
    if kind == "onedrive":
        return "OneDrive · signed in"
    return kind


def services() -> list[dict]:
    return [
        {"id": key, "label": value["label"], "blurb": value["blurb"]}
        for key, value in _SERVICES.items()
    ]


def default_root() -> str:
    return RCLONE_ROOT_NAME
