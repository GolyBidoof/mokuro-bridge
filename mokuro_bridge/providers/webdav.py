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
    "Install it with: pip install -r requirements-webdav.txt"
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
            "a WebDAV base URL is required (for example "
            "https://cloud.example.com/remote.php/dav/files/you/)"
        )
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("the WebDAV base URL must start with http:// or https:// and include a host")
    if parsed.username or parsed.password:
        raise ValueError(
            "put the username and password in their own fields, not inside the URL"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("the WebDAV base URL must not carry a query string or fragment")
    if "\x00" in parsed.path:
        raise ValueError("the WebDAV base URL contains an invalid character")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


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
    return accounts.save_instance(
        "webdav",
        name,
        label=(label or None),
        root=(root or None),
        alias=alias,
        extra={"url": base, "username": clean_username},
    )


# --- HTTP plumbing ---------------------------------------------------------

def _session(username_value: str, password_value: str):
    """A requests session with preemptive Basic auth and a bridge user agent."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency is optional
        raise RuntimeError(_WEBDAV_IMPORT_HINT) from exc
    session = requests.Session()
    session.auth = (username_value, password_value)
    session.headers.update({"User-Agent": "mokuro-bridge/webdav"})
    return session


def _credentials(name: str) -> tuple[str, str, str]:
    """(base, username, password) for one account, or a setup error."""
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
    return base, user, secret


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


def verify_webdav_creds(url: str, username_value: str, password_value: str) -> tuple[bool, Optional[str]]:
    """Probe one server with the given credentials. Returns (ok, error).

    A Depth: 0 PROPFIND on the base URL is the cheapest request that proves
    both that the URL is a WebDAV collection and that the credentials work.
    """
    try:
        base = clean_base_url(url)
        session = _session(str(username_value or ""), str(password_value or ""))
        response = session.request(
            "PROPFIND", base + "/", headers={"Depth": "0"}, timeout=_TIMEOUT, allow_redirects=True
        )
    except ValueError as error:
        return False, str(error)
    except RuntimeError as error:
        return False, str(error)
    except Exception as error:  # requests raises a family of connection errors
        return False, safe_provider_error(f"could not reach {url}: {error}")
    if response.status_code in (200, 207):
        return True, None
    if response.status_code in (401, 403):
        return False, "the server rejected those credentials (HTTP 401/403)"
    if response.status_code == 405:
        return False, "that URL answered but does not accept WebDAV requests (HTTP 405)"
    return False, safe_provider_error(_error_text("PROPFIND", response))


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
            base, user, secret = _credentials(name)
            session = _session(user, secret)
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
        base, user, secret = _credentials(name)
        session = _session(user, secret)
        return _probe_file(session, _join_url(base, str(remote_dir or "").strip("/"), filename))


def _webdav_download(name: str, remote_dir: str, filename: str) -> Optional[bytes]:
    """Read one small sidecar back (only series.json is ever downloaded)."""
    with provider_account_lock("webdav", name):
        base, user, secret = _credentials(name)
        session = _session(user, secret)
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
        base, user, secret = _credentials(name)
        session = _session(user, secret)
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
    return ProviderAccount(
        label=instance.display_name_for("WebDAV"),
        extra={
            "root": instance.root_path,
            "url": server,
            "username": account_identity(instance),
        },
        current_folder=f"{instance.root_path} on {server}",
    )


def _run_setup_webdav(name: Optional[str] = None, root: str = "", label: str = "") -> None:
    """Interactive wizard: ask for the server URL and credentials, then verify."""
    from ..util import _ensure_python_deps

    if not _ensure_python_deps(["requests"], "requirements-webdav.txt"):
        print(
            "WebDAV setup needs the requests library. "
            "Install it with:\n  pip install -r requirements-webdav.txt\n"
            "then re-run: mokuro-bridge --setup-upload webdav"
        )
        return

    name = accounts.ask_account_name(name, "webdav")
    existing = _instance(name)
    print("WebDAV upload setup for mokuro-bridge")
    print("-" * 40)
    print(f"Account: {accounts.method_id('webdav', name)}")
    print(
        "The base URL is the WebDAV collection the bridge uploads into, for "
        "example https://cloud.example.com/remote.php/dav/files/you/"
    )

    current_url = base_url(name)
    answer = input(f"Base URL [{current_url}]: ").strip() if current_url else input("Base URL: ").strip()
    value = answer or current_url
    try:
        clean_base_url(value)
    except ValueError as error:
        print(f"error: {error}")
        return

    current_user = username(name)
    answer = input(f"Username [{current_user}]: ").strip() if current_user else input("Username: ").strip()
    user = answer or current_user
    if not user:
        print("No username given — aborting.")
        return

    import getpass

    entered = getpass.getpass("Password (empty = keep current): ")
    secret = entered or password(name)
    if not secret:
        print("No password given — aborting.")
        return

    ok, error = verify_webdav_creds(value, user, secret)
    if not ok:
        print(f"warning: the server did not confirm those credentials: {error}")
        answer = input("Store them anyway? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing was stored.")
            return
    stored = store_webdav_account(
        name,
        value,
        user,
        secret,
        alias=accounts.alias_for_storage("webdav", name),
        label=(label or None),
        root=(root or None),
    )
    print(f"Credentials stored in {_creds_path(name)} (permissions 0600).")
    print(f"Remote root: {stored.root_path} (created under the base URL on first upload)")
    print(f"Account ready: {stored.id}")
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
