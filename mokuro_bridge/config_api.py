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

from . import APP_NAME, __version__, accounts, rclone_auth, settings
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
from .providers import rclone as rclone_provider
from .providers import webdav as webdav_provider

router = APIRouter()
DASHBOARD_HTML = Path(__file__).with_name("web") / "dashboard.html"
_ENGINE_LOCK = threading.Lock()
# A default change that lands while a Bunko sync is running is remembered and
# followed as soon as that sync finishes, instead of being dropped.
_BUNKO_PENDING = {"method": ""}
_ENGINE_JOBS: dict[str, dict] = {
    "mokuro": {"state": "idle", "code": None, "output": "", "started": 0.0, "finished": 0.0},
    "bunko": {"state": "idle", "code": None, "output": "", "started": 0.0, "finished": 0.0},
}
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
    "rclone": {
        "label": "rclone",
        "browser_setup": "form",
        "setup_hint": "install rclone, run `rclone config`, then add this destination",
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
        rclone_ready = rclone_provider.binary() is not None
        if provider_id in ("mega", "drive", "onedrive", "webdav") and rclone_ready:
            # rclone performs these sign-ins now. The native SDK checks below
            # only matter for legacy accounts, so the cards stay usable.
            entry["available"] = True
            entry["missing"] = []
            entry["rclone_backed"] = True
            entry["service"] = provider_id
            entry["default_root"] = rclone_auth.default_root()
        if provider_id == "mega":
            if not rclone_ready and shutil.which("megatools") is None:
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
            if rclone_ready:
                missing = []
            entry["missing"] = missing
            entry["available"] = rclone_ready or not missing
            entry["client_preset"] = bool(
                drive_provider._drive_client_id_from_env()
                or os.environ.get("DRIVE_CLIENT_SECRET", "").strip()
                or _drive_secret_file_configured()
            )
        elif provider_id == "onedrive":
            # Only a real install problem disables the provider. A missing app
            # ID is something the form can collect, so it must not gray the
            # card out and leave the user with no path forward.
            missing = [
                package
                for package, module in (("msal", "msal"), ("requests", "requests"))
                if not _module_available(module)
            ]
            if rclone_ready:
                missing = []
            entry["missing"] = missing
            entry["available"] = rclone_ready or not missing
            entry["client_id"] = onedrive_provider.client_id()
            entry["client_id_source"] = onedrive_provider.client_id_source()
            entry["client_preset"] = bool(entry["client_id"])
            entry["needs_client_id"] = not entry["client_preset"]
        elif provider_id == "webdav":
            # Any WebDAV server is acceptable, so the only install check is the
            # HTTP client; the server URL is asked for per account.
            missing = [] if (_module_available("requests") or rclone_ready) else ["requests"]
            entry["missing"] = missing
            entry["available"] = not missing
        elif provider_id == "rclone":
            # rclone owns the sign-in, and the page drives it: one shortcut per
            # service, so no terminal `rclone config` step is required.
            installed = rclone_provider.binary() is not None
            entry["missing"] = [] if installed else ["rclone"]
            entry["available"] = installed
            entry["remotes"] = rclone_provider.list_remotes() if installed else []
            entry["browser_setup"] = "shortcuts"
            entry["services"] = rclone_auth.services()
            entry["default_root"] = rclone_auth.default_root()
            entry["recommended"] = True
        # Legacy native accounts stay manageable, but a new setup never needs
        # the old SDKs: the cards above are rclone-backed.
        if provider_id in ("mega", "drive", "onedrive", "webdav"):
            entry["legacy_native"] = not rclone_ready
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
    if instance.provider == "rclone":
        return bool(rclone_provider.remote_for(instance.name))
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
    "rclone": rclone_provider.account_identity,
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
                "tracked": instance is not None,
                # rclone-backed accounts carry the service they configure
                # ("mega", "drive", ...) so the matching card keeps its count.
                "service": (instance.extra.get("kind") if instance is not None else "") or provider_id,
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
    _start_bunko_sync(resolved)
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


def _promote_default_if_local(method_id: str) -> bool:
    """Make the first remote destination the default upload method.

    A fresh install defaults to local; connecting cloud storage should not
    leave the Default badge on "local".
    """
    try:
        if _default_upload_method() == "local":
            _remember_upload_method(method_id)
            return True
    except Exception:
        pass
    return False


def _mega_login_error(detail: str) -> str:
    """Turn megatools' raw login errors into something a person can act on."""
    text = (detail or "").strip()
    low = text.lower()
    if any(key in low for key in ("2fa", "two-factor", "two factor", "mfa", "verification code", "e_mfa")):
        return (
            "MEGA asked for a two-factor code. megatools cannot sign in to "
            "two-factor accounts, so this login was not stored. Disable 2FA "
            "temporarily or use a MEGA account without it; proper 2FA support "
            "is planned with a session-capable backend."
        )
    if any(key in low for key in ("confirm", "verification email", "verify your", "not verified", "email sent")):
        return (
            "MEGA wants this new login confirmed. Check the email MEGA sent, "
            "approve it, then try again."
        )
    if any(key in low for key in ("blocked", "suspended", "disabled", "locked")):
        return "MEGA blocked or locked this account. Sign in at mega.nz to resolve it, then try again."
    if any(key in low for key in ("rate", "too many", "try again later", "temporary")):
        return "MEGA is rate-limiting logins right now. Wait a few minutes and try again."
    if any(key in low for key in ("password", "invalid", "bad credentials", "wrong")):
        return "MEGA rejected the email or password. Check both, then try again."
    return "MEGA login failed - credentials were not stored" + (f": {text}" if text else "")


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
    try:
        duplicates = [
            instance
            for instance in accounts.instances_for("mega")
            if instance.name != name
            and (instance.email or "").strip().lower() == email_value.lower()
        ]
    except Exception:
        duplicates = []
    if duplicates:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This MEGA email is already connected as {duplicates[0].id}. "
                "Reconnect or remove that account instead of adding it twice."
            ),
        )
    # The default account can be configured through the old keychain path with
    # no account file, so it has no recorded email to compare against.
    if name != accounts.DEFAULT_NAME and not accounts.load_instance("mega", accounts.DEFAULT_NAME).tracked:
        try:
            legacy_email = mega_provider._keychain_mega_account() or ""
        except Exception:
            legacy_email = ""
        if legacy_email.strip().lower() == email_value.lower():
            raise HTTPException(
                status_code=400,
                detail=(
                    "This MEGA email is already set up as the first MEGA account "
                    "(found in Keychain). Remove that account first, then add it "
                    "under this name."
                ),
            )
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
        raise HTTPException(status_code=400, detail=_mega_login_error(detail))
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
    promoted = _promote_default_if_local(instance.id)
    return JSONResponse({"ok": True, "method": instance.id, "root": instance.root_path, "made_default": promoted})


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
    insecure: str = Form(""),
) -> JSONResponse:
    """Verify and store one WebDAV account (server URL + Basic credentials).

    The server is contacted once before anything is written, so a typo in the
    URL or the password is a 400 carrying a plain-language explanation, not an
    account that silently fails every later upload. ``discover`` fallbacks are
    tried, so pasting the server's web address (rather than its WebDAV path)
    still works, and the answer reports the address that actually answered.
    Re-submitting an existing alias re-credentials it in place, which is the
    page's Reconnect button.
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
    url_value = webdav_provider.normalise_server_address(str(url or "").strip())
    username_value = str(username or "").strip()
    password_value = str(password or "")
    insecure = str(insecure or "").strip().lower() in ("1", "true", "yes", "on")
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
        ok, verify_error, resolved = await asyncio.to_thread(
            webdav_provider.verify_webdav_creds,
            url_value,
            username_value,
            password_value,
            insecure,
            True,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not ok:
        detail = safe_provider_error(verify_error or "").strip()
        raise HTTPException(
            status_code=400,
            detail=(
                "WebDAV connection failed - nothing was stored."
                + (f" {detail}" if detail else "")
            ),
        )
    try:
        instance = await asyncio.to_thread(
            webdav_provider.store_webdav_account,
            name,
            resolved or url_value,
            username_value,
            password_value,
            alias=clean_alias,
            label=(label.strip() or None),
            root=(root.strip() or None),
            insecure=insecure,
        )
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # Create the upload folder now, so the page's Change folder shows a real
    # folder and a bad root path surfaces while the user is still on the form.
    folder_ready = True
    try:
        await asyncio.to_thread(webdav_provider.ensure_account_root, name)
    except Exception:
        folder_ready = False
    server = webdav_provider.base_url(instance.name)
    try:
        entered = webdav_provider.clean_base_url(url_value)
    except ValueError:
        entered = ""
    promoted = _promote_default_if_local(instance.id)
    return JSONResponse(
        {
            "ok": True,
            "method": instance.id,
            "root": instance.root_path,
            "made_default": promoted,
            "server": server,
            "username": webdav_provider.username(instance.name),
            "discovered": bool(entered and server.rstrip("/") != entered.rstrip("/")),
            "insecure": insecure,
            "folder_ready": folder_ready,
        }
    )


# --- adding an account: rclone (a remote from rclone's own config) ---------

def _run_bunko_sync_job(method: str) -> None:
    import contextlib
    import io

    def stream(message: str) -> None:
        line = str(message).rstrip()
        if not line:
            return
        with _ENGINE_LOCK:
            current = _ENGINE_JOBS["bunko"].get("output", "")
            _ENGINE_JOBS["bunko"]["output"] = (current + "\n" + line)[-6000:]

    buffer = io.StringIO()
    code = 1
    try:
        from . import bunko_agent

        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            bunko_agent.repoint(method, log=stream)
        code = 0
    except Exception as error:
        stream(f"{type(error).__name__}: {error}")
        code = 1
    finally:
        tail = buffer.getvalue().strip()
        if tail:
            stream(tail)
        with _ENGINE_LOCK:
            _ENGINE_JOBS["bunko"].update(
                state="done" if code == 0 else "error",
                code=code,
                finished=time.time(),
            )
            pending = _BUNKO_PENDING["method"]
            _BUNKO_PENDING["method"] = ""
        if code == 0:
            # Follow a default that changed while this sync was running.
            _start_bunko_sync(pending or None)


def _start_bunko_sync(method: Optional[str] = None) -> bool:
    """Follow a destination change automatically, in the background."""
    from . import bunko_agent

    try:
        info = bunko_agent.sync_info()
    except Exception:
        return False
    if not info.get("installed") or info.get("in_sync"):
        return False
    target = str(method or info.get("default_method") or "")
    if target in ("", "local"):
        return False
    with _ENGINE_LOCK:
        if _ENGINE_JOBS["bunko"]["state"] == "running":
            _BUNKO_PENDING["method"] = target
            return False
        _ENGINE_JOBS["bunko"].update(
            state="running", code=None, output="", started=time.time(), finished=0.0
        )
    threading.Thread(
        target=_run_bunko_sync_job, args=(target,), name="bunko-auto-sync", daemon=True
    ).start()
    return True


@router.post("/config/bunko/sync")
async def sync_bunko(request: Request) -> JSONResponse:
    """Re-point the running Bunko library at the bridge's current default."""
    _require_local_origin(request)
    from . import bunko_agent

    info = await asyncio.to_thread(bunko_agent.sync_info)
    if not info.get("installed"):
        raise HTTPException(status_code=400, detail="set up mokuro-bunko first")
    if info.get("default_method") in ("", "local"):
        raise HTTPException(status_code=400, detail="make a cloud destination the default first")
    if info.get("in_sync"):
        raise HTTPException(status_code=409, detail="mokuro-bunko already follows the default destination")
    with _ENGINE_LOCK:
        if _ENGINE_JOBS["bunko"]["state"] == "running":
            raise HTTPException(status_code=409, detail="a mokuro-bunko job is already running")
        _ENGINE_JOBS["bunko"].update(
            state="running", code=None, output="", started=time.time(), finished=0.0
        )
    threading.Thread(
        target=_run_bunko_sync_job, args=(info["default_method"],), name="bunko-sync", daemon=True
    ).start()
    return JSONResponse(
        {
            "ok": True,
            "state": "running",
            "method": info["default_method"],
            "needs_sign_in": info.get("needs_sign_in", False),
        },
        status_code=202,
    )


