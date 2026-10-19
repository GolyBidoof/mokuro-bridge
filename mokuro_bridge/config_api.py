"""The configuration dashboard and the JSON API behind it.

`GET /` serves one self-contained page (no external assets, no CDN) showing the
bridge's health and editing its upload targets, default folders and settings.
Writes are accepted only from a loopback `Origin`, or from a client that sends
no `Origin` at all (curl, the CLI): the bridge is unauthenticated, so a page in
the user's browser must not be able to repoint an upload destination.

Accounts can be added from the page: MEGA collects an email + password and
verifies them with megatools, OneDrive runs Microsoft's device-code flow, and
Google Drive runs an OAuth redirect flow whose callback is this same server.
Both interactive flows are asynchronous (start → poll status) because the
sign-in happens in another tab. Secrets are written to each provider's existing
store, never returned to the page.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import APP_NAME, __version__, accounts, settings
from . import providers
from .config import (
    ACCOUNTS_DIR,
    OUTPUT_DIR,
    WORK_DIR,
    _LOCAL_INGEST_ROOTS,
    _load_remembered_local_dir,
    _remember_local_dir,
)
from .providers import (
    _build_upload_methods,
    _default_upload_method,
    _method_current_folder,
    _remember_upload_method,
    resolve_upload_method,
    safe_provider_error,
)
from .providers import drive as drive_provider
from .providers import mega as mega_provider
from .providers import onedrive as onedrive_provider
from .providers import webdav as webdav_provider

router = APIRouter()
DASHBOARD_HTML = Path(__file__).with_name("web") / "dashboard.html"
_LOCAL_ID = "local"
_MAX_DIRS = 400

# The browser sign-in flows live in memory only: a restart cancels a sign-in in
# progress, which is correct because the callback URL would not survive it.
_FLOW_TTL_S = 600.0
_MAX_FLOWS = 64
_flows: dict[str, dict] = {}
_oauth_states: dict[str, str] = {}
_flows_lock = threading.RLock()

# What the page needs to render one "add an account" card per provider.
_PROVIDER_UI = {
    "mega": {
        "label": "MEGA",
        "browser_setup": "form",
        "setup_hint": "mokuro-bridge --setup-upload mega",
    },
    "drive": {
        "label": "Google Drive",
        "browser_setup": "oauth",
        "setup_hint": "mokuro-bridge --setup-upload drive",
    },
    "onedrive": {
        "label": "OneDrive",
        "browser_setup": "device_code",
        "setup_hint": "mokuro-bridge --setup-upload onedrive",
    },
    "webdav": {
        "label": "WebDAV",
        "browser_setup": "form",
        "setup_hint": "mokuro-bridge --setup-upload webdav",
    },
}

def _restart_command() -> str:
    if sys.platform == "darwin":
        return "launchctl kickstart -k gui/$(id -u)/com.mokuro-bridge"
    return "restart the process running mokuro-bridge"


def _require_local_origin(request: Request) -> None:
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    host = origin.split("//", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]").casefold()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(
            status_code=403, detail="configuration changes are accepted only from this machine"
        )


def _is_allowed_local(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for root in _LOCAL_INGEST_ROOTS:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _local_dirs(raw_path: str) -> dict:
    from .api import _resolve_local_output_dir  # api includes this router, so import late

    target = Path(raw_path).expanduser() if str(raw_path).strip() else Path.home()
    try:
        resolved = _resolve_local_output_dir(str(target))
    except HTTPException as error:
        raise HTTPException(status_code=400, detail=str(error.detail)) from error
    entries = []
    try:
        children = sorted(resolved.iterdir(), key=lambda p: p.name.casefold())
    except OSError as error:
        raise HTTPException(status_code=400, detail=f"cannot read {resolved}: {error}") from error
    for child in children[: _MAX_DIRS * 4]:
        if child.name.startswith("."):
            continue
        try:
            if child.is_symlink() or not child.is_dir():
                continue
        except OSError:
            continue
        entries.append({"name": child.name, "path": str(child), "type": "folder"})
        if len(entries) >= _MAX_DIRS:
            break
    parent = str(resolved.parent) if _is_allowed_local(resolved.parent) else ""
    return {
        "path": str(resolved),
        "parent": parent,
        "home": str(Path.home()),
        "entries": entries,
        "truncated": len(children) > len(entries),
    }


async def _remote_dirs(method: str, raw_path: str) -> dict:
    root = providers.method_root(method) if method != _LOCAL_ID else ""
    target = str(raw_path).strip() or root
    try:
        entries = await asyncio.to_thread(providers.list_remote_folders, method, target)
    except Exception as error:
        raise HTTPException(
            status_code=502, detail=f"listing failed: {safe_provider_error(error)}"
        ) from error
    stripped = target.rstrip("/")
    parent = stripped.rsplit("/", 1)[0] if "/" in stripped else ""
    return {"path": target, "parent": parent, "root": root, "entries": entries, "truncated": False}


@router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    # Read the file every request: the page is small, and editing it must not
    # require restarting a service that may be mid-upload.
    try:
        page = DASHBOARD_HTML.read_text(encoding="utf-8")
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"dashboard page missing: {error}") from error
    page = page.replace("{{VERSION}}", __version__).replace("{{APP}}", APP_NAME)
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


# --- signing-in flow bookkeeping -------------------------------------------

def _purge_flows() -> None:
    now = time.monotonic()
    with _flows_lock:
        for flow_id, flow in list(_flows.items()):
            if now - float(flow.get("created", 0.0)) <= _FLOW_TTL_S:
                continue
            del _flows[flow_id]
            for state, owner in list(_oauth_states.items()):
                if owner == flow_id:
                    del _oauth_states[state]


def _new_flow(kind: str, **fields) -> str:
    _purge_flows()
    flow_id = uuid.uuid4().hex
    with _flows_lock:
        if len(_flows) >= _MAX_FLOWS:
            oldest = min(_flows, key=lambda key: _flows[key].get("created", 0.0))
            del _flows[oldest]
        _flows[flow_id] = {
            "id": flow_id,
            "kind": kind,
            "status": "pending",
            "detail": "",
            "method": "",
            "created": time.monotonic(),
            **fields,
        }
    return flow_id


def _update_flow(flow_id: str, **fields) -> None:
    with _flows_lock:
        flow = _flows.get(flow_id)
        if flow is not None:
            flow.update(fields)


def _flow_snapshot(flow_id: str) -> Optional[dict]:
    _purge_flows()
    with _flows_lock:
        flow = _flows.get(flow_id)
        return dict(flow) if flow is not None else None


def _flow_response(flow: Optional[dict]) -> JSONResponse:
    if flow is None:
        return JSONResponse(
            {"status": "error", "detail": "unknown or expired setup flow"}
        )
    body: dict = {"status": flow.get("status", "error")}
    if flow.get("method"):
        body["method"] = flow["method"]
    if flow.get("detail"):
        body["detail"] = safe_provider_error(flow["detail"])
    return JSONResponse(body)


def _register_oauth_state(state: str, flow_id: str) -> None:
    with _flows_lock:
        _oauth_states[str(state)] = flow_id


def _take_oauth_state(state: str) -> Optional[str]:
    _purge_flows()
    with _flows_lock:
        return _oauth_states.pop(str(state or ""), None)


def _account_target(provider: str, alias: str) -> tuple[str, str]:
    """(storage name, alias) for a browser sign-in.

    An alias that already names an account of this provider re-credentials that
    account in place, which is what the dashboard's "Reconnect" button does
    (root, label, default flag and historical id survive). An alias that is
    taken by another provider is a 400. Any other alias creates a new account,
    validated for collisions first.
    """
    value = str(alias or "").strip().lower()
    if not value:
        raise ValueError("an alias is required, for example drive-next")
    existing = accounts.find_alias(value)
    if existing is not None:
        if existing.provider != provider:
            raise ValueError(
                f"alias {value!r} already belongs to the {existing.provider} provider; "
                f"pick another one (for example {accounts.suggest_alias(provider)})"
            )
        return existing.name, existing.id
    clean = accounts.validate_alias(provider, value)
    return accounts.alias_to_name(provider, clean), clean


def _callback_url(request: Request, path: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _callback_page(status_code: int, title: str, detail: str = "") -> HTMLResponse:
    """A small self-contained page for the OAuth popup/tab, then close it."""
    closing = (
        "<p>You can close this tab; the dashboard has already been updated.</p>"
        if status_code == 200
        else "<p>Close this tab and start the sign-in again from the dashboard.</p>"
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)} - {html.escape(APP_NAME)}</title>
<style>
 body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0; min-height: 100vh; display: grid; place-items: center;
        background: #f6f6f4; color: #1d1d1b; }}
 main {{ max-width: 34rem; padding: 2rem 2.5rem; background: #fff;
         border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
 h1 {{ font-size: 1.25rem; margin: 0 0 .75rem; }}
 p  {{ margin: .35rem 0; color: #4a4a47; }}
</style>
</head>
<body>
<main>
<h1>{html.escape(title)}</h1>
<p>{html.escape(detail)}</p>
{closing}
</main>
<script>setTimeout(function () {{ window.close(); }}, 2000);</script>
</body>
</html>
"""
    return HTMLResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})


