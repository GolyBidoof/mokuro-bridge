from __future__ import annotations
import importlib.util
import json
import os
import re
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from .. import accounts
from ..accounts import DEFAULT_NAME
from ..util import _chmod_fd_private
from ..config import (
    ONEDRIVE_APP_FILE,
    ONEDRIVE_CLIENT_ID,
    ONEDRIVE_TOKEN_FILE,
    _GRAPH_BASE,
    _ONEDRIVE_BUILTIN_CLIENT_ID,
    _ONEDRIVE_SCOPES,
)
from .base import (
    DOWNLOAD_LIMIT_BYTES,
    LIST_LIMIT_ENTRIES,
    Provider,
    ProviderAccount,
    listing_entry,
    provider_account_lock,
    walk_folders,
)

_ONEDRIVE_IMPORT_HINT = (
    "OneDrive upload needs the msal and requests libraries. "
    "Install them with: pip install -r requirements-onedrive.txt"
)

_CLIENT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _read_app_file() -> str:
    """The dashboard-written app ID, or "" when absent/unreadable."""
    try:
        data = json.loads(ONEDRIVE_APP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("client_id") or "").strip()


def client_id() -> str:
    """The Azure application (client) ID for the device-code sign-in.

    Resolution: ONEDRIVE_CLIENT_ID in the environment, then an
    ONEDRIVE_CLIENT_ID patched onto this module (tests and older callers), then
    the file the dashboard writes, then a project default a maintainer chose
    to ship. Reading it per call is what lets the page set it without a
    restart.
    """
    env = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip()
    if env:
        return env
    if str(ONEDRIVE_CLIENT_ID or "").strip():
        return str(ONEDRIVE_CLIENT_ID).strip()
    from_file = _read_app_file()
    if from_file:
        return from_file
    return str(_ONEDRIVE_BUILTIN_CLIENT_ID or "").strip()


def client_id_source() -> Optional[str]:
    """Where the app ID comes from, for /config: env, file, builtin or None."""
    if os.environ.get("ONEDRIVE_CLIENT_ID", "").strip() or str(ONEDRIVE_CLIENT_ID or "").strip():
        return "env"
    if _read_app_file():
        return "file"
    if str(_ONEDRIVE_BUILTIN_CLIENT_ID or "").strip():
        return "builtin"
    return None


