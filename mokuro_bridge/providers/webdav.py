from __future__ import annotations
import importlib.util
import json
import os
import time
from typing import Optional

from ..config import WEBDAV_BASE_URL, WEBDAV_CREDS_FILE
from ..creds import _KEYRING_SERVICE, _keyring

_WEBDAV_IMPORT_HINT = (
    "WebDAV upload needs the requests library. "
    "Install it with: pip install -r requirements-webdav.txt"
)

_WEBDAV_KEYRING_USERNAME = "webdav:username"
_WEBDAV_KEYRING_PASSWORD = "webdav:password"

def _webdav_username() -> Optional[str]:
    """WebDAV username: env WEBDAV_USERNAME → keyring 'webdav:username'."""
    env = os.environ.get("WEBDAV_USERNAME", "").strip()
    if env:
        return env
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_USERNAME)
            if value:
                return value
        except Exception:
            pass
    creds = _read_webdav_creds_file()
    if creds:
        return creds.get("username") or None
    return None

def _webdav_password() -> Optional[str]:
    """WebDAV password: env WEBDAV_PASSWORD → keyring 'webdav:password'."""
    env = os.environ.get("WEBDAV_PASSWORD", "").strip()
    if env:
        return env
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_PASSWORD)
            if value:
                return value
        except Exception:
            pass
    creds = _read_webdav_creds_file()
    if creds:
        return creds.get("password") or None
    return None

def _webdav_configured() -> bool:
    """Whether the WebDAV method is usable right now."""
    return (
        bool(WEBDAV_BASE_URL)
        and bool(_webdav_username())
        and bool(_webdav_password())
        and importlib.util.find_spec("requests") is not None
    )

def _webdav_base_url() -> str:
    """The configured DAV base URL with any trailing slash stripped."""
    return WEBDAV_BASE_URL.rstrip("/")