# --- GET /config ------------------------------------------------------------

def _provider_rows() -> list[dict]:
    """One row per provider: what the page may offer and what is missing."""
    rows = []
    for provider_id, meta in _PROVIDER_UI.items():
        entry = {
            "id": provider_id,
            "label": meta["label"],
            "browser_setup": meta["browser_setup"],
            "setup_hint": meta["setup_hint"],
            "available": True,
            "missing": [],
            "alias_suggestion": accounts.suggest_alias(provider_id),
        }
        if provider_id == "mega":
            if shutil.which("megatools") is None:
                entry["available"] = False
                entry["missing"].append("megatools")
        elif provider_id == "drive":
            missing = [
                package
                for package, module in (
                    ("google-auth-oauthlib", "google_auth_oauthlib"),
                    ("google-api-python-client", "googleapiclient"),
                )
                if not _module_available(module)
            ]
            entry["missing"] = missing
            entry["available"] = not missing
            entry["client_preset"] = bool(
                drive_provider._drive_client_id_from_env()
                or os.environ.get("DRIVE_CLIENT_SECRET", "").strip()
                or _drive_secret_file_configured()
            )
        elif provider_id == "onedrive":
            missing = [
                package
                for package, module in (("msal", "msal"), ("requests", "requests"))
                if not _module_available(module)
            ]
            if not onedrive_provider.ONEDRIVE_CLIENT_ID:
                missing.append("ONEDRIVE_CLIENT_ID")
            entry["missing"] = missing
            entry["available"] = not missing
            entry["client_preset"] = bool(onedrive_provider.ONEDRIVE_CLIENT_ID)
        elif provider_id == "webdav":
            # Any WebDAV server is acceptable, so the only install check is the
            # HTTP client; the server URL is asked for per account.
            missing = [] if _module_available("requests") else ["requests"]
            entry["missing"] = missing
            entry["available"] = not missing
        rows.append(entry)
    return rows


