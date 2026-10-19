"""WebDAV upload provider.

A WebDAV destination is a base URL plus Basic-auth credentials, so an account
here is closer to "a remote folder" than to a cloud SDK session: every call is
one plain HTTP request against ``<base>/<root>/<series>/<file>``. That is why
this module owns URL parsing and XML parsing itself instead of wrapping a
client library; ``requests`` is the only dependency.

The base URL belongs to the *account* (it is stored in the account metadata),
so two WebDAV accounts can point at two different servers. Credentials follow
the same rule as the other providers: the username is non-secret and lives in
the account metadata, the password is written to a 0600 per-account file, and
the historical single-account keychain entry keeps working for the default
account. Resolution order for the default account is environment, then the
credentials file, then the OS keychain, so a password changed in the dashboard
always wins over a stale keychain item.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .. import accounts
from ..accounts import DEFAULT_NAME
from ..config import WEBDAV_BASE_URL, WEBDAV_CREDS_FILE
from ..creds import _KEYRING_SERVICE, _keyring
from ..util import _chmod_fd_private
from .base import (
    DOWNLOAD_LIMIT_BYTES,
    LIST_LIMIT_ENTRIES,
    Provider,
    ProviderAccount,
    listing_entry,
    provider_account_lock,
    safe_provider_error,
    safe_provider_url,
)

_WEBDAV_IMPORT_HINT = (
    "WebDAV upload needs the requests library. "
    "Install it with: pip install requests"
)

# The keychain items the old single-account WebDAV provider used. Kept so an
# existing setup keeps authenticating after the account model arrived.
_WEBDAV_KEYRING_USERNAME = "webdav:username"
_WEBDAV_KEYRING_PASSWORD = "webdav:password"

_DAV_NS = "{DAV:}"
_TIMEOUT = 30
# (connect, read): a first connect to a slow host should not hang a finalize,
# but a multi-hundred-MB volume over a home uplink legitimately takes minutes.
_UPLOAD_TIMEOUT = (10, 600)

# Probing is interactive (the dashboard form waits on it), so it is much more
# impatient than an upload. The discovery tries are bounded: a wrong candidate
# answers 404 at once, and an unreachable host aborts the whole probe.
_PROBE_TIMEOUT = (3, 8)

# Where a WebDAV collection usually lives under a server address that was meant
# for the web UI. Only tried when the address the user gave does not itself
# answer as a WebDAV folder, and only for the well-known layouts.
_WEBDAV_PATH_HINTS = (
    "/remote.php/dav/files/{user}",
    "/remote.php/webdav",
    "/remote.php/dav",
    "/dav",
    "/webdav",
)


# --- credential store ------------------------------------------------------

def _creds_path(name: str = DEFAULT_NAME) -> Path:
    """Where one WebDAV account's URL + username + password live.

    The default account keeps the historical WEBDAV_CREDS_FILE so an existing
    ``WEBDAV_BASE_URL`` / keychain setup needs no migration; additional accounts
    get their own 0600 file under ACCOUNTS_DIR.
    """
    if name == DEFAULT_NAME:
        return WEBDAV_CREDS_FILE
    return accounts.secret_path("webdav", name, "creds.json")


def _read_creds_file(name: str = DEFAULT_NAME) -> dict:
    """The stored credential object for one account, or {} when absent."""
    try:
        data = json.loads(_creds_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_secret_file(path: Path, text: str) -> None:
    """Atomically write one 0600 secret file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        _chmod_fd_private(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if fd != -1:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _instance(name: str = DEFAULT_NAME) -> Optional[accounts.Instance]:
    return accounts.load_instance("webdav", name)


def _stored_field(name: str, key: str) -> str:
    """One non-secret field (url, username) from the account metadata."""
    instance = _instance(name)
    if instance is None:
        return ""
    return str(instance.extra.get(key) or "").strip()


def _env_applies(name: str) -> bool:
    """Whether the historical WEBDAV_* environment variables address this account."""
    return name == DEFAULT_NAME


def clean_base_url(value: object) -> str:
    """Validate and normalise a WebDAV base URL.

    The collection URL, without a trailing slash and without credentials, query
    or fragment: a user who pastes ``https://host/dav/`` and one who pastes
    ``https://host/dav`` must configure the same account.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError(
            "A WebDAV server address is required (for example "
            "https://cloud.example.com)."
        )
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("The WebDAV server address must start with http:// or https:// and include a host.")
    if parsed.username or parsed.password:
        raise ValueError(
            "Put the username and password in their own fields, not inside the address."
        )
    # A browser URL pasted from the address bar often carries a query
    # (?dir=…) or a fragment. Those are never part of the collection address,
    # so drop them rather than sending a beginner back to their server.
    parsed = parsed._replace(query="", fragment="")
    if "\x00" in parsed.path:
        raise ValueError("The WebDAV server address contains an invalid character.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def normalise_server_address(value: object) -> str:
    """Add the scheme a beginner leaves out, picking http for a local host.

    "cloud.example.com" becomes https and "nas:5005" becomes http, matching
    what the dashboard form does before it talks to the API.
    """
    text = str(value or "").strip()
    if not text:
        return text
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        return text
    host = text.split("/")[0].lower()
    hostname = host.split(":")[0]
    local = (
        hostname == "localhost"
        or hostname.endswith(".local")
        or "." not in hostname
        or bool(re.match(r"^(127\.|10\.|192\.168\.|169\.254\.)", hostname))
        or bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", hostname))
    )
    return f"{'http' if local else 'https'}://{text}"


def base_url(name: str = DEFAULT_NAME) -> str:
    """The account's base URL ("" when none is configured or it is unusable)."""
    candidates = [_stored_field(name, "url")]
    if _env_applies(name):
        candidates.append(os.environ.get("WEBDAV_BASE_URL", "").strip())
        candidates.append(WEBDAV_BASE_URL)
    candidates.append(str(_read_creds_file(name).get("url") or ""))
    for candidate in candidates:
        if not str(candidate).strip():
            continue
        try:
            return clean_base_url(candidate)
        except ValueError:
            continue
    return ""


def username(name: str = DEFAULT_NAME) -> str:
    """The account's username (non-secret; visible in /health and the page)."""
    stored = _stored_field(name, "username")
    if stored:
        return stored
    if _env_applies(name):
        env = os.environ.get("WEBDAV_USERNAME", "").strip()
        if env:
            return env
    from_file = str(_read_creds_file(name).get("username") or "").strip()
    if from_file:
        return from_file
    if _env_applies(name):
        keyring_value = _keyring_get(_WEBDAV_KEYRING_USERNAME)
        if keyring_value:
            return keyring_value
    return ""


def password(name: str = DEFAULT_NAME) -> str:
    """The account's password, resolved in the documented order ("" when absent)."""
    if _env_applies(name):
        env = os.environ.get("WEBDAV_PASSWORD", "").strip()
        if env:
            return env
    from_file = str(_read_creds_file(name).get("password") or "")
    if from_file:
        return from_file
    if _env_applies(name):
        keyring_value = _keyring_get(_WEBDAV_KEYRING_PASSWORD)
        if keyring_value:
            return keyring_value
    return ""


def _keyring_get(key: str) -> str:
    kr = _keyring()
    if kr is None:
        return ""
    try:
        value = kr.get_password(_KEYRING_SERVICE, key)
    except Exception:
        return ""
    return str(value or "").strip()


def _keyring_set(key: str, value: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(_KEYRING_SERVICE, key, value)
        return True
    except Exception:
        return False


def _keyring_delete(key: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(_KEYRING_SERVICE, key)
        return True
    except Exception:
        return False


def forget_default_keychain() -> list[str]:
    """Remove the historical WebDAV keychain items (used by forget_account)."""
    removed = []
    for key in (_WEBDAV_KEYRING_USERNAME, _WEBDAV_KEYRING_PASSWORD):
        if _keyring_delete(key):
            removed.append(f"keychain:mokuro-bridge/{key}")
    return removed


def creds_source(name: str = DEFAULT_NAME) -> Optional[str]:
    """A short word for /health describing where this account's password lives."""
    if _env_applies(name) and os.environ.get("WEBDAV_PASSWORD", "").strip():
        return "env"
    if str(_read_creds_file(name).get("password") or ""):
        return "file"
    if _env_applies(name) and _keyring_get(_WEBDAV_KEYRING_PASSWORD):
        return "keychain"
    return None


def configured(name: str = DEFAULT_NAME) -> bool:
    """Whether this account is usable right now (requests, URL, credentials)."""
    return (
        importlib.util.find_spec("requests") is not None
        and bool(base_url(name))
        and bool(username(name))
        and bool(password(name))
    )


def account_identity(instance: accounts.Instance) -> str:
    """The non-secret identity shown for one account: its username."""
    return str(instance.extra.get("username") or "").strip() or username(instance.name)


def store_webdav_account(
    name: str,
    url: str,
    username_value: str,
    password_value: str,
    *,
    alias: Optional[str] = None,
    label: Optional[str] = None,
    root: Optional[str] = None,
    insecure: bool = False,
) -> accounts.Instance:
    """Persist one WebDAV account: secret file first, then metadata.

    The credentials file is written before the metadata so a crash between the
    two leaves an addressable account whose ``configured`` check is simply
    false until it is completed, never metadata that points at a missing secret.
    """
    base = clean_base_url(url)
    clean_username = str(username_value or "").strip()
    clean_password = str(password_value or "")
    if not clean_username:
        raise ValueError("a WebDAV username is required")
    if not clean_password:
        raise ValueError("a WebDAV password is required")
    payload = json.dumps(
        {"url": base, "username": clean_username, "password": clean_password},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    _write_secret_file(_creds_path(name), payload)
    if _env_applies(name):
        # Mirror into the OS keychain so an existing keychain-only setup keeps
        # working; the file stays authoritative, so a failed store is harmless.
        _keyring_set(_WEBDAV_KEYRING_USERNAME, clean_username)
        _keyring_set(_WEBDAV_KEYRING_PASSWORD, clean_password)
    extra: dict = {"url": base, "username": clean_username}
    if insecure:
        # Non-secret, and deliberately visible in /config so the page can warn
        # that this account does not verify its server's certificate.
        extra["insecure"] = True
    return accounts.save_instance(
        "webdav",
        name,
        label=(label or None),
        root=(root or None),
        alias=alias,
        extra=extra,
    )


# --- HTTP plumbing ---------------------------------------------------------

def _session(username_value: str, password_value: str, insecure: bool = False):
    """A requests session with preemptive Basic auth and a bridge user agent.

    ``insecure`` turns certificate verification off for one account. It exists
    because the most common WebDAV target is a NAS or a Nextcloud box on the
    user's own network with a self-signed certificate, which a browser accepts
    after one click but a plain HTTP client refuses outright.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency is optional
        raise RuntimeError(_WEBDAV_IMPORT_HINT) from exc
    session = requests.Session()
    session.auth = (username_value, password_value)
    session.headers.update({"User-Agent": "mokuro-bridge/webdav"})
    if insecure:
        session.verify = False
        try:  # keep urllib3 from printing a warning per request
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # pragma: no cover - urllib3 ships with requests
            pass
    return session


def account_insecure(name: str = DEFAULT_NAME) -> bool:
    """Whether this account was told to accept an untrusted TLS certificate."""
    instance = _instance(name)
    return bool(instance is not None and instance.extra.get("insecure"))


def _credentials(name: str) -> tuple[str, str, str, bool]:
    """(base, username, password, insecure) for one account, or a setup error."""
    base = base_url(name)
    if not base:
        hint = "mokuro-bridge --setup-upload webdav"
        if name != DEFAULT_NAME:
            hint += f" --name {name}"
        raise RuntimeError(f"WebDAV account '{name}' has no base URL; run `{hint}`")
    user = username(name)
    secret = password(name)
    if not user or not secret:
        hint = "mokuro-bridge --setup-upload webdav"
        if name != DEFAULT_NAME:
            hint += f" --name {name}"
        raise RuntimeError(f"WebDAV account '{name}' has no usable credentials; run `{hint}`")
    return base, user, secret, account_insecure(name)


def _quote_segment(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def _join_url(base: str, *parts: str) -> str:
    """Join a base URL and "/"-separated relative paths, quoting each segment."""
    segments: list[str] = []
    for part in parts:
        segments.extend(segment for segment in str(part or "").strip("/").split("/") if segment)
    suffix = "/".join(_quote_segment(segment) for segment in segments)
    return f"{base}/{suffix}" if suffix else base


def _collection_url(base: str, remote_dir: str) -> str:
    """The URL of one remote folder, always with a trailing slash."""
    return _join_url(base, str(remote_dir or "").strip("/")) + "/"


def _error_text(action: str, response) -> str:
    detail = str(getattr(response, "text", "") or "").strip()[:200]
    text = f"WebDAV {action} returned HTTP {getattr(response, 'status_code', '?')}"
    return f"{text}: {detail}" if detail else text


def _auth_error(action: str, response) -> RuntimeError:
    return RuntimeError(
        f"{_error_text(action, response)} — check the WebDAV username and password"
    )


def _check_response(action: str, response, expected: tuple[int, ...]) -> None:
    """Raise a readable error for a response outside ``expected``."""
    if response.status_code in expected:
        return
    if response.status_code in (401, 403):
        raise _auth_error(action, response)
    raise RuntimeError(safe_provider_error(_error_text(action, response)))


def discovery_candidates(raw_url: str, username_value: str) -> list[str]:
    """Addresses to try when the user pasted a server address, not a DAV path.

    The first entry is always exactly what was typed. The rest are the
    well-known layouts (Nextcloud/ownCloud ``remote.php/dav/files/<user>``,
    then ``/dav`` and ``/webdav`` under the pasted address). Ordered so a
    Nextcloud server URL resolves to the per-user collection, while a plain
    server address that already is the collection (the usual NAS case) wins
    on the first try.
    """
    base = clean_base_url(raw_url)
    parsed = urllib.parse.urlsplit(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = [base]

    def add(value: str) -> None:
        candidate = str(value or "").rstrip("/")
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    if path:
        # A pasted web-UI path ("/apps/files", "/login") usually means the
        # server root, so try that before adding more to the path.
        add(origin)
    user = urllib.parse.quote(str(username_value or "").strip(), safe="")
    if user and "/remote.php/dav" not in path:
        add(f"{origin}/remote.php/dav/files/{user}")
    if "/remote.php/dav" not in path:
        add(f"{origin}/remote.php/webdav")
    if path:
        add(f"{base}/dav")
        add(f"{base}/webdav")
    add(f"{origin}/dav")
    add(f"{origin}/webdav")
    return candidates[:8]


def _probe_once(session, url: str) -> tuple[Optional[int], str, dict, Optional[str]]:
    """One Depth: 0 PROPFIND. Returns (status, body, headers, transport_error)."""
    try:
        response = session.request(
            "PROPFIND",
            str(url).rstrip("/") + "/",
            headers={"Depth": "0"},
            timeout=_PROBE_TIMEOUT,
            allow_redirects=True,
        )
    except Exception as error:  # requests raises a family of transport errors
        return None, "", {}, str(error)
    headers = {str(k).lower(): str(v) for k, v in dict(getattr(response, "headers", {}) or {}).items()}
    return response.status_code, str(getattr(response, "text", "") or ""), headers, None


def _is_dav_answer(status: Optional[int], text: str) -> bool:
    """Whether a probe response proves a WebDAV collection.

    A real PROPFIND answers 207 with a multistatus body. A few servers answer
    200 instead; a 200 that is an HTML login page must not count, or the form
    would accept the address of a web UI as a working destination.
    """
    if status == 207:
        return True
    return status == 200 and "multistatus" in str(text or "").lower()


def _looks_like_html(text: str, headers: dict) -> bool:
    kind = str(headers.get("content-type", "")).lower()
    return "text/html" in kind or str(text or "").lstrip().lower().startswith("<!doctype html")


def _certificate_error(error: str) -> bool:
    lowered = str(error or "").lower()
    return "certificate" in lowered or "ssl" in lowered and "verify" in lowered


def _friendly_connection_error(error: str, url: str, insecure: bool) -> str:
    host = urllib.parse.urlsplit(url).netloc or url
    if _certificate_error(error) and not insecure:
        return (
            "The server's certificate could not be verified. If this is your own "
            "NAS or home server with a self-signed certificate, tick "
            "\"Accept a self-signed certificate\" and try again."
        )
    if _certificate_error(error) and insecure:
        return f"The server rejected its own certificate even without verification: {error}"
    return (
        f"Could not reach {host}. Check the address, the port and that the server "
        f"is running. ({error})"
    )


_AUTH_HELP = (
    "The server rejected that username and password. Many WebDAV servers need an "
    "app password instead of your login password: in Nextcloud open Settings, then "
    "Security, then App passwords; on a Synology NAS create one in the user's "
    "settings. Some servers also expect the username with their domain prefix."
)


def _friendly_not_dav_error(status: Optional[int], text: str, headers: dict, url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc or url
    if status is None:
        return "That address did not answer. Check the address and that the server is running."
    if status in (301, 302, 303, 307, 308) or _looks_like_html(text, headers):
        return (
            f"{host} answered with a web page, not a WebDAV folder. Use the WebDAV "
            "address from your server's settings, not the page you browse: for "
            f"Nextcloud it is https://{host}/remote.php/dav/files/<username>/, and "
            "most other servers use a path ending in /dav/ or /webdav/."
        )
    if status == 404:
        return (
            f"No WebDAV folder was found at that address on {host} (HTTP 404). "
            "Copy the WebDAV address from your server's settings; for Nextcloud it "
            f"is https://{host}/remote.php/dav/files/<username>/."
        )
    if status == 405:
        return (
            f"{host} is a normal web server, not a WebDAV folder (HTTP 405). Look "
            "for WebDAV in your server's settings and copy that address."
        )
    return f"{host} answered HTTP {status}, which is not a WebDAV collection."


def verify_webdav_creds(
    url: str,
    username_value: str,
    password_value: str,
    insecure: bool = False,
    discover: bool = False,
) -> tuple[bool, Optional[str], str]:
    """Probe a server with the given credentials. Returns (ok, error, resolved_url).

    A Depth: 0 PROPFIND is the cheapest request that proves both that the
    address is a WebDAV collection and that the credentials work. With
    ``discover`` the probe also tries the well-known WebDAV paths under the
    pasted address, so "https://cloud.example.com" typed into the form resolves
    to the Nextcloud collection the user meant. Error text is written for
    someone who has never configured WebDAV before.
    """
    try:
        clean = clean_base_url(url)
        candidates = discovery_candidates(clean, username_value) if discover else [clean]
        session = _session(
            str(username_value or ""), str(password_value or ""), bool(insecure)
        )
    except ValueError as error:
        return False, str(error), ""
    except RuntimeError as error:  # requests missing
        return False, str(error), ""

    first_problem: tuple[Optional[int], str, dict] = (None, "", {})
    for index, candidate in enumerate(candidates):
        status, text, headers, transport_error = _probe_once(session, candidate)
        if _is_dav_answer(status, text):
            return True, None, candidate
        if transport_error is not None:
            # The host itself is unreachable or its certificate is refused;
            # other paths on the same host cannot do better.
            if index == 0 or _certificate_error(transport_error):
                return False, _friendly_connection_error(transport_error, candidate, bool(insecure)), clean
            continue
        if index == 0:
            if status in (401, 403):
                # The address is a WebDAV endpoint but the credentials are not
                # accepted; trying other paths with the same credentials would
                # only produce the same answer.
                return False, _AUTH_HELP, clean
            first_problem = (status, text, headers)
    status, text, headers = first_problem
    return False, safe_provider_error(_friendly_not_dav_error(status, text, headers, clean)), clean


def _ensure_remote_dir(session, base: str, remote_dir: str) -> str:
    """Make sure every segment of remote_dir exists; return the folder URL."""
    current = base
    for segment in [part for part in str(remote_dir or "").strip("/").split("/") if part]:
        current = _join_url(current, segment)
        probe = session.request("PROPFIND", current + "/", headers={"Depth": "0"}, timeout=_TIMEOUT)
        if probe.status_code in (200, 207):
            continue
        if probe.status_code in (401, 403):
            raise _auth_error("PROPFIND", probe)
        if probe.status_code != 404:
            raise RuntimeError(safe_provider_error(_error_text("PROPFIND", probe)))
        made = session.request("MKCOL", current, timeout=_TIMEOUT)
        if made.status_code in (200, 201, 204, 301):
            continue
        if made.status_code == 405:
            continue  # created by another writer between the probe and MKCOL
        raise RuntimeError(safe_provider_error(_error_text("MKCOL", made)))
    return current


def _probe_file(session, url: str) -> bool:
    """Whether one file exists; a missing collection never counts as a file."""
    response = session.request("PROPFIND", url, headers={"Depth": "0"}, timeout=_TIMEOUT)
    if response.status_code in (200, 207):
        return True
    if response.status_code == 404:
        return False
    if response.status_code in (401, 403):
        raise _auth_error("PROPFIND", response)
    raise RuntimeError(safe_provider_error(_error_text("PROPFIND", response)))


def ensure_account_root(name: str = DEFAULT_NAME) -> bool:
    """Create the account's default folder chain (idempotent).

    Called once after setup so the dashboard's Change folder immediately shows
    a real folder instead of an empty listing, and so a bad root path is found
    while the user is still on the form rather than at the first finalize.
    """
    with provider_account_lock("webdav", name):
        base, user, secret, insecure = _credentials(name)
        instance = _instance(name)
        root = instance.root_path if instance is not None else ""
        if not root:
            return False
        session = _session(user, secret, insecure)
        _ensure_remote_dir(session, base, root)
        return True


class _CountingBody:
    """File body that reports byte progress while requests streams it.

    requests reads through ``read()`` and takes Content-Length from ``len``;
    progress fires at most every 0.25 s (immediately at start and finish) to
    match the throttle the other providers use.
    """

    def __init__(self, path: Path, total: int, on_progress) -> None:
        self._file = open(path, "rb")
        self.len = total
        self.done = 0
        self._on_progress = on_progress
        self._last_time = time.monotonic()
        self._last_done = 0
        self._last_emit = 0.0

    def read(self, size: int = -1) -> bytes:
        chunk = self._file.read(size if size and size > 0 else 1 << 16)
        if chunk:
            self.done += len(chunk)
            now = time.monotonic()
            elapsed = now - self._last_time
            speed = int((self.done - self._last_done) / elapsed) if elapsed > 0 else 0
            self._last_time, self._last_done = now, self.done
            if self.done >= self.len or now - self._last_emit >= 0.25:
                self._last_emit = now
                self._on_progress(self.done, self.len, speed)
        return chunk

    def close(self) -> None:
        self._file.close()


# --- provider contract -----------------------------------------------------

def _webdav_upload(
    name: str,
    root: str,
    remote_dir: str,
    local_path: Path,
    on_progress: Optional[callable],
    overwrite: str = "fail",
) -> tuple[bool, Optional[str], Optional[str]]:
    """PUT one file under ``<base>/<remote_dir>``.

    The parent chain is created with MKCOL per segment (idempotent), existing
    files honour the shared overwrite policy (fail/skip/overwrite), and the
    upload streams through a counting body so the client's progress bar moves.
    Every failure becomes a failure tuple: the finalize stream has nowhere to
    catch an exception at this point.
    """
    try:
        with provider_account_lock("webdav", name):
            base, user, secret, insecure = _credentials(name)
            session = _session(user, secret, insecure)
            total = local_path.stat().st_size
            _ensure_remote_dir(session, base, remote_dir)
            file_url = _join_url(base, str(remote_dir or "").strip("/"), local_path.name)
            if _probe_file(session, file_url):
                if overwrite == "skip":
                    return True, None, safe_provider_url(file_url)
                if overwrite not in ("overwrite", "replace"):
                    return (
                        False,
                        f"destination already exists: {local_path.name} "
                        "(send overwrite=overwrite to replace it, or overwrite=skip "
                        "to keep the existing copy)",
                        None,
                    )
            body = None
            try:
                if on_progress is None:
                    with open(local_path, "rb") as handle:
                        response = session.put(
                            file_url,
                            data=handle,
                            timeout=_UPLOAD_TIMEOUT,
                            headers={"Content-Type": "application/octet-stream"},
                        )
                else:
                    body = _CountingBody(local_path, total, on_progress)
                    response = session.put(
                        file_url,
                        data=body,
                        timeout=_UPLOAD_TIMEOUT,
                        headers={"Content-Type": "application/octet-stream"},
                    )
            finally:
                if body is not None:
                    body.close()
            if response.status_code not in (200, 201, 204):
                if response.status_code in (401, 403):
                    raise _auth_error("PUT", response)
                return False, safe_provider_error(_error_text("PUT", response)), None
            if on_progress is not None:
                on_progress(total, total, 0)
            return True, None, safe_provider_url(file_url)
    except Exception as error:
        return False, safe_provider_error(error), None


def _webdav_remote_exists(name: str, remote_dir: str, filename: str) -> bool:
    """Whether one file exists, without creating any folder on the way."""
    with provider_account_lock("webdav", name):
        base, user, secret, insecure = _credentials(name)
        session = _session(user, secret, insecure)
        return _probe_file(session, _join_url(base, str(remote_dir or "").strip("/"), filename))


def _webdav_download(name: str, remote_dir: str, filename: str) -> Optional[bytes]:
    """Read one small sidecar back (only series.json is ever downloaded)."""
    with provider_account_lock("webdav", name):
        base, user, secret, insecure = _credentials(name)
        session = _session(user, secret, insecure)
        url = _join_url(base, str(remote_dir or "").strip("/"), filename)
        response = session.get(url, timeout=_TIMEOUT, stream=True)
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise _auth_error("GET", response)
        if response.status_code != 200:
            raise RuntimeError(safe_provider_error(_error_text("GET", response)))
        declared = 0
        try:
            declared = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > DOWNLOAD_LIMIT_BYTES:
            raise RuntimeError(
                f"refusing to read {remote_dir}/{filename}: the server reports "
                f"{declared} bytes, above the {DOWNLOAD_LIMIT_BYTES}-byte sidecar limit"
            )
        chunks: list[bytes] = []
        received = 0
        try:
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                received += len(chunk)
                if received > DOWNLOAD_LIMIT_BYTES:
                    raise RuntimeError(
                        f"refusing to read {remote_dir}/{filename}: the body exceeded "
                        f"the {DOWNLOAD_LIMIT_BYTES}-byte sidecar limit"
                    )
                chunks.append(chunk)
        finally:
            response.close()
        return b"".join(chunks)


def _parse_multistatus(xml_bytes: bytes, request_url: str, remote_dir: str) -> list[dict]:
    """Turn a 207 response into the shared listing entries.

    hrefs may be absolute URLs, absolute paths or relative paths depending on
    the server, so each one is resolved against the request URL before it is
    compared with the requested collection (which is how the self entry is
    skipped) and its last segment is used as the child name.
    """
    try:
        document = ET.fromstring(xml_bytes or b"")
    except ET.ParseError as error:
        raise RuntimeError(f"WebDAV returned a directory listing that is not valid XML: {error}") from error
    base_remote = str(remote_dir or "").strip("/")
    self_path = urllib.parse.unquote(
        urllib.parse.urlsplit(request_url).path
    ).rstrip("/")
    entries: list[dict] = []
    for node in document.findall(f"{_DAV_NS}response"):
        href = (node.findtext(f"{_DAV_NS}href") or "").strip()
        if not href:
            continue
        absolute = urllib.parse.urlsplit(urllib.parse.urljoin(request_url, href)).path
        path = urllib.parse.unquote(absolute).rstrip("/")
        if not path or path == self_path:
            continue
        name = path.rsplit("/", 1)[-1]
        if not name:
            continue
        prop = node.find(f"{_DAV_NS}propstat/{_DAV_NS}prop")
        is_folder = prop is not None and prop.find(f"{_DAV_NS}resourcetype/{_DAV_NS}collection") is not None
        size: Optional[int] = None
        if prop is not None and not is_folder:
            raw_size = prop.findtext(f"{_DAV_NS}getcontentlength")
            if raw_size:
                try:
                    size = int(raw_size)
                except ValueError:
                    size = None
        entries.append(
            listing_entry(
                name,
                f"{base_remote}/{name}".strip("/") if base_remote else name,
                is_folder=is_folder,
                size=size,
            )
        )
        if len(entries) >= LIST_LIMIT_ENTRIES:
            break
    entries.sort(key=lambda entry: (entry["type"] != "folder", entry["name"].casefold()))
    return entries


def _webdav_list_folders(name: str, remote_dir: str) -> list[dict]:
    """List one collection read-only. A missing collection is an empty listing."""
    with provider_account_lock("webdav", name):
        base, user, secret, insecure = _credentials(name)
        session = _session(user, secret, insecure)
        target = _collection_url(base, str(remote_dir or "").strip("/"))
        response = session.request("PROPFIND", target, headers={"Depth": "1"}, timeout=_TIMEOUT)
        if response.status_code == 404:
            return []
        if response.status_code not in (200, 207):
            if response.status_code in (401, 403):
                raise _auth_error("PROPFIND", response)
            raise RuntimeError(safe_provider_error(_error_text("PROPFIND", response)))
        return _parse_multistatus(response.content, target, str(remote_dir or "").strip("/"))


def _webdav_account_view(instance: accounts.Instance) -> ProviderAccount:
    """Labels and health fields for one WebDAV account."""
    server = safe_provider_url(base_url(instance.name)) or "no server set"
    extra: dict = {
        "root": instance.root_path,
        "url": server,
        "username": account_identity(instance),
    }
    if instance.extra.get("insecure"):
        extra["insecure"] = True
    return ProviderAccount(
        label=instance.display_name_for("WebDAV"),
        extra=extra,
        current_folder=f"{instance.root_path} on {server}",
    )


def _run_setup_webdav(name: Optional[str] = None, root: str = "", label: str = "") -> None:
    """Interactive wizard for one WebDAV account.

    Written for someone who has never configured WebDAV: the prompt shows the
    address each common server exposes, a failed check is explained in plain
    words and retried instead of aborting, and a self-signed certificate is a
    yes/no question rather than an SSL traceback.
    """
    from ..util import _ensure_python_deps

    if not _ensure_python_deps(["requests"], ["requests>=2.31"]):
        print(
            "WebDAV setup needs the requests library. "
            "Install it with:\n  pip install requests\n"
            "then re-run: mokuro-bridge --setup-upload webdav"
        )
        return

    import getpass

    name = accounts.ask_account_name(name, "webdav")
    existing = _instance(name)
    print("WebDAV upload setup for mokuro-bridge")
    print("-" * 40)
    print(f"Account: {accounts.method_id('webdav', name)}")
    print("Find the WebDAV address in your server's settings. Common ones:")
    print("  Nextcloud   https://cloud.example.com   (the bridge finds remote.php/dav/files/<you>/)")
    print("  ownCloud    https://cloud.example.com")
    print("  Synology    https://your-nas:5006        (WebDAV Server package; plain HTTP is 5005)")
    print("  Other       https://your-server/dav/ or https://your-server/webdav/")
    print("Some servers need an app password instead of your login password.")

    current_url = base_url(name)
    current_user = username(name)
    insecure = account_insecure(name)
    value = current_url
    user = current_user
    secret = password(name)
    resolved = ""
    for attempt in range(1, 4):
        if attempt > 1:
            print()
        answer = (
            input(f"Server address [{current_url}]: ").strip()
            if current_url
            else input("Server address: ").strip()
        )
        value = normalise_server_address(answer or current_url)
        if not str(value or "").strip():
            print("No server address given.")
            continue
        answer = (
            input(f"Username [{current_user}]: ").strip()
            if current_user
            else input("Username: ").strip()
        )
        user = answer or current_user
        if not user:
            print("No username given.")
            continue
        entered = getpass.getpass("Password (empty = keep current): ")
        secret = entered or password(name)
        if not secret:
            print("No password given.")
            continue

        ok, error, resolved = verify_webdav_creds(
            value, user, secret, insecure=insecure, discover=True
        )
        if ok:
            break
        print(f"error: {error}")
        if _certificate_error(str(error or "")) and not insecure:
            answer = input("Accept this server's certificate? [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                insecure = True
                ok, error, resolved = verify_webdav_creds(
                    value, user, secret, insecure=True, discover=True
                )
                if ok:
                    break
                print(f"error: {error}")
        if attempt == 3:
            print("Nothing was stored. Run this again when you have the WebDAV address.")
            return

    try:
        entered_url = clean_base_url(value)
    except ValueError as error:
        print(f"error: {error}")
        return
    if resolved and resolved.rstrip("/") != entered_url.rstrip("/"):
        print(f"The WebDAV folder is at: {resolved}")
        answer = input("Use that address? [Y/n] ").strip().lower()
        if answer in ("n", "no"):
            print("Nothing was stored.")
            return
        value = resolved

    stored = store_webdav_account(
        name,
        value,
        user,
        secret,
        alias=accounts.alias_for_storage("webdav", name),
        label=(label or None),
        root=(root or None),
        insecure=insecure,
    )
    try:
        ensure_account_root(name)
    except Exception as error:  # the folder is created on first upload anyway
        print(f"note: could not create the upload folder yet: {error}")
    print(f"Credentials stored in {_creds_path(name)} (permissions 0600).")
    print(f"Remote folder: {stored.root_path} on {base_url(name)}")
    print(f"Account ready: {stored.id}")
    if insecure:
        print("This account does not verify its server's certificate (self-signed).")
    if existing is None or not existing.tracked:
        print(
            "Tip: the same account can be added from the dashboard at "
            "http://127.0.0.1:62642/"
        )


PROVIDER = Provider(
    id="webdav",
    label="WebDAV",
    configured=configured,
    creds_source=creds_source,
    upload=_webdav_upload,
    remote_exists=_webdav_remote_exists,
    download=_webdav_download,
    list_folders=_webdav_list_folders,
    run_setup=_run_setup_webdav,
    account=_webdav_account_view,
)