def validate_client_id(value: object) -> str:
    """Normalise and validate a pasted Azure Application (client) ID.

    A GUID check catches the two usual mistakes: copying the Object ID or the
    Directory (tenant) ID from the portal, or pasting a truncated value. The
    error names the field to copy.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("An Azure application (client) ID is required.")
    if not _CLIENT_ID_RE.match(text):
        raise ValueError(
            "That does not look like an Azure Application (client) ID. It is a "
            "GUID such as 00000000-0000-0000-0000-000000000000, copied from the "
            "app registration's Overview page; the Object ID and Directory "
            "(tenant) ID next to it are not the one to use."
        )
    return text


def store_client_id(value: object) -> str:
    """Persist the app ID for future sessions (0600, non-secret)."""
    clean = validate_client_id(value)
    _write_secret_atomic(
        ONEDRIVE_APP_FILE, json.dumps({"client_id": clean}, indent=2) + "\n"
    )
    return clean


_AADSTS_HELP = {
    "AADSTS50059": (
        "Microsoft could not match that app ID to any app registration. Copy the "
        "Application (client) ID again from the app's Overview page (the Object "
        "ID and Directory ID are different), and choose the account types that "
        "include personal Microsoft accounts when you create the app."
    ),
    "AADSTS700016": (
        "Microsoft does not recognize that app ID for the account you signed in "
        "with. Copy the Application (client) ID from the app registration's "
        "Overview page, and set the app's supported account types to allow "
        "personal Microsoft accounts as well as work or school ones."
    ),
    "AADSTS65001": (
        "The app has not been granted the Files.ReadWrite permission yet. Open "
        "the app registration, go to API permissions, add Microsoft Graph, "
        "Delegated permissions, Files.ReadWrite, then retry."
    ),
    "AADSTS70011": (
        "Microsoft rejected the requested permission scope. Add the delegated "
        "Microsoft Graph permission Files.ReadWrite to the app registration."
    ),
    "AADSTS50076": (
        "Microsoft asked for an extra verification step for this account. Sign "
        "in once at https://microsoft.com/devicelogin with the code the bridge "
        "shows, or finish the multi-factor prompt in your browser."
    ),
}


def explain_aadsts(message: object) -> str:
    """Turn an AADSTS error code into the step it actually needs.

    Returns "" when the message names no known code, so a caller can fall back
    to the raw (already redacted) provider text.
    """
    text = str(message or "")
    for code, help_text in _AADSTS_HELP.items():
        if code in text:
            return f"{help_text} ({code})"
    return ""


def _write_secret_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        _chmod_fd_private(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if fd != -1:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _onedrive_token_path(name: str = DEFAULT_NAME) -> Path:
    """Where one OneDrive account's msal token cache lives.

    The default account keeps the historical ONEDRIVE_TOKEN_FILE (no
    migration); additional accounts get their own 0600 file under ACCOUNTS_DIR.
    """
    if name == DEFAULT_NAME:
        return ONEDRIVE_TOKEN_FILE
    return accounts.secret_path("onedrive", name, "token.json")

def _onedrive_setup_hint(name: str = DEFAULT_NAME) -> str:
    """The command that signs this account in (for error messages)."""
    if name == DEFAULT_NAME:
        return "mokuro-bridge --setup-upload onedrive"
    return f"mokuro-bridge --setup-upload onedrive --name {name}"

def _onedrive_configured(name: str = DEFAULT_NAME) -> bool:
    """Whether one OneDrive account is usable right now."""
    return (
        importlib.util.find_spec("msal") is not None
        and bool(client_id())
        and _onedrive_token_path(name).is_file()
    )

def _load_token_cache(name: str = DEFAULT_NAME):
    """Load an account's token file into a SerializableTokenCache (empty if absent)."""
    try:
        from msal import SerializableTokenCache
    except ImportError as exc:
        raise RuntimeError(_ONEDRIVE_IMPORT_HINT) from exc
    cache = SerializableTokenCache()
    try:
        data = _onedrive_token_path(name).read_text(encoding="utf-8")
    except OSError:
        return cache
    if data.strip():
        try:
            cache.deserialize(data)
        except Exception:
            pass  # corrupt cache — start over
    return cache

def _save_token_cache(cache, name: str = DEFAULT_NAME) -> None:
    """Persist an account's msal token cache to its token file (0600)."""
    if not cache.has_state_changed:
        return
    path = _onedrive_token_path(name)
    _write_secret_atomic(path, cache.serialize())