_RCLONE_LIBS_HINT = (
    "rclone destinations need the rclone command-line tool. Install it with "
    "`brew install rclone` (macOS), `winget install Rclone.Rclone` (Windows) or "
    "`apt install rclone` (Debian/Ubuntu), then run `rclone config` and retry."
)


@router.post("/config/accounts/rclone")
async def add_rclone_account(
    request: Request,
    alias: str = Form(""),
    label: str = Form(""),
    remote: str = Form(""),
    root: str = Form(""),
) -> JSONResponse:
    """Add (or re-point) one rclone destination.

    rclone performs the sign-in itself and keeps the token in its own config,
    so the page only has to name an existing remote. Choosing an alias that
    already exists re-points that account at another remote and keeps its
    folder, note and default.
    """
    _require_local_origin(request)
    if rclone_provider.binary() is None:
        raise HTTPException(status_code=400, detail=_RCLONE_LIBS_HINT)
    try:
        name, clean_alias = _account_target("rclone", alias)
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    remote_value = str(remote or "").strip()
    if not remote_value and accounts.load_instance("rclone", name) is not None:
        remote_value = rclone_provider.remote_for(name)
    remotes = await asyncio.to_thread(rclone_provider.list_remotes, refresh=True)
    if not remote_value:
        available = ", ".join(remotes) or "none"
        raise HTTPException(
            status_code=400,
            detail=(
                "Choose an rclone remote (configured: "
                f"{available}). Run `rclone config` in a terminal to add one; "
                "for OneDrive pick Microsoft OneDrive and leave the client ID "
                "blank, rclone has its own app registration."
            ),
        )
    remote_value = remote_value if remote_value.endswith(":") else f"{remote_value}:"
    if remote_value not in remotes:
        available = ", ".join(remotes) or "none"
        raise HTTPException(
            status_code=400,
            detail=f"rclone has no remote named {remote_value} (configured: {available})",
        )
    ok, check_error = await asyncio.to_thread(rclone_provider.check_remote, remote_value)
    if not ok:
        detail = safe_provider_error(check_error or "").strip()
        raise HTTPException(
            status_code=400,
            detail=f"rclone remote could not be used - nothing was stored. {detail}",
        )
    try:
        instance = await asyncio.to_thread(
            accounts.save_instance,
            "rclone",
            name,
            label=(label.strip() or None),
            root=(root.strip() or None),
            alias=clean_alias,
            extra={"remote": remote_value},
        )
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    folder_ready = True
    try:
        await asyncio.to_thread(rclone_provider.ensure_account_root, name)
    except Exception:
        folder_ready = False
    return JSONResponse(
        {
            "ok": True,
            "method": instance.id,
            "remote": remote_value,
            "root": instance.root_path,
            "folder_ready": folder_ready,
            "remotes": remotes,
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
        _promote_default_if_local(instance.id)
        _update_flow(flow_id, status="complete", method=instance.id)


@router.post("/config/accounts/onedrive/start")
async def start_onedrive_account(
    request: Request,
    alias: str = Form(""),
    label: str = Form(""),
    client_id: str = Form(""),
) -> JSONResponse:
    """Begin a device-code sign-in for one OneDrive account.

    The one-time Azure app ID can be posted with the sign-in, so the page can
    collect it instead of asking the user to edit an environment variable and
    restart the service. It is stored in ONEDRIVE_APP_FILE (0600).
    """
    _require_local_origin(request)
    if not _module_available("msal"):
        raise HTTPException(status_code=400, detail=_MSAL_HINT)
    try:
        name, clean_alias = _account_target("onedrive", alias)
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    posted = str(client_id or "").strip()
    pinned = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip()
    if posted and pinned and posted != pinned:
        raise HTTPException(
            status_code=400,
            detail=(
                "ONEDRIVE_CLIENT_ID is set in the bridge's environment, so an app "
                "ID entered here would be ignored; unset it in the service "
                "environment to manage the app ID from this page"
            ),
        )
    if posted:
        try:
            posted = onedrive_provider.validate_client_id(posted)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    if not (posted or onedrive_provider.client_id()):
        raise HTTPException(
            status_code=400,
            detail=(
                "OneDrive needs a free Azure application (client) ID once. Open "
                "https://portal.azure.com, go to App registrations and create one "
                "that allows personal Microsoft accounts; set Allow public client "
                "flows to Yes, add the delegated Microsoft Graph permission "
                "Files.ReadWrite, then paste the Application (client) ID here. "
                "No redirect URI or secret is needed."
            ),
        )
    try:
        app, cache, flow = await asyncio.to_thread(
            onedrive_provider.start_device_flow, name, posted or None
        )
    except ValueError as error:
        detail = onedrive_provider.explain_aadsts(error) or str(error)
        raise HTTPException(status_code=400, detail=detail) from error
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # Microsoft accepted the app, so it is safe to keep it for later. A failed
    # write only means the user gets asked again next time.
    client_id_saved = False
    if posted and not pinned:
        try:
            await asyncio.to_thread(onedrive_provider.store_client_id, posted)
            client_id_saved = True
        except (ValueError, OSError):
            client_id_saved = False
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
            "client_id": posted or onedrive_provider.client_id(),
            "client_id_source": onedrive_provider.client_id_source(),
            "client_id_saved": client_id_saved,
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
    instance = accounts.save_instance(
        "drive",
        flow["name"],
        label=(flow.get("label") or None),
        root=(flow.get("root") or None),
        alias=flow.get("alias") or None,
    )
    _promote_default_if_local(instance.id)
    return instance


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
            new_alias = accounts.validate_alias(
                provider, alias, name=name, allow_reserved=provider == "rclone"
            )
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
            allow_reserved=provider == "rclone",
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


def _mokuro_engine_status() -> dict:
    try:
        from . import mokuro_flavor

        info = mokuro_flavor.detect(refresh=True)
    except Exception as error:  # detection itself never raises, but be safe
        return {"installed": False, "version": None, "flavour": "unknown", "detail": str(error)}
    flavour = info.get("flavor") or "unknown"
    if flavour == "fork":
        detail = "GolyBidoof fork"
    elif flavour == "upstream":
        detail = "upstream build"
    elif info.get("installed"):
        detail = flavour
    else:
        detail = "not installed"
    return {
        "installed": bool(info.get("installed")),
        "version": info.get("version"),
        "flavour": flavour,
        "detail": detail,
        "can_install": flavour != "fork",
    }


def _bunko_destination() -> tuple[Optional[str], Optional[str]]:
    """(method id, error) for the destination Bunko should manage.

    Bunko needs a cloud folder. Prefer the configured default, otherwise the
    first connected remote account, so a fresh setup does not build for minutes
    and then discover there is nothing to connect to.
    """
    methods = _method_rows()
    remote = [
        row for row in methods
        if row.get("provider") != _LOCAL_ID and row.get("state") != "not_configured"
    ]
    if not remote:
        return None, "add a MEGA, Drive, OneDrive or WebDAV destination first"
    default = [row for row in remote if row.get("default")]
    chosen = (default or remote)[0]
    return chosen["id"], None


def _bunko_engine_status() -> dict:
    try:
        from . import bunko_agent
    except Exception:
        return {
            "available": False,
            "installed": False,
            "version": None,
            "detail": "mokuro-bunko support is not part of this build yet",
            "can_install": False,
        }
    method, method_error = _bunko_destination()
    prerequisites = {
        "git": bool(shutil.which("git")),
        "cargo": bool(shutil.which("cargo")),
    }
    missing = [name for name, present in prerequisites.items() if not present]
    try:
        state = bunko_agent.load_state()
        installed = state is not None
        detail = bunko_agent.status_line(state)
        can_install = not installed and not missing and method_error is None
        if missing:
            detail = f"Needs {', '.join(missing)} installed first."
        elif method_error is not None:
            detail = "Add a cloud destination first; Bunko reads the folder the bridge uploads to."
        return {
            "available": True,
            "installed": installed,
            "version": getattr(state, "commit", None) if state else None,
            "detail": detail,
            "can_install": can_install,
            "method": method,
            "prerequisites": prerequisites,
            "sync": bunko_agent.sync_info(),
            "update_available": bunko_agent.update_available(),
            "latest_commit": bunko_agent.latest_commit(),
        }
    except Exception as error:
        return {"available": True, "installed": False, "version": None, "detail": str(error), "can_install": True}


def _engine_job_json(name: str) -> dict:
    with _ENGINE_LOCK:
        job = dict(_ENGINE_JOBS[name])
    if job["state"] == "running":
        job["elapsed_s"] = max(0, round(time.time() - job["started"], 1))
    return job


def _run_mokuro_install_job() -> None:
    import contextlib
    import io

    def stream(message: str) -> None:
        line = str(message).rstrip()
        if not line:
            return
        with _ENGINE_LOCK:
            current = _ENGINE_JOBS["mokuro"].get("output", "")
            _ENGINE_JOBS["mokuro"]["output"] = (current + "\n" + line)[-6000:]

    buffer = io.StringIO()
    code = 1
    try:
        from .cli import _run_install_mokuro

        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = _run_install_mokuro()
    except Exception as error:
        buffer.write(f"{type(error).__name__}: {error}\n")
        code = 1
    finally:
        tail = buffer.getvalue().strip()
        if tail:
            stream(tail)
        with _ENGINE_LOCK:
            _ENGINE_JOBS["mokuro"].update(
                state="done" if code == 0 else "error",
                code=code,
                finished=time.time(),
            )


def _run_bunko_install_job(method: Optional[str], totp: Optional[str]) -> None:
    import contextlib
    import io

    def stream(message: str) -> None:
        line = str(message).rstrip()
        if not line:
            return
        with _ENGINE_LOCK:
            current = _ENGINE_JOBS["bunko"].get("output", "")
            _ENGINE_JOBS["bunko"]["output"] = (current + "\n" + line)[-6000:]

    buffer = io.StringIO()
    code = 1
    stream("installing mokuro-bunko (a first install compiles from source, which takes a few minutes)")
    try:
        from . import bunko_agent

        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            bunko_agent.setup(method, log=stream, totp=totp)
        code = 0
    except Exception as error:
        stream(f"{type(error).__name__}: {error}")
        code = 1
    finally:
        tail = buffer.getvalue().strip()
        if tail:
            stream(tail)
        with _ENGINE_LOCK:
            _ENGINE_JOBS["bunko"].update(
                state="done" if code == 0 else "error",
                code=code,
                finished=time.time(),
            )


_BUNKO_RESTART_LOCK = threading.Lock()
_BUNKO_RESTART_TS = {"at": 0.0}


def _ensure_bunko_running() -> None:
    """Bring an installed-but-stopped Bunko back up, at most every 15 seconds."""
    from . import bunko_agent

    now = time.time()
    with _BUNKO_RESTART_LOCK:
        if now - _BUNKO_RESTART_TS["at"] < 15.0:
            return
        _BUNKO_RESTART_TS["at"] = now

    def run() -> None:
        try:
            bunko_agent.ensure_running(log=lambda message: None)
        except Exception:
            pass

    threading.Thread(target=run, name="bunko-restart", daemon=True).start()


@router.get("/config/engines")
async def read_engines() -> JSONResponse:
    # Installed engines change at runtime, and a cached "not installed" answer
    # left the dashboard red until a hard reload.
    mokuro_status = await asyncio.to_thread(_mokuro_engine_status)
    bunko_status = await asyncio.to_thread(_bunko_engine_status)
    if (
        bunko_status.get("installed")
        and not (bunko_status.get("sync") or {}).get("running")
        and _ENGINE_JOBS["bunko"]["state"] == "idle"
    ):
        _ensure_bunko_running()
    return JSONResponse(
        headers={"Cache-Control": "no-store, max-age=0"},
        content={
            "mokuro": mokuro_status,
            "bunko": bunko_status,
            "jobs": {
                "mokuro": _engine_job_json("mokuro"),
                "bunko": _engine_job_json("bunko"),
            },
        },
    )


def _start_engine_job(name: str, runner, *args) -> None:
    with _ENGINE_LOCK:
        _ENGINE_JOBS[name].update(
            state="running", code=None, output="", started=time.time(), finished=0.0
        )
    threading.Thread(target=runner, args=args, name=f"install-{name}", daemon=True).start()


def _run_bunko_update_job() -> None:
    import contextlib
    import io

    def stream(message: str) -> None:
        line = str(message).rstrip()
        if not line:
            return
        with _ENGINE_LOCK:
            current = _ENGINE_JOBS["bunko"].get("output", "")
            _ENGINE_JOBS["bunko"]["output"] = (current + "\n" + line)[-6000:]

    code = 1
    try:
        from . import bunko_agent

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bunko_agent.update(log=stream)
        code = 0
    except Exception as error:
        stream(f"{type(error).__name__}: {error}")
    with _ENGINE_LOCK:
        _ENGINE_JOBS["bunko"].update(
            state="done" if code == 0 else "error", code=code, finished=time.time()
        )


@router.post("/config/engines/bunko/update")
async def update_bunko_engine(request: Request) -> JSONResponse:
    """Rebuild Bunko at the newest development commit."""
    _require_local_origin(request)
    with _ENGINE_LOCK:
        if _ENGINE_JOBS["bunko"]["state"] == "running":
            raise HTTPException(status_code=409, detail="a mokuro-bunko job is already running")
        _ENGINE_JOBS["bunko"].update(
            state="running", code=None, output="", started=time.time(), finished=0.0
        )
    threading.Thread(target=_run_bunko_update_job, name="bunko-update", daemon=True).start()
    return JSONResponse({"ok": True, "state": "running"}, status_code=202)


@router.post("/config/update/apply")
async def apply_bridge_update(request: Request) -> JSONResponse:
    """Run the bridge's own updater; the process must be restarted afterwards."""
    _require_local_origin(request)
    import sys as _sys

    try:
        result = await asyncio.to_thread(
            lambda: __import__("subprocess").run(
                [_sys.executable, "-m", "mokuro_bridge", "--update"],
                capture_output=True, text=True, timeout=300,
            )
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    output = ((result.stdout or "") + (result.stderr or ""))[-4000:]
    return JSONResponse(
        {
            "ok": result.returncode == 0,
            "output": output,
            "restart_required": result.returncode == 0,
        }
    )


@router.post("/config/update/check")
async def check_bridge_update(request: Request) -> JSONResponse:
    """Force a fresh release check and return the same fields /health shows."""
    _require_local_origin(request)
    from . import update

    result = await asyncio.to_thread(update.refresh, force=True)
    return JSONResponse({"ok": True, **result})


@router.post("/config/engines/mokuro/uninstall")
async def uninstall_mokuro_engine(request: Request) -> JSONResponse:
    """Remove the OCR engine from this interpreter and unload it."""
    _require_local_origin(request)
    import contextlib
    import io
    import sys as _sys

    from . import api as api_module
    from . import ocr

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        result = await asyncio.to_thread(
            lambda: __import__("subprocess").run(
                [_sys.executable, "-m", "pip", "uninstall", "-y", "mokuro"],
                capture_output=True, text=True,
            )
        )
        buffer.write(result.stdout or "")
        buffer.write(result.stderr or "")
        await asyncio.to_thread(ocr.reload_engine)
    api_module._mokuro_pkg = ocr._mokuro_pkg
    api_module._MOKURO_REPO = ocr._MOKURO_REPO
    return JSONResponse(
        {"ok": result.returncode == 0, "output": buffer.getvalue()[-2000:]}
    )


@router.post("/config/engines/bunko/uninstall")
async def uninstall_bunko_engine(request: Request) -> JSONResponse:
    """Stop Bunko and delete its binary, source, config and state."""
    _require_local_origin(request)
    from . import bunko_agent

    buffer: list[str] = []
    removed = await asyncio.to_thread(bunko_agent.uninstall, log=buffer.append)
    with _ENGINE_LOCK:
        _ENGINE_JOBS["bunko"].update(
            state="idle", code=None, output="\n".join(buffer)[-2000:], started=0.0, finished=time.time()
        )
    return JSONResponse({"ok": True, "removed": removed, "output": "\n".join(buffer)[-2000:]})


@router.post("/config/engines/mokuro/reload")
async def reload_mokuro_engine(request: Request) -> JSONResponse:
    """Load a freshly installed engine into the running bridge."""
    _require_local_origin(request)
    from . import api as api_module
    from . import ocr

    info = await asyncio.to_thread(ocr.reload_engine)
    api_module._mokuro_pkg = ocr._mokuro_pkg
    api_module._MOKURO_REPO = ocr._MOKURO_REPO
    if not info.get("installed"):
        raise HTTPException(
            status_code=400,
            detail=f"the engine is installed but could not be imported: {info.get('error', 'unknown error')}",
        )
    return JSONResponse({"ok": True, **info})


@router.post("/config/engines/mokuro/install")
async def install_mokuro_engine(request: Request) -> JSONResponse:
    _require_local_origin(request)
    status = await asyncio.to_thread(_mokuro_engine_status)
    if status.get("flavour") == "fork":
        raise HTTPException(status_code=409, detail="the fast mokuro fork is already installed")
    with _ENGINE_LOCK:
        if _ENGINE_JOBS["mokuro"]["state"] == "running":
            raise HTTPException(status_code=409, detail="a mokuro install is already running")
    _start_engine_job("mokuro", _run_mokuro_install_job)
    return JSONResponse({"ok": True, "state": "running"}, status_code=202)


@router.post("/config/engines/bunko/install")
async def install_bunko_engine(request: Request, totp: str = Form("")) -> JSONResponse:
    _require_local_origin(request)
    status = await asyncio.to_thread(_bunko_engine_status)
    if not status.get("available"):
        raise HTTPException(status_code=400, detail=status.get("detail") or "mokuro-bunko is not available")
    if status.get("installed"):
        raise HTTPException(status_code=409, detail="mokuro-bunko is already configured")
    method, method_error = await asyncio.to_thread(_bunko_destination)
    if method_error:
        raise HTTPException(status_code=400, detail=method_error)
    missing = [name for name, present in (status.get("prerequisites") or {}).items() if not present]
    if missing:
        raise HTTPException(status_code=400, detail=f"install {', '.join(missing)} first")
    with _ENGINE_LOCK:
        if _ENGINE_JOBS["bunko"]["state"] == "running":
            raise HTTPException(status_code=409, detail="a mokuro-bunko install is already running")
    code = (totp or "").strip() or None
    _start_engine_job("bunko", _run_bunko_install_job, method, code)
    return JSONResponse({"ok": True, "state": "running"}, status_code=202)


# --- rclone shortcuts: configure MEGA, Drive, OneDrive and WebDAV from here --

_RCLONE_SERVICE_ALIASES = ("mega", "drive", "onedrive", "webdav")


def _rclone_account_target(alias: str, replace: bool = False) -> tuple[str, str]:
    """(storage name, alias) for a bridge-created rclone account.

    The four service cards keep the names users already know. Those names are
    also the legacy native provider ids, so they are allowed only while no
    native account of that provider is configured.
    """
    candidate = str(alias or "").strip().lower()
    try:
        return _account_target("rclone", alias)
    except (accounts.AliasCollision, ValueError) as error:
        if candidate not in _RCLONE_SERVICE_ALIASES:
            raise
        conflict = accounts.find_alias(candidate)
        if conflict is not None and conflict.tracked and conflict.provider != "rclone":
            if not replace:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"the name '{candidate}' is already used by "
                        f"{conflict.display_name}. Confirm to move that name to "
                        "this new account; the old one keeps working under its "
                        "generated id."
                    ),
                ) from error
            accounts.clear_alias(conflict.provider, conflict.name)
        elif conflict is not None and conflict.tracked:
            raise error
        return candidate, candidate


def _rclone_save_account(
    remote: str, alias: str, label: str, root: str, extra: dict, replace: bool = False
) -> accounts.Instance:
    """Record one bridge account for a remote the page just created."""
    try:
        name, clean_alias = _rclone_account_target(alias, replace)
    except accounts.AliasCollision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    service = str(extra.pop("kind", "") or "")
    merged = {
        "remote": remote if remote.endswith(":") else f"{remote}:",
        # "kind" is what the page groups the account under, so the four cards
        # keep their own counts after the rclone migration.
        "kind": service,
        "provider_kind": service,
    }
    merged.update(extra)
    try:
        instance = accounts.save_instance(
            "rclone",
            name,
            label=(label.strip() or None),
            root=(root.strip() or rclone_auth.default_root()),
            alias=clean_alias,
            extra=merged,
            # The service cards keep their familiar ids ("mega", "webdav");
            # _rclone_account_target already proved no native account uses them.
            allow_reserved=clean_alias in _RCLONE_SERVICE_ALIASES,
        )
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail=(
                "the account file could not be written "
                f"({accounts.accounts_dir()}): {error}. Check that the folder "
                "exists and this user can write to it."
            ),
        ) from error
    if _promote_default_if_local(instance.id):
        _start_bunko_sync(instance.id)
    # Start the shared gateway now so the first upload does not pay for it.
    try:
        from . import rclone_gateway

        rclone_gateway.ensure(instance.name)
    except Exception:
        pass
    return instance


