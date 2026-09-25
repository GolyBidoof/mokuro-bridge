from __future__ import annotations
import importlib.util
import os
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from .. import accounts
from ..accounts import DEFAULT_NAME
from ..util import _chmod_fd_private
from ..config import ONEDRIVE_CLIENT_ID, ONEDRIVE_TOKEN_FILE, _GRAPH_BASE, _ONEDRIVE_SCOPES

_ONEDRIVE_IMPORT_HINT = (
    "OneDrive upload needs the msal and requests libraries. "
    "Install them with: pip install -r requirements-onedrive.txt"
)

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
        return "python server.py --setup-upload onedrive"
    return f"python server.py --setup-upload onedrive --name {name}"

def _onedrive_configured(name: str = DEFAULT_NAME) -> bool:
    """Whether one OneDrive account is usable right now."""
    return (
        importlib.util.find_spec("msal") is not None
        and bool(ONEDRIVE_CLIENT_ID)
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
        client_id=ONEDRIVE_CLIENT_ID,
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
            "then re-run: python server.py --setup-upload onedrive"
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
    client_id = ONEDRIVE_CLIENT_ID or input("Azure app client ID: ").strip()
    if not client_id:
        print(
            "No client ID given. In the Azure portal:\n"
            "  1. Create an app registration\n"
            "  2. Add the 'Mobile and desktop applications' platform and "
            "enable public client flows\n"
            "  3. Add the delegated Microsoft Graph permission "
            "'Files.ReadWrite'\n"
            "  4. Copy the Application (client) ID into "
            "ONEDRIVE_CLIENT_ID (env) or enter it above."
        )
        return

    cache = _load_token_cache(name)
    app = PublicClientApplication(
        client_id=client_id,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    flow = app.initiate_device_flow(scopes=_ONEDRIVE_SCOPES)
    if "user_code" not in flow:
        print(f"error: could not start device flow: {flow.get('error_description', flow)}")
        return
    print(flow.get("message", ""))
    print("Waiting for you to sign in…")
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print(
            "error: sign-in failed: "
            f"{result.get('error')}: {result.get('error_description', '')}"
        )
        return
    _save_token_cache(cache, name)
    print(f"OneDrive token stored in {token_path} (permissions 0600).")
    stored = accounts.save_instance(
        "onedrive",
        name,
        label=(label or None),
        root=(root or None),
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

def _onedrive_ensure_folder(
    sess, headers: dict, parent_id: str, name: str
) -> str:
    """Find (or create) a folder named `name` under parent_id; return its id.

    GET 200 → exists; GET 404 → create via POST children; create 409 →
    already exists → re-GET to resolve the id. Any other status raises.
    """
    quoted = urllib.parse.quote(name, safe="")
    if parent_id == "root":
        check_url = f"{_GRAPH_BASE}/me/drive/root:/{quoted}:"
        create_url = f"{_GRAPH_BASE}/me/drive/root/children"
    else:
        check_url = f"{_GRAPH_BASE}/me/drive/items/{parent_id}:/{quoted}:"
        create_url = f"{_GRAPH_BASE}/me/drive/items/{parent_id}/children"
    resp = sess.get(check_url, headers=headers, timeout=30)
    if resp.status_code == 200:
        info = resp.json()
        if info.get("folder") is not None or info.get("id"):
            return info["id"]
        raise RuntimeError(f"Graph path exists but is not a folder: {name}")
    if resp.status_code != 404:
        raise RuntimeError(
            f"Graph GET {check_url} → {resp.status_code}: "
            f"{(resp.text or '')[:300]}"
        )
    # Missing → create.
    create_resp = sess.post(
        create_url,
        headers={**headers, "Content-Type": "application/json"},
        json={"name": name, "folder": {}},
        timeout=30,
    )
    if create_resp.status_code in (200, 201):
        return create_resp.json()["id"]
    if create_resp.status_code == 409:
        # Raced with another creator — resolve the existing id.
        retry = sess.get(check_url, headers=headers, timeout=30)
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
    quoted = urllib.parse.quote(name, safe="")
    check_url = f"{_GRAPH_BASE}/me/drive/items/{parent_id}:/{quoted}:"
    resp = sess.get(check_url, headers=headers, timeout=30)
    if resp.status_code == 200:
        info = resp.json()
        if info.get("folder") is None and info.get("id"):
            return info["id"]
        return None  # a folder with that name, not a file
    if resp.status_code == 404:
        return None
    raise RuntimeError(
        f"Graph GET {check_url} → {resp.status_code}: "
        f"{(resp.text or '')[:300]}"
    )

def _onedrive_ensure_remote_dir(
    sess, headers: dict, remote_dir: str
) -> str:
    """Ensure the OneDrive folder chain for remote_dir; return deepest folder id.

    First segment is created under drive root, later segments nest under the
    previously resolved folder id.
    """
    segments = [seg for seg in str(remote_dir or "").strip("/").split("/") if seg]
    if not segments:
        raise ValueError(f"empty OneDrive remote path: {remote_dir!r}")
    parent_id = "root"
    for seg in segments:
        parent_id = _onedrive_ensure_folder(sess, headers, parent_id, seg)
    return parent_id

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

    overwrite: "fail" → an existing file with the same name is an error;
    "skip" → existing file counts as success (nothing uploaded);
    "overwrite" → conflictBehavior replace (fresh copy replaces the old).
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
        if overwrite != "overwrite":
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
    conflict = "replace" if overwrite == "overwrite" else "fail"
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