def _module_available(module: str) -> bool:
    """Whether an optional provider library is importable (mockable in tests)."""
    return importlib.util.find_spec(module) is not None


def _drive_secret_file_configured() -> bool:
    raw = os.environ.get(drive_provider._DRIVE_CLIENT_SECRET_ENV, "").strip()
    return bool(raw) and Path(raw).expanduser().is_file()


def _creds_present(instance: Optional[accounts.Instance]) -> bool:
    """Whether *some* credential material exists, without contacting a provider."""
    if instance is None:
        return False
    provider = providers._PROVIDERS.get(instance.provider)
    try:
        if provider is not None and provider.creds_source(instance.name):
            return True
    except Exception:
        pass
    if instance.provider == "mega":
        return bool(instance.email)
    if instance.provider == "drive":
        return drive_provider._drive_creds_path(instance.name).is_file()
    if instance.provider == "onedrive":
        return onedrive_provider._onedrive_token_path(instance.name).is_file()
    if instance.provider == "webdav":
        return webdav_provider._creds_path(instance.name).is_file()
    return False


def _method_state(method, instance: Optional[accounts.Instance]) -> tuple[str, bool]:
    """(state, needs_reauth) for one account row.

    "connected" means usable right now. "needs_sign_in" means credential
    material exists but the account is not usable (revoked/expired token, or a
    provider library missing; the provider rows say which). "not_configured"
    means there is nothing stored yet.
    """
    if method.configured:
        return "connected", False
    if _creds_present(instance):
        return "needs_sign_in", True
    return "not_configured", False