@router.get("/config/rclone/services")
async def rclone_services() -> JSONResponse:
    return JSONResponse(
        {
            "available": rclone_auth.binary() is not None,
            "services": rclone_auth.services(),
            "default_root": rclone_auth.default_root(),
        }
    )


@router.post("/config/rclone/mega")
async def rclone_add_mega(
    request: Request,
    alias: str = Form(""),
    label: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    totp: str = Form(""),
    root: str = Form(""),
    replace: str = Form(""),
) -> JSONResponse:
    """Create a MEGA remote through rclone, then record the destination."""
    _require_local_origin(request)
    try:
        remote, extra = await asyncio.to_thread(
            rclone_auth.create_mega, alias or "mega", email, password, totp, check=False
        )
    except rclone_auth.RcloneAuthError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    extra["kind"] = "mega"
    instance = await asyncio.to_thread(
        _rclone_save_account, remote, alias, label, root, extra, replace in ("1", "true", "yes")
    )
    verify_id = await asyncio.to_thread(rclone_auth.start_verify, remote, instance.name)
    return JSONResponse(
        {
            "ok": True,
            "method": instance.id,
            "remote": instance.extra.get("remote"),
            "identity": rclone_auth.remote_summary(str(instance.extra.get("remote") or "")),
            "verify_id": verify_id,
        }
    )


