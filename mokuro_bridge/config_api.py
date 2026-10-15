"""The configuration dashboard and the JSON API behind it.

`GET /` serves one self-contained page (no external assets, no CDN) showing the
bridge's health and editing its upload targets, default folders and settings.
Writes are accepted only from a loopback `Origin`, or from a client that sends
no `Origin` at all (curl, the CLI): the bridge is unauthenticated, so a page in
the user's browser must not be able to repoint an upload destination.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import APP_NAME, __version__, accounts, providers, settings
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

router = APIRouter()
DASHBOARD_HTML = Path(__file__).with_name("web") / "dashboard.html"
_LOCAL_ID = "local"
_MAX_DIRS = 400

_html_cache: Optional[str] = None


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
    global _html_cache
    if _html_cache is None:
        try:
            _html_cache = DASHBOARD_HTML.read_text(encoding="utf-8")
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"dashboard page missing: {error}") from error
    page = _html_cache.replace("{{VERSION}}", __version__).replace("{{APP}}", APP_NAME)
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/config")
async def read_config(request: Request) -> JSONResponse:
    methods = await asyncio.to_thread(_build_upload_methods)
    rows = []
    for method in methods.values():
        instance = providers.instance_for(method.id)
        rows.append(
            {
                "id": method.id,
                "name": method.name,
                "configured": method.configured,
                "default": method.default,
                "provider": (instance.provider if instance else _LOCAL_ID),
                "account": (instance.name if instance else ""),
                "label": (instance.label if instance else ""),
                "root": (instance.root if instance else _method_current_folder(_LOCAL_ID)),
                "creds_source": method.extra.get("creds_source"),
                "current_folder": await asyncio.to_thread(_method_current_folder, method.id),
                "extra": {
                    key: value
                    for key, value in method.extra.items()
                    if key not in ("creds_source", "tracked") and value not in (None, "")
                },
            }
        )
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
            "methods": rows,
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
    if not str(method).strip() or method == _LOCAL_ID:
        raise HTTPException(status_code=400, detail="a provider account is required")
    try:
        result = await asyncio.to_thread(providers.forget_account, method)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return JSONResponse({"ok": True, **result})