def _onedrive_msal_app(name: str = DEFAULT_NAME):
    """Build the msal PublicClientApplication with the account's cache."""
    try:
        from msal import PublicClientApplication
    except ImportError as exc:
        raise RuntimeError(_ONEDRIVE_IMPORT_HINT) from exc
    cache = _load_token_cache(name)
    app = PublicClientApplication(
        client_id=client_id(),
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    return app, cache

def _onedrive_token(name: str = DEFAULT_NAME) -> str:
    """Silently acquire a valid access token (or raise with a re-setup hint).

    Raises RuntimeError (with a `--setup-upload onedrive` hint) when no
    account/token is cached or the cached refresh token is no longer usable.
    """
    app, cache = _onedrive_msal_app(name)
    accounts_in_cache = app.get_accounts()
    result = None
    if accounts_in_cache:
        result = app.acquire_token_silent_with_error(
            _ONEDRIVE_SCOPES, account=accounts_in_cache[0]
        )
        _save_token_cache(cache, name)
    if result and "access_token" in result:
        return result["access_token"]
    if accounts_in_cache and result and "error" in result:
        raise RuntimeError(
            f"OneDrive token refresh failed ({result.get('error')}: "
            f"{result.get('error_description', '')}). Re-run `"
            f"{_onedrive_setup_hint(name)}` to sign in again."
        )
    raise RuntimeError(
        f"OneDrive account '{name}' is not authorized. Run `"
        f"{_onedrive_setup_hint(name)}` to sign in (device-code flow)."
    )

def start_device_flow(name: str = DEFAULT_NAME, app_client_id: Optional[str] = None):
    """Begin a device-code sign-in for one account.

    Returns ``(app, cache, flow)``; the caller shows ``flow['user_code']`` and
    ``flow['verification_uri']``, then calls ``finish_device_flow`` (blocking,
    usually in a thread). Reuses the account's existing token cache so a
    re-authorization updates the same file.
    """
    try:
        from msal import PublicClientApplication
    except ImportError as exc:
        raise RuntimeError(_ONEDRIVE_IMPORT_HINT) from exc
    app_client_id = str(app_client_id or "").strip() or client_id()
    if not app_client_id:
        raise ValueError(
            "OneDrive sign-in needs an Azure application (client) ID once. "
            "Open https://portal.azure.com, create an app registration, enable "
            "public client flows, add the delegated Microsoft Graph permission "
            "Files.ReadWrite, then paste the Application (client) ID here."
        )
    cache = _load_token_cache(name)
    app = PublicClientApplication(
        client_id=app_client_id,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    flow = app.initiate_device_flow(scopes=_ONEDRIVE_SCOPES)
    if "user_code" not in flow:
        raise ValueError(
            "could not start the OneDrive device-code flow: "
            f"{flow.get('error_description', flow)}"
        )
    return app, cache, flow

def finish_device_flow(
    app,
    cache,
    flow,
    name: str,
    *,
    alias: Optional[str] = None,
    label: Optional[str] = None,
    root: Optional[str] = None,
) -> accounts.Instance:
    """Wait for the device-code sign-in, then persist the token and metadata.

    Blocking: it returns when the user finishes (or the code expires) and raises
    RuntimeError with the provider's message on failure.
    """
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(
            "OneDrive sign-in failed: "
            f"{result.get('error', 'unknown error')}: "
            f"{result.get('error_description', '')}"
        )
    _save_token_cache(cache, name)
    return accounts.save_instance(
        "onedrive",
        name,
        label=(label or None),
        root=(root or None),
        alias=alias,
    )

def account_identity(instance: accounts.Instance) -> str:
    """The non-secret identity (username) recorded in one account's msal cache.

    msal stores cache entries as JSON: an "Account" record carries ``username``
    and ``home_account_id``, an "IdToken" record a ``preferred_username``. Both
    are read straight from the file, read-only, with no msal import and no
    network call. A cache without a username (or a corrupt one) yields "".
    """
    if instance.email:
        return instance.email
    try:
        data = json.loads(_onedrive_token_path(instance.name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for section in ("Account", "IdToken"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            for key in ("username", "preferred_username"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""

def _run_setup_onedrive(
    name: Optional[str] = None, root: str = "", label: str = ""
) -> None:
    """Interactive device-code wizard: sign in one account, persist its token.

    Run it again with a different `name` to add a second Microsoft account:
    the default account is the bare `onedrive` target, additional ones are
    `onedrive:<name>`.
    """
    from ..util import _ensure_python_deps

    if not _ensure_python_deps(["msal", "requests"], "requirements-onedrive.txt"):
        print(
            "OneDrive setup needs the msal + requests libraries. "
            "Install them with:\n  pip install -r requirements-onedrive.txt\n"
            "then re-run: mokuro-bridge --setup-upload onedrive"
        )
        return

    from msal import PublicClientApplication

    name = accounts.ask_account_name(name, "onedrive")
    try:
        accounts.parse_method_id(accounts.method_id("onedrive", name))
    except ValueError as exc:
        print(f"error: {exc}")
        return
    token_path = _onedrive_token_path(name)

    print("OneDrive upload setup for mokuro-bridge")
    print("-" * 40)
    print(f"Account: {accounts.method_id('onedrive', name)}")
    if token_path.is_file():
        answer = input(
            f"Account {accounts.method_id('onedrive', name)} is already signed "
            f"in ({token_path}). Sign in again? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing sign-in.")
            return
    app_client_id = client_id()
    if app_client_id:
        source = client_id_source()
        print(f"Using the Azure app ID from {source or 'the configured value'}.")
    else:
        print(
            "OneDrive needs a free Azure app registration once. In the Azure portal:\n"
            "  1. Open https://portal.azure.com and go to App registrations, then\n"
            "     New registration.\n"
            "  2. Choose the account types that include personal Microsoft\n"
            "     accounts (the bridge signs in through the 'common' authority).\n"
            "  3. Open Authentication and set Allow public client flows to Yes.\n"
            "  4. Open API permissions, add Microsoft Graph, Delegated\n"
            "     permissions, Files.ReadWrite.\n"
            "  5. Copy the Application (client) ID from the Overview page (not\n"
            "     the Object ID or the Directory ID) and paste it below.\n"
            "No redirect URI or client secret is needed for the device-code flow."
        )
    for attempt in range(1, 3):
        if not app_client_id:
            entered = input("Azure Application (client) ID: ").strip()
            try:
                app_client_id = validate_client_id(entered)
            except ValueError as error:
                print(f"error: {error}")
                continue
        break
    if not app_client_id:
        print("No app ID given; nothing was stored.")
        return

    cache = _load_token_cache(name)
    app = PublicClientApplication(
        client_id=app_client_id,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    flow = app.initiate_device_flow(scopes=_ONEDRIVE_SCOPES)
    if "user_code" not in flow:
        detail = str(flow.get("error_description", flow))
        print(f"error: could not start device flow: {explain_aadsts(detail) or detail}")
        return
    print(flow.get("message", ""))
    print("Waiting for you to sign in…")
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        detail = f"{result.get('error')}: {result.get('error_description', '')}"
        print(f"error: sign-in failed: {explain_aadsts(detail) or detail}")
        return
    if not os.environ.get("ONEDRIVE_CLIENT_ID", "").strip():
        try:
            store_client_id(app_client_id)
            print(f"App ID kept in {ONEDRIVE_APP_FILE} for the dashboard (0600).")
        except (ValueError, OSError) as error:
            print(f"note: could not keep the app ID for later: {error}")
    _save_token_cache(cache, name)
    print(f"OneDrive token stored in {token_path} (permissions 0600).")
    stored = accounts.save_instance(
        "onedrive",
        name,
        label=(label or None),
        root=(root or None),
        alias=accounts.alias_for_storage("onedrive", name),
    )
    if stored is not None:
        print(f"Remote root: {stored.root_path} (a folder in this account's drive)")
        print(f"Account ready: {stored.id}")

def _onedrive_headers(name: str = DEFAULT_NAME) -> dict:
    """Authorization headers for Graph calls."""
    return {"Authorization": f"Bearer {_onedrive_token(name)}"}

def _graph_request(sess, method: str, url: str, **kwargs):
    """Graph call wrapper that surfaces non-2xx as RuntimeError."""
    resp = sess.request(method, url, timeout=30, **kwargs)
    if resp.status_code not in (200, 201, 202, 204):
        try:
            detail = resp.json()
        except ValueError:
            detail = (resp.text or "")[:300]
        raise RuntimeError(
            f"Graph {method} {url} → {resp.status_code}: {detail}"
        )
    return resp

def _onedrive_item_url(name: str, parent_id: str) -> str:
    """Graph address of one child by name, under parent_id ("root" or an id)."""
    quoted = urllib.parse.quote(name, safe="")
    if parent_id == "root":
        return f"{_GRAPH_BASE}/me/drive/root:/{quoted}:"
    return f"{_GRAPH_BASE}/me/drive/items/{parent_id}:/{quoted}:"

def _onedrive_children_url(parent_id: str) -> str:
    """Graph collection address to create a child under parent_id."""
    if parent_id == "root":
        return f"{_GRAPH_BASE}/me/drive/root/children"
    return f"{_GRAPH_BASE}/me/drive/items/{parent_id}/children"

def _onedrive_folder_id(
    sess, headers: dict, parent_id: str, name: str
) -> Optional[str]:
    """Read-only lookup: a folder id, or None when absent or not a folder.

    This is the probe the read-only walk uses. A name that resolves to a file
    returns None (the walk treats it as a missing folder), while a Graph error
    other than 404 raises.
    """
    url = _onedrive_item_url(name, parent_id)
    resp = sess.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        info = resp.json()
        if info.get("folder") is None:
            return None
        folder_id = info.get("id")
        if not folder_id:
            raise RuntimeError(f"Graph returned a folder without an id for {name!r}")
        return folder_id
    if resp.status_code == 404:
        return None
    raise RuntimeError(f"Graph GET {url} → {resp.status_code}: {(resp.text or '')[:300]}")

def _onedrive_probe_folder(
    sess, headers: dict, parent_id: str, name: str
) -> Optional[str]:
    """Probe used by the create walk: an id, or None when the child is missing.

    It mirrors the old ensure lookup: a 200 response whose item carries an id is
    accepted, and only a response with neither a folder nor an id raises.
    """
    url = _onedrive_item_url(name, parent_id)
    resp = sess.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        info = resp.json()
        if info.get("folder") is not None or info.get("id"):
            return info["id"]
        raise RuntimeError(f"Graph path exists but is not a folder: {name}")
    if resp.status_code != 404:
        raise RuntimeError(
            f"Graph GET {url} → {resp.status_code}: {(resp.text or '')[:300]}"
        )
    return None

def _onedrive_create_folder(
    sess, headers: dict, parent_id: str, name: str
) -> str:
    """Create a folder under parent_id; return its id.

    A 409 means another creator won the race, so the existing id is resolved
    with a re-GET.
    """
    create_url = _onedrive_children_url(parent_id)
    create_resp = sess.post(
        create_url,
        headers={**headers, "Content-Type": "application/json"},
        json={"name": name, "folder": {}},
        timeout=30,
    )
    if create_resp.status_code in (200, 201):
        return create_resp.json()["id"]
    if create_resp.status_code == 409:
        retry = sess.get(
            _onedrive_item_url(name, parent_id), headers=headers, timeout=30
        )
        if retry.status_code == 200:
            return retry.json()["id"]
        raise RuntimeError(
            f"Graph create {name} conflicted (409) and re-GET failed: "
            f"{retry.status_code}: {(retry.text or '')[:300]}"
        )
    raise RuntimeError(
        f"Graph POST {create_url} → {create_resp.status_code}: "
        f"{(create_resp.text or '')[:300]}"
    )


def _onedrive_item_by_name(
    sess, headers: dict, parent_id: str, name: str
) -> Optional[str]:
    """Resolve a file id by name under parent_id (None when missing)."""
    url = _onedrive_item_url(name, parent_id)
    resp = sess.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        info = resp.json()
        if info.get("folder") is None and info.get("id"):
            return info["id"]
        return None  # a folder with that name, not a file
    if resp.status_code == 404:
        return None
    raise RuntimeError(
        f"Graph GET {url} → {resp.status_code}: "
        f"{(resp.text or '')[:300]}"
    )

def _onedrive_remote_dir_exists(sess, headers: dict, remote_dir: str) -> Optional[str]:
    """Resolve an existing remote folder without creating any part of it."""
    return walk_folders(
        remote_dir,
        find=lambda parent, name: _onedrive_folder_id(sess, headers, parent, name),
    )

def _onedrive_ensure_remote_dir(
    sess, headers: dict, remote_dir: str
) -> str:
    """Ensure the OneDrive folder chain for remote_dir; return deepest folder id.

    The shared walk in base.py finds or creates each segment: the first segment
    is created under the drive root and later segments nest under the previously
    resolved folder id, so OneDrive is a creating provider.
    """
    folder_id = walk_folders(
        remote_dir,
        find=lambda parent, name: _onedrive_probe_folder(sess, headers, parent, name),
        make=lambda parent, name: _onedrive_create_folder(sess, headers, parent, name),
        require_nonempty=True,
    )
    assert folder_id is not None
    return folder_id

def _onedrive_upload_file(
    token: str,
    local_path: Path,
    remote_dir: str,
    on_progress: Optional[callable],
    overwrite: str = "fail",
) -> tuple[bool, Optional[str]]:
    """Upload one file to OneDrive via a chunked createUploadSession.

    Ensures the parent folder chain exists, then PUTs 10 MiB chunks
    (a multiple of the required 320 KiB fragment size) through the session
    upload URL. Handles 202 continuation, 416 resume, 404 expiry, and
    429/5xx retries. Returns (success, error_message).

    overwrite: "fail" → existing file is an error; "skip" → success without
    uploading; "overwrite" and "replace" → conflictBehavior replace.
    """
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(_ONEDRIVE_IMPORT_HINT) from exc
    total = local_path.stat().st_size
    headers = {"Authorization": f"Bearer {token}"}
    sess = requests.Session()
    series_folder_id = _onedrive_ensure_remote_dir(sess, headers, remote_dir)
    quoted_name = urllib.parse.quote(local_path.name, safe="")

    # Existing-file policy: Graph resolve-by-name GET under the folder.
    existing = _onedrive_item_by_name(sess, headers, series_folder_id, local_path.name)
    if existing:
        if overwrite == "skip":
            return True, None, None
        if overwrite not in ("overwrite", "replace"):
            return (
                False,
                f"destination already exists: {local_path.name} "
                "(send overwrite=overwrite to replace it, or overwrite=skip "
                "to keep the existing copy)",
                None,
            )

    create_url = (
        f"{_GRAPH_BASE}/me/drive/items/{series_folder_id}:/"
        f"{quoted_name}:/createUploadSession"
    )
    conflict = "replace" if overwrite in ("overwrite", "replace") else "fail"
    try:
        resp = sess.post(
            create_url,
            headers={**headers, "Content-Type": "application/json"},
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
            timeout=30,
        )
    except Exception as exc:
        return False, f"OneDrive createUploadSession failed: {exc}", None
    if resp.status_code not in (200, 201):
        return (
            False,
            f"OneDrive createUploadSession → {resp.status_code}: "
            f"{(resp.text or '')[:300]}",
        )
    upload_url = resp.json().get("uploadUrl")
    if not upload_url:
        return False, "OneDrive createUploadSession returned no uploadUrl", None

    CHUNK = 10 * 1024 * 1024  # multiple of 320 KiB (327680)
    start = 0
    last_time = time.monotonic()
    last_done = 0
    tries = 0
    range_attempts = 0
    range_steps = 0
    try:
        with open(local_path, "rb") as f:
            while start < total:
                end = min(start + CHUNK, total) - 1
                f.seek(start)
                data = f.read(end - start + 1)
                put_headers = {
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(data)),
                }  # NO Authorization header on session PUTs (docs: can 401)
                put_resp = sess.put(upload_url, data=data, headers=put_headers, timeout=300)
                status = put_resp.status_code
                if status == 202:
                    next_ranges = put_resp.json().get("nextExpectedRanges") or []
                    if not next_ranges:
                        return False, "OneDrive returned 202 without nextExpectedRanges", None
                    try:
                        nxt = int(str(next_ranges[0]).split("-")[0])
                    except (ValueError, IndexError):
                        return False, f"OneDrive returned an invalid next range: {next_ranges}", None
                    if nxt <= start or nxt > total:
                        return False, f"OneDrive upload range did not advance: {start} -> {nxt}", None
                    range_attempts += 1
                    range_steps += 1
                    if range_attempts > 3 or range_steps > 10000:
                        return False, "OneDrive upload made too many range requests", None
                    start = nxt
                    range_attempts = 0
                    now = time.monotonic()
                    dt = now - last_time
                    speed = int((start - last_done) / dt) if dt > 0 else 0
                    last_time, last_done = now, start
                    if on_progress and (
                        start >= total or start <= 0 or dt >= 0.25
                    ):
                        on_progress(start, total, speed)
                    tries = 0
                    continue
                if status in (200, 201):
                    if on_progress:
                        on_progress(total, total, 0)
                    try:
                        item = put_resp.json()
                        url = item.get("webUrl")
                    except Exception:
                        url = None
                    return True, None, url
                if status == 416:
                    # Range not satisfiable — fetch the server's expected range,
                    # but require forward progress and a bounded number of probes.
                    get_resp = sess.get(upload_url, timeout=30)
                    if get_resp.status_code in (200, 202):
                        body = get_resp.json()
                        ranges = body.get("nextExpectedRanges") or []
                        if ranges:
                            try:
                                next_start = int(str(ranges[0]).split("-")[0])
                            except (ValueError, IndexError):
                                return False, f"OneDrive upload 416 with unparseable ranges: {ranges}", None
                            if next_start <= start or next_start > total:
                                return False, f"OneDrive upload range did not advance: {start} -> {next_start}", None
                            range_attempts += 1
                            range_steps += 1
                            if range_attempts > 3 or range_steps > 10000:
                                return False, "OneDrive upload made too many range requests", None
                            start = next_start
                            continue
                    return False, (
                        f"OneDrive upload 416 and no resumable range: "
                        f"{(get_resp.text or put_resp.text or '')[:300]}"
                    ), None
                if status == 404:
                    return False, "OneDrive upload session expired — recreate & restart the upload", None
                if status in (429, 500, 502, 503, 504) and tries < 3:
                    tries += 1
                    retry_after = put_resp.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 1.0 * tries
                    except ValueError:
                        delay = 1.0 * tries
                    time.sleep(min(delay, 10))
                    continue
                return False, (
                    f"OneDrive chunk PUT → {status}: {(put_resp.text or '')[:300]}"
                ), None
        # Only an explicit final 2xx response is accepted. A 202 response with
        # a missing/non-advancing range is not proof that the upload completed.
        if on_progress:
            on_progress(total, total, 0)
        return False, "OneDrive upload ended without a final 2xx response", None
    except Exception as exc:
        return False, str(exc), None


def _onedrive_account_view(instance: accounts.Instance) -> ProviderAccount:
    """Labels and health fields for one OneDrive account."""
    return ProviderAccount(
        label=instance.display_name_for("OneDrive"),
        extra={"root": instance.root_path},
        current_folder=f"{instance.root_path} (a folder in that account's drive)",
    )


def _onedrive_upload(
    name: str,
    root: str,
    remote_dir: str,
    local_path: Path,
    on_progress: Optional[callable],
    overwrite: str = "fail",
) -> tuple[bool, Optional[str], Optional[str]]:
    """Upload one file for one OneDrive account.

    Acquires a token, then runs the chunked upload session (which ensures the
    folder chain itself). Any failure, including a missing or unrefreshable
    token, becomes a failure tuple.
    """
    try:
        with provider_account_lock("onedrive", name):
            token = _onedrive_token(name)
            return _onedrive_upload_file(token, local_path, remote_dir, on_progress, overwrite)
    except Exception as error:
        return False, str(error), None


def _onedrive_account_remote_exists(name: str, remote_dir: str, filename: str) -> bool:
    """Whether a file exists at ``remote_dir/filename`` for one OneDrive account.

    The folder walk is read-only, so no part of the path is created.
    """
    import requests

    with provider_account_lock("onedrive", name):
        sess = requests.Session()
        headers = _onedrive_headers(name)
        folder_id = _onedrive_remote_dir_exists(sess, headers, remote_dir)
        return bool(folder_id and _onedrive_item_by_name(sess, headers, folder_id, filename))


def _onedrive_account_download(name: str, remote_dir: str, filename: str) -> Optional[bytes]:
    """Read ``remote_dir/filename`` for one OneDrive account; None when absent.
    Graph redirects to the download URL; the size is checked after the body."""
    import requests

    with provider_account_lock("onedrive", name):
        sess = requests.Session()
        headers = _onedrive_headers(name)
        folder_id = _onedrive_remote_dir_exists(sess, headers, remote_dir)
        if not folder_id:
            return None
        item_id = _onedrive_item_by_name(sess, headers, folder_id, filename)
        if not item_id:
            return None
        resp = _graph_request(
            sess,
            "GET",
            f"{_GRAPH_BASE}/me/drive/items/{item_id}/content",
            headers=headers,
            allow_redirects=True,
        )
        if len(resp.content or b"") > DOWNLOAD_LIMIT_BYTES:
            raise RuntimeError(
                f"refusing to read {remote_dir}/{filename}: {len(resp.content)} bytes "
                f"exceeds the {DOWNLOAD_LIMIT_BYTES}-byte sidecar limit"
            )
        return resp.content


def _onedrive_account_list_folders(name: str, remote_dir: str) -> list[dict]:
    """List one OneDrive folder (read-only); "" lists the drive root."""
    import requests

    base = str(remote_dir).strip("/")
    with provider_account_lock("onedrive", name):
        sess = requests.Session()
        headers = _onedrive_headers(name)
        parent = "root"
        if base:
            parent = _onedrive_remote_dir_exists(sess, headers, base)
        if not parent:
            return []
        resp = _graph_request(
            sess,
            "GET",
            f"{_onedrive_children_url(parent)}"
            f"?$select=id,name,folder,size&$top={LIST_LIMIT_ENTRIES}",
            headers=headers,
        )
    entries = []
    for item in (resp.json() or {}).get("value", []):
        size = item.get("size")
        entries.append(
            listing_entry(
                str(item.get("name", "")),
                f"{base}/{item.get('name', '')}".strip("/"),
                is_folder=bool(item.get("folder")),
                size=size if isinstance(size, int) else None,
            )
        )
    return entries


PROVIDER = Provider(
    id="onedrive",
    label="OneDrive",
    configured=_onedrive_configured,
    creds_source=lambda name: "token" if _onedrive_configured(name) else None,
    upload=_onedrive_upload,
    remote_exists=_onedrive_account_remote_exists,
    download=_onedrive_account_download,
    list_folders=_onedrive_account_list_folders,
    run_setup=_run_setup_onedrive,
    account=_onedrive_account_view,
)