@router.post("/config/rclone/webdav")
async def rclone_add_webdav(
    request: Request,
    alias: str = Form(""),
    label: str = Form(""),
    url: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    root: str = Form(""),
    replace: str = Form(""),
) -> JSONResponse:
    """Create a WebDAV remote through rclone, then record the destination."""
    _require_local_origin(request)
    try:
        remote, extra = await asyncio.to_thread(
            rclone_auth.create_webdav, alias or "webdav", url, username, password, check=False
        )
    except rclone_auth.RcloneAuthError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    extra["kind"] = "webdav"
    instance = await asyncio.to_thread(
        _rclone_save_account, remote, alias, label, root, extra, replace in ("1", "true", "yes")
    )
    verify_id = await asyncio.to_thread(rclone_auth.start_verify, remote, instance.name)
    return JSONResponse(
        {
            "ok": True,
            "method": instance.id,
            "remote": instance.extra.get("remote"),
            "identity": rclone_auth.remote_summary(str(instance.extra.get("remote") or "")),
            "verify_id": verify_id,
        }
    )


_LOCAL_USAGE_TTL_S = 300.0
_local_usage_cache: dict[str, tuple[float, dict]] = {}


def _local_usage(path: str, *, refresh: bool = False) -> dict:
    """Disk space and folder size for a local destination (cached briefly)."""
    target = Path(path).expanduser()
    if not target.exists() or not target.is_dir():
        return {"available": False, "error": "the local folder does not exist yet"}
    now = time.monotonic()
    key = str(target)
    cached = _local_usage_cache.get(key)
    if cached is not None and not refresh and now - cached[0] < _LOCAL_USAGE_TTL_S:
        return dict(cached[1])
    disk = shutil.disk_usage(str(target))
    bytes_total = 0
    count = 0
    errors = 0
    for root, _dirs, files in os.walk(target):
        for name in files:
            try:
                stat_result = os.stat(os.path.join(root, name), follow_symlinks=False)
            except OSError:
                errors += 1
                continue
            bytes_total += stat_result.st_size
            count += 1
    data = {
        "available": True,
        "about": {"total": disk.total, "used": disk.used, "free": disk.free},
        "about_error": "",
        "folder": {"bytes": bytes_total, "count": count, "errors": errors},
        "folder_error": "",
        "checked_at": time.time(),
    }
    _local_usage_cache[key] = (now, data)
    return dict(data)