def _read_webdav_creds_file() -> Optional[dict]:
    """Read WEBDAV_CREDS_FILE (JSON: base_url/username/password) or None."""
    try:
        data = json.loads(WEBDAV_CREDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data

def _write_webdav_creds_file(url: str, username: str, password: str) -> None:
    """Persist WebDAV URL + credentials as JSON (0600)."""
    WEBDAV_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEBDAV_CREDS_FILE.write_text(
        json.dumps(
            {"base_url": url, "username": username, "password": password},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        WEBDAV_CREDS_FILE.chmod(0o600)
    except OSError:
        pass

def _run_setup_webdav() -> None:
    """Interactive first-run wizard: capture base URL + credentials and store.

    Resolution order at runtime: env vars (WEBDAV_USERNAME / WEBDAV_PASSWORD)
    → OS keychain (keyring) → WEBDAV_CREDS_FILE. The base URL is read from
    WEBDAV_BASE_URL (required) and persisted into the creds file so the setup
    survives restarts; when keyring is available the username/password are
    ALSO stored there and the file only holds the URL.
    """
    from ..util import _ensure_python_deps

    if not _ensure_python_deps(["requests"], "requirements-webdav.txt"):
        print(
            "WebDAV setup needs the requests library. "
            "Install it with:\n  pip install -r requirements-webdav.txt\n"
            "then re-run: python server.py --setup-upload webdav"
        )
        return

    try:
        import getpass
    except ImportError:
        getpass = None

    print("WebDAV upload setup for mokuro-bridge")
    print("-" * 40)
    if not WEBDAV_BASE_URL:
        print(
            "No WebDAV base URL configured. Set the WEBDAV_BASE_URL "
            "environment variable (e.g. "
            "https://host/remote.php/dav/files/<username>) and re-run."
        )
        return

    print(f"Base URL: {WEBDAV_BASE_URL}")
    print(
        "Leave a prompt empty to keep the currently configured value "
        "(env / keyring / file)."
    )

    username = os.environ.get("WEBDAV_USERNAME", "").strip() or _webdav_username() or ""
    answer = input(f"Username [{username}]: ").strip()
    if answer:
        username = answer
    if not username:
        print("No username given — aborting.")
        return

    password = os.environ.get("WEBDAV_PASSWORD", "").strip() or _webdav_password() or ""
    if getpass is not None:
        entered = getpass.getpass("Password (empty = keep current): ")
    else:
        entered = input("Password (empty = keep current): ")
    if entered:
        password = entered
    if not password:
        print("No password given — aborting.")
        return

    stored = []
    _write_webdav_creds_file(WEBDAV_BASE_URL, username, password)
    stored.append(str(WEBDAV_CREDS_FILE))
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_USERNAME, username)
            kr.set_password(_KEYRING_SERVICE, _WEBDAV_KEYRING_PASSWORD, password)
            stored.append("system keyring")
        except Exception as exc:  # pragma: no cover - backend-specific
            print(f"note: could not store in the system keyring: {exc}")
    print(
        "Stored WebDAV credentials in: "
        + ", ".join(stored)
        + " (creds file is 0600)."
    )
    print(
        "Tip: you can also use environment variables WEBDAV_USERNAME / "
        "WEBDAV_PASSWORD instead of storing anything."
    )

class _CountingBody:
    """File-like body that counts bytes as requests streams it, throttled.

    `requests` reads via `.read()` and sets Content-Length from `.len`.
    on_progress(bytes_done, total_bytes, speed_bps) fires at most every
    0.25s (or immediately on start/finish), mirroring the caller-side
    throttle in _upload_batch.
    """

    def __init__(self, path, total, on_progress):
        self._f = open(path, "rb")
        self.len = total
        self.done = 0
        self._on_progress = on_progress
        self._last_t = time.monotonic()
        self._last_done = 0
        self._last_emit = 0.0

    def read(self, n=-1):
        chunk = self._f.read(n if n and n > 0 else 1 << 16)
        if chunk:
            self.done += len(chunk)
            now = time.monotonic()
            dt = now - self._last_t
            speed = int((self.done - self._last_done) / dt) if dt > 0 else 0
            self._last_t, self._last_done = now, self.done
            if (
                self.done >= self.len
                or self.done <= 0
                or now - self._last_emit >= 0.25
            ):
                self._last_emit = now
                self._on_progress(self.done, self.len, speed)
        return chunk

    def close(self):
        self._f.close()

def _webdav_mkcol(sess, url: str) -> bool:
    """Best-effort MKCOL; returns True when the collection exists/ok.

    Accepts 201 (created), 301 (moved), 405 (exists, method not allowed on
    an existing collection), 409 (already exists / conflict) as
    "exists/ok"; anything else returns False.
    """
    try:
        resp = sess.request("MKCOL", url, timeout=30)
    except Exception:
        return False
    return resp.status_code in (201, 204, 301, 405, 409)

def _webdav_upload_file(
    base_url: str,
    username: str,
    password: str,
    local_path: Path,
    remote_dir: str,
    on_progress: Optional[callable],
) -> tuple[bool, Optional[str]]:
    """PUT one file over WebDAV under <base_url>/<remote_dir>/<name>.

    Ensures the parent collections exist (idempotent MKCOL per segment),
    then streams the PUT through a counting body so on_progress fires with
    live byte/speed deltas. Returns (success, error_message).
    """
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(_WEBDAV_IMPORT_HINT) from exc
    base = base_url.rstrip("/")
    remote_dir = str(remote_dir or "").strip("/")
    if not base:
        raise RuntimeError(
            "WebDAV base URL not configured. Set WEBDAV_BASE_URL or run "
            "`python server.py --setup-upload webdav`."
        )
    total = local_path.stat().st_size
    remote_url = f"{base}/{remote_dir}/{local_path.name}"

    sess = requests.Session()
    sess.auth = (username, password)  # preemptive Basic auth
    sess.headers["Content-Type"] = "application/octet-stream"

    # Ensure parent collections exist (root, then each series segment).
    accumulated = base
    for seg in remote_dir.split("/"):
        if not seg:
            continue
        accumulated = f"{accumulated}/{seg}"
        if not _webdav_mkcol(sess, accumulated):
            return False, f"WebDAV MKCOL failed for {accumulated}"

    body = None
    try:
        if on_progress is None:
            with open(local_path, "rb") as f:
                resp = sess.put(remote_url, data=f, timeout=None)
        else:
            body = _CountingBody(local_path, total, on_progress)
            resp = sess.put(remote_url, data=body, timeout=None)
        if resp.status_code in (200, 201, 204):
            if on_progress:
                on_progress(total, total, 0)
            return True, None
        text = (resp.text or "").strip()
        return False, f"WebDAV PUT {resp.status_code}: {text[:300] or 'no detail'}"
    except Exception as exc:
        return False, str(exc)
    finally:
        if body is not None:
            body.close()