_IDENTITY_PROVIDERS = {
    "mega": mega_provider.account_identity,
    "drive": drive_provider.account_identity,
    "onedrive": onedrive_provider.account_identity,
    "webdav": webdav_provider.account_identity,
}

def _account_identity(instance: Optional[accounts.Instance]) -> str:
    """Locally derivable, non-secret identity (username/email) of one account.

    Only local files/metadata/OS-store reads: no network call, no secret. Empty
    when the provider cannot tell who the account is.
    """
    if instance is None:
        return ""
    derive = _IDENTITY_PROVIDERS.get(instance.provider)
    if derive is None:
        return ""
    try:
        return str(derive(instance) or "").strip()
    except Exception:
        return ""  # identity is decoration: never fail /config over it


def _method_rows() -> list[dict]:
    rows = []
    methods = _build_upload_methods()
    for method in methods.values():
        instance = providers.instance_for(method.id)
        state, needs_reauth = _method_state(method, instance)
        provider_id = instance.provider if instance is not None else _LOCAL_ID
        extra = {
            key: value
            for key, value in method.extra.items()
            if key not in ("creds_source", "tracked", "alias") and value not in (None, "")
        }
        # The provider views only know metadata; for accounts configured through
        # an older path the username lives in the credential store instead.
        if instance is not None and not str(extra.get("email") or "").strip():
            identity = _account_identity(instance)
            if identity:
                extra["email"] = identity
        rows.append(
            {
                "id": method.id,
                "alias": method.id,
                "name": method.name,
                "configured": method.configured,
                "default": method.default,
                "provider": provider_id,
                "provider_label": _PROVIDER_UI.get(provider_id, {}).get("label", "Local folder"),
                "account": (instance.name if instance else ""),
                "label": (instance.label if instance else ""),
                "root": (instance.root if instance else _method_current_folder(_LOCAL_ID)),
                "creds_source": method.extra.get("creds_source"),
                "current_folder": _method_current_folder(method.id),
                "needs_reauth": needs_reauth,
                "state": state,
                "extra": extra,
            }
        )
    return rows


@router.get("/config")
async def read_config(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "app": APP_NAME,
            "version": __version__,
            "host": request.headers.get("host", ""),
            "default_method": await asyncio.to_thread(_default_upload_method),
            "local_output": await asyncio.to_thread(_method_current_folder, _LOCAL_ID),
            "local_output_remembered": bool(_load_remembered_local_dir()),
            "output_dir": str(OUTPUT_DIR),
            "work_dir": str(WORK_DIR),
            "accounts_dir": str(ACCOUNTS_DIR),
            "settings_file": str(settings.SETTINGS_FILE),
            "providers": await asyncio.to_thread(_provider_rows),
            "methods": await asyncio.to_thread(_method_rows),
            "settings": await asyncio.to_thread(settings.snapshot),
            "restart_command": _restart_command(),
            "platform": sys.platform,
            "writable": True,
        }
    )