@router.get("/config/folder-link")
async def read_folder_link(method: str = "") -> JSONResponse:
    """A deep link to this destination's upload folder, when one exists."""
    if not method:
        raise HTTPException(status_code=400, detail="a destination is required")
    instance = providers.instance_for(method)
    if instance is None:
        raise HTTPException(status_code=404, detail="unknown destination")
    if instance.provider != "rclone":
        return JSONResponse({"url": ""})
    from . import rclone_auth

    url = await asyncio.to_thread(
        rclone_auth.folder_link, instance.name, str(instance.root or "")
    )
    return JSONResponse({"url": str(url or "")})


@router.get("/config/account-email")
async def read_account_email(method: str = "") -> JSONResponse:
    """Resolve the address behind one account, only when Details asks for it."""
    if not method:
        raise HTTPException(status_code=400, detail="a destination is required")
    instance = providers.instance_for(method)
    if instance is None:
        raise HTTPException(status_code=404, detail="unknown destination")
    if instance.provider == "rclone":
        email = await asyncio.to_thread(rclone_provider.email_identity, instance)
    else:
        email = await asyncio.to_thread(_account_identity, instance)
    return JSONResponse({"email": str(email or "").strip()})


@router.get("/config/usage")
async def read_usage(method: str = "", refresh: str = "") -> JSONResponse:
    """Space details for any destination: local disk or an rclone remote."""
    if not method or method == "local":
        path = str(OUTPUT_DIR)
        try:
            config = _build_upload_methods()
            local = config.get("local")
            if local is not None and getattr(local, "root", ""):
                path = str(local.root)
        except Exception:
            pass
        data = await asyncio.to_thread(
            _local_usage, path, refresh=refresh in ("1", "true", "yes")
        )
        return JSONResponse(data)
    try:
        provider, name = accounts.parse_method_id(method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if provider != "rclone":
        raise HTTPException(status_code=400, detail="space details are only available for local and rclone destinations")
    data = await asyncio.to_thread(
        rclone_provider.usage, name, refresh=refresh in ("1", "true", "yes")
    )
    return JSONResponse(data)


@router.get("/config/rclone/usage")
async def rclone_usage(method: str = "", refresh: str = "") -> JSONResponse:
    """Quota and upload-folder size for one rclone destination (cached)."""
    try:
        provider, name = accounts.parse_method_id(method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if provider != "rclone":
        raise HTTPException(status_code=400, detail="space details are only available for rclone destinations")
    data = await asyncio.to_thread(
        rclone_provider.usage, name, refresh=refresh in ("1", "true", "yes")
    )
    return JSONResponse(data)


@router.get("/config/rclone/verify/status")
async def rclone_verify_status(flow_id: str = "") -> JSONResponse:
    """Progress of a background sign-in check after a MEGA/WebDAV card."""
    try:
        return JSONResponse(await asyncio.to_thread(rclone_auth.verify_status, flow_id))
    except rclone_auth.RcloneAuthError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/config/rclone/authorize/start")
async def rclone_authorize_start(
    request: Request,
    service: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
) -> JSONResponse:
    """Open the consent tab for Drive or OneDrive through rclone.

    Drive accepts an optional client id and secret when the user wants their
    own Google project instead of rclone's built-in client.
    """
    _require_local_origin(request)
    kind = str(service or "").strip().lower()
    try:
        flow = await asyncio.to_thread(
            rclone_auth.start_authorize, kind, client_id, client_secret
        )
    except rclone_auth.RcloneAuthError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse({"ok": True, **flow})


@router.get("/config/rclone/authorize/status")
async def rclone_authorize_status(flow_id: str = "") -> JSONResponse:
    try:
        return JSONResponse(await asyncio.to_thread(rclone_auth.authorize_status, flow_id))
    except rclone_auth.RcloneAuthError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/config/rclone/authorize/finish")
async def rclone_authorize_finish(
    request: Request,
    flow_id: str = Form(""),
    alias: str = Form(""),
    label: str = Form(""),
    root: str = Form(""),
) -> JSONResponse:
    """Turn a finished consent flow into a destination."""
    _require_local_origin(request)
    try:
        remote, extra = await asyncio.to_thread(
            rclone_auth.finish_authorize, flow_id, alias or ""
        )
    except rclone_auth.RcloneAuthError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    extra["kind"] = "drive" if alias.startswith("drive") else "onedrive"
    instance = await asyncio.to_thread(_rclone_save_account, remote, alias, label, root, extra)
    return JSONResponse(
        {
            "ok": True,
            "method": instance.id,
            "remote": instance.extra.get("remote"),
            "identity": rclone_auth.remote_summary(str(instance.extra.get("remote") or "")),
        }
    )