@router.get("/config/browse")
async def browse(method: str = "local", path: str = "") -> JSONResponse:
    """One directory listing: provider folders, or local folders under an allowed root."""
    try:
        method_id = resolve_upload_method(method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if method_id == _LOCAL_ID:
        return JSONResponse(await asyncio.to_thread(_local_dirs, path))
    return JSONResponse(await _remote_dirs(method_id, path))


# --- account browsing / defaults --------------------------------------------

@router.post("/config/account")
async def save_account(
    request: Request,
    method: str = Form(...),
    root: str = Form(""),
    label: str = Form(""),
) -> JSONResponse:
    """Set one account's default upload directory (and its display label)."""
    _require_local_origin(request)
    try:
        provider, name = accounts.parse_method_id(method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if accounts.load_instance(provider, name) is None and name != accounts.DEFAULT_NAME:
        raise HTTPException(status_code=404, detail=f"unknown account: {method}")
    try:
        instance = await asyncio.to_thread(
            accounts.save_instance,
            provider,
            name,
            root=(root.strip() or None),
            label=(label.strip() or None),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse({"ok": True, "method": instance.id, "root": instance.root})


@router.post("/config/default")
async def save_default(request: Request, method: str = Form(...)) -> JSONResponse:
    """Set the sticky default upload target. Applies to the next finalize."""
    _require_local_origin(request)
    try:
        resolved = resolve_upload_method(method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if resolved != _LOCAL_ID and (await asyncio.to_thread(_build_upload_methods)).get(resolved) is None:
        raise HTTPException(status_code=404, detail=f"unknown destination: {method}")
    await asyncio.to_thread(_remember_upload_method, resolved)
    if resolved != _LOCAL_ID and (
        await asyncio.to_thread(_default_upload_method)
    ) != resolved:
        raise HTTPException(
            status_code=409,
            detail="that destination is not configured yet, so it cannot be the default",
        )
    return JSONResponse({"ok": True, "default_method": resolved})


@router.post("/config/local-output")
async def save_local_output(request: Request, path: str = Form(...)) -> JSONResponse:
    """Choose the default local output folder (the sticky `local` destination)."""
    _require_local_origin(request)
    if not str(path).strip():
        raise HTTPException(status_code=400, detail="path is required")
    listing = await asyncio.to_thread(_local_dirs, path)
    await asyncio.to_thread(_remember_local_dir, listing["path"])
    return JSONResponse({"ok": True, "local_output": listing["path"]})


@router.post("/config/new-folder")
async def new_local_folder(request: Request, parent: str = Form(...), name: str = Form(...)) -> JSONResponse:
    """Create one folder under an allowed local root, for the output picker."""
    _require_local_origin(request)
    clean = str(name).strip()
    if not clean or any(part in clean for part in ("/", "\\")) or clean.startswith("."):
        raise HTTPException(status_code=400, detail="folder name must be a single plain name")
    listing = await asyncio.to_thread(_local_dirs, parent)
    target = Path(listing["path"]) / clean
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise HTTPException(status_code=400, detail="that name is not a folder")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HTTPException(status_code=400, detail=f"could not create {target}: {error}") from error
    return JSONResponse({"ok": True, "path": str(target)})


@router.post("/config/settings")
async def save_settings(request: Request) -> JSONResponse:
    """Persist wizard settings. Most need a restart; the page says which."""
    _require_local_origin(request)
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="expected a JSON object") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    saved, problems = await asyncio.to_thread(settings.update, payload)
    if problems:
        return JSONResponse({"ok": False, "problems": problems}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "saved": saved,
            "settings": await asyncio.to_thread(settings.snapshot),
            "restart_command": _restart_command(),
        }
    )


@router.post("/config/forget")
async def forget(request: Request, method: str = Form(...)) -> JSONResponse:
    """Remove one upload account: metadata, secret files and keychain item."""
    _require_local_origin(request)
    if not str(method).strip() or str(method).strip().lower() == _LOCAL_ID:
        raise HTTPException(status_code=400, detail="a provider account is required")
    try:
        result = await asyncio.to_thread(providers.forget_account, method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse({"ok": True, **result})


# --- adding an account: MEGA (email + password) ----------------------------

_MEGATOOLS_HINT = (
    "MEGA setup needs the megatools command-line tool. Install it with "
    "`brew install megatools` (macOS) or `apt install megatools` "
    "(Debian/Ubuntu), then retry."
)


@router.post("/config/accounts/mega")
async def add_mega_account(
    request: Request,
    alias: str = Form(""),
    label: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    root: str = Form(""),
) -> JSONResponse:
    """Verify MEGA credentials and add (or re-credential) one MEGA account."""
    _require_local_origin(request)
    try:
        name, clean_alias = _account_target("mega", alias)
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    email_value = str(email or "").strip()
    password_value = str(password or "")
    if not email_value or not password_value:
        raise HTTPException(status_code=400, detail="MEGA email and password are required")
    if shutil.which("megatools") is None:
        raise HTTPException(status_code=400, detail=_MEGATOOLS_HINT)
    if name == accounts.DEFAULT_NAME and (
        os.environ.get("MEGA_EMAIL", "").strip()
        and os.environ.get("MEGA_PASSWORD", "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "MEGA_EMAIL/MEGA_PASSWORD are set in the environment, so storing "
                "another MEGA password here would be ignored; unset them first"
            ),
        )
    if (
        name == accounts.DEFAULT_NAME
        and accounts.load_instance("mega").tracked is False
        and mega_provider._mega_creds_source(name)
    ):
        source = mega_provider._mega_creds_source(name)
        raise HTTPException(
            status_code=400,
            detail=(
                f"MEGA is already configured for the first account ({source}); "
                "run `mokuro-bridge --setup-upload mega` to replace it, or add this "
                "account under another alias such as "
                f"{accounts.suggest_alias('mega')!r}"
            ),
        )
    try:
        ok, verify_error = await asyncio.to_thread(
            mega_provider._mega_verify_creds, email_value, password_value
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not ok:
        detail = safe_provider_error(verify_error or "").strip()
        raise HTTPException(
            status_code=400,
            detail=(
                "MEGA login failed - credentials were not stored"
                + (f": {detail}" if detail else "")
            ),
        )
    try:
        instance, _backend, _written = await asyncio.to_thread(
            mega_provider.store_mega_account,
            name,
            email_value,
            password_value,
            alias=clean_alias,
            label=(label.strip() or None),
            root=(root.strip() or None),
        )
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse({"ok": True, "method": instance.id, "root": instance.root_path})


# --- adding an account: WebDAV (base URL + Basic auth) ---------------------

_WEBDAV_LIBS_HINT = (
    "WebDAV upload needs the requests library. Install it with "
    "`pip install -r requirements-webdav.txt`, then retry."
)


@router.post("/config/accounts/webdav")
async def add_webdav_account(
    request: Request,
    alias: str = Form(""),
    label: str = Form(""),
    url: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    root: str = Form(""),
) -> JSONResponse:
    """Verify and store one WebDAV account (server URL + Basic credentials).

    The server is contacted once before anything is written, so a typo in the
    URL or the password is a 400 carrying the server's own answer, not an
    account that silently fails every later upload. Re-submitting an existing
    alias re-credentials it in place, which is the page's Reconnect button.
    """
    _require_local_origin(request)
    if not _module_available("requests"):
        raise HTTPException(status_code=400, detail=_WEBDAV_LIBS_HINT)
    try:
        name, clean_alias = _account_target("webdav", alias)
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    url_value = str(url or "").strip()
    username_value = str(username or "").strip()
    password_value = str(password or "")
    # Reconnecting an existing account may leave a field empty: fall back to
    # what is stored, so "fix the URL" does not force retyping the password.
    if accounts.load_instance("webdav", name) is not None:
        url_value = url_value or webdav_provider.base_url(name)
        username_value = username_value or webdav_provider.username(name)
        password_value = password_value or webdav_provider.password(name)
    if not url_value:
        raise HTTPException(status_code=400, detail="a WebDAV server URL is required")
    if not username_value or not password_value:
        raise HTTPException(
            status_code=400, detail="a WebDAV username and password are required"
        )
    try:
        ok, verify_error = await asyncio.to_thread(
            webdav_provider.verify_webdav_creds, url_value, username_value, password_value
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not ok:
        detail = safe_provider_error(verify_error or "").strip()
        raise HTTPException(
            status_code=400,
            detail=(
                "WebDAV connection failed - nothing was stored"
                + (f": {detail}" if detail else "")
            ),
        )
    try:
        instance = await asyncio.to_thread(
            webdav_provider.store_webdav_account,
            name,
            url_value,
            username_value,
            password_value,
            alias=clean_alias,
            label=(label.strip() or None),
            root=(root.strip() or None),
        )
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse(
        {
            "ok": True,
            "method": instance.id,
            "root": instance.root_path,
            "server": webdav_provider.base_url(instance.name),
            "username": webdav_provider.username(instance.name),
        }
    )


# --- adding an account: OneDrive (device code) -----------------------------

_MSAL_HINT = (
    "OneDrive sign-in needs the msal library. Install it with "
    "`pip install -r requirements-onedrive.txt`, then retry."
)


def _run_onedrive_flow(
    flow_id: str,
    app,
    cache,
    flow: dict,
    name: str,
    alias: str,
    label: str,
) -> None:
    try:
        instance = onedrive_provider.finish_device_flow(
            app, cache, flow, name, alias=alias, label=(label or None)
        )
    except Exception as error:  # provider/network failure: report, never crash
        _update_flow(flow_id, status="error", detail=safe_provider_error(error))
    else:
        _update_flow(flow_id, status="complete", method=instance.id)


@router.post("/config/accounts/onedrive/start")
async def start_onedrive_account(
    request: Request, alias: str = Form(""), label: str = Form("")
) -> JSONResponse:
    """Begin a device-code sign-in for one OneDrive account."""
    _require_local_origin(request)
    if not _module_available("msal"):
        raise HTTPException(status_code=400, detail=_MSAL_HINT)
    try:
        name, clean_alias = _account_target("onedrive", alias)
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        app, cache, flow = await asyncio.to_thread(
            onedrive_provider.start_device_flow, name
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    flow_id = _new_flow("onedrive", name=name, alias=clean_alias, label=label)
    threading.Thread(
        target=_run_onedrive_flow,
        args=(flow_id, app, cache, flow, name, clean_alias, label),
        name=f"onedrive-setup-{flow_id[:8]}",
        daemon=True,
    ).start()
    try:
        expires_in = int(flow.get("expires_in") or 900)
    except (TypeError, ValueError):
        expires_in = 900
    try:
        interval_s = int(flow.get("interval") or 5)
    except (TypeError, ValueError):
        interval_s = 5
    return JSONResponse(
        {
            "ok": True,
            "flow_id": flow_id,
            "user_code": str(flow.get("user_code", "")),
            "verification_uri": str(
                flow.get("verification_uri") or flow.get("verification_url") or ""
            ),
            "expires_in": expires_in,
            "interval_s": max(1, interval_s),
        }
    )


@router.get("/config/accounts/onedrive/status")
async def onedrive_status(flow_id: str = "") -> JSONResponse:
    """Poll a device-code sign-in. 200 with status pending/complete/error."""
    return _flow_response(_flow_snapshot(flow_id))


# --- adding an account: Google Drive (OAuth redirect) ----------------------

_DRIVE_LIBS_HINT = (
    "Google Drive sign-in needs the google client libraries. Install them with "
    "`pip install -r requirements-drive.txt`, then retry."
)


def _drive_libraries_available() -> bool:
    return _module_available("google_auth_oauthlib") and _module_available(
        "googleapiclient"
    )


@router.post("/config/accounts/drive/start")
async def start_drive_account(
    request: Request,
    alias: str = Form(""),
    label: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
) -> JSONResponse:
    """Begin a Google OAuth sign-in; the callback lands on this server."""
    _require_local_origin(request)
    if not _drive_libraries_available():
        raise HTTPException(status_code=400, detail=_DRIVE_LIBS_HINT)
    try:
        name, clean_alias = _account_target("drive", alias)
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        client_config, client_source = await asyncio.to_thread(
            drive_provider.resolve_oauth_client, client_id, client_secret
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if client_source != "secret-file":
        installed = (client_config or {}).get("installed", {})
        try:
            valid = await asyncio.to_thread(
                drive_provider._drive_client_valid,
                str(installed.get("client_id", "")),
                str(installed.get("client_secret", "")),
            )
        except Exception as error:
            raise HTTPException(
                status_code=400,
                detail=(
                    "could not reach Google to verify the OAuth client "
                    f"({safe_provider_error(error)}); check the connection and retry"
                ),
            ) from error
        if not valid:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Google does not recognize that OAuth client ID and secret "
                    "(error: invalid_client). Check for a truncated paste, or make "
                    "sure the credential was created as a 'Desktop app' client."
                ),
            )
    callback = _callback_url(request, "config/oauth/drive/callback")
    try:
        flow, auth_url, state = await asyncio.to_thread(
            drive_provider.start_oauth_flow, client_config, callback
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    flow_id = _new_flow(
        "drive",
        name=name,
        alias=clean_alias,
        label=label,
        root="",
        flow=flow,
        redirect_uri=callback,
    )
    _register_oauth_state(state, flow_id)
    return JSONResponse(
        {
            "ok": True,
            "flow_id": flow_id,
            "auth_url": str(auth_url),
            "redirect_uri": callback,
        }
    )


def _exchange_drive_code(flow, code: str):
    flow.fetch_token(code=code)
    return flow.credentials


def _complete_drive_flow(flow: dict, credentials) -> accounts.Instance:
    drive_provider.store_drive_credentials(flow["name"], credentials.to_json())
    return accounts.save_instance(
        "drive",
        flow["name"],
        label=(flow.get("label") or None),
        root=(flow.get("root") or None),
        alias=flow.get("alias") or None,
    )


@router.get("/config/oauth/drive/callback")
async def drive_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
) -> HTMLResponse:
    """Google's redirect: exchange the code, store the token, close the tab."""
    flow_id = _take_oauth_state(state)
    flow = _flow_snapshot(flow_id) if flow_id else None
    if flow is None or flow.get("status") != "pending":
        return _callback_page(
            400,
            "This sign-in link is no longer valid",
            "Start the Google Drive sign-in again from the dashboard.",
        )
    if error:
        detail = f"{error}: {error_description}".strip(": ").strip()
        _update_flow(flow_id, status="error", detail=detail)
        return _callback_page(
            400,
            "Google refused the sign-in",
            f"{detail}. Start it again from the dashboard.",
        )
    if not str(code).strip():
        _update_flow(flow_id, status="error", detail="no authorization code returned")
        return _callback_page(
            400, "Google returned no authorization code", "Start the sign-in again."
        )
    try:
        credentials = await asyncio.to_thread(_exchange_drive_code, flow["flow"], code)
        instance = await asyncio.to_thread(_complete_drive_flow, flow, credentials)
    except Exception as failure:  # network/token/store error: friendly, no traceback
        detail = safe_provider_error(failure)
        _update_flow(flow_id, status="error", detail=detail)
        return _callback_page(
            400,
            "The Google Drive sign-in did not finish",
            f"{detail}. Start it again from the dashboard.",
        )
    _update_flow(flow_id, status="complete", method=instance.id)
    return _callback_page(
        200,
        "Google Drive account connected",
        f"{instance.display_name} is ready to use as {instance.id!r}.",
    )


@router.get("/config/accounts/drive/status")
async def drive_status(flow_id: str = "") -> JSONResponse:
    """Poll an OAuth sign-in. 200 with status pending/complete/error."""
    return _flow_response(_flow_snapshot(flow_id))


# --- rename / relabel an existing account ----------------------------------

@router.post("/config/accounts/update")
async def update_account(
    request: Request,
    method: str = Form(...),
    alias: Optional[str] = Form(None),
    label: Optional[str] = Form(None),
) -> JSONResponse:
    """Rename an account's alias and/or change its display label.

    ``alias``/``label`` omitted keeps the current value; an empty ``label``
    clears it. Root, credentials and the default flag are never touched.
    """
    _require_local_origin(request)
    # The parsed form distinguishes an omitted field from an empty one: an
    # omitted label keeps it, an empty label clears it, an empty alias keeps it.
    form = await request.form()
    alias_given = "alias" in form
    label_given = "label" in form
    try:
        provider, name = accounts.parse_method_id(method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    instance = accounts.load_instance(provider, name)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"unknown account: {method}")
    old_alias = instance.id
    new_alias: Optional[str] = None
    if alias_given and str(alias or "").strip():
        try:
            new_alias = accounts.validate_alias(provider, alias, name=name)
        except accounts.AliasCollision as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    new_label: Optional[str] = None
    if label_given:
        new_label = str(form.get("label") or "").strip()
    try:
        saved = await asyncio.to_thread(
            accounts.save_instance,
            provider,
            name,
            alias=new_alias,
            label=new_label,
        )
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if new_alias is not None and saved.id != old_alias:
        await asyncio.to_thread(providers._repoint_upload_method, old_alias, saved.id)
    return JSONResponse(
        {
            "ok": True,
            "method": saved.id,
            "alias": saved.id,
            "label": saved.label,
            "default_method": await asyncio.to_thread(_default_upload_method),
        }
    )
