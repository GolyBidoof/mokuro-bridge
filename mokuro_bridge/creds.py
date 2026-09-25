from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

from . import accounts
from .config import MEGA_CREDS_FILE
from .util import _chmod_fd_private

# ── MEGA / helpers ─────────────────────────────────────────────────────

# ── MEGA credentials ──────────────────────────────────────────────────
# Resolution order for the *default* account:
#   1. MEGA_EMAIL / MEGA_PASSWORD env vars
#   2. MEGA_CREDS_FILE (KEY=VALUE, chmod 600)
#   3. the OS credential store, when one is available:
#        macOS  — Keychain (via `security`, or `keyring`)
#        Windows— Credential Manager (via `keyring`)
#        Linux  — Secret Service / gnome-keyring (via `keyring`)
# Additional accounts (mega:work, …) read their password from the OS store
# scoped to the email recorded in their account file (accounts.py), with a
# 0600 per-account file as the fallback when no OS store is available.
# All optional: MEGA upload is disabled by default and can be skipped entirely.

_KEYRING_SERVICE = "mokuro-bridge"  # namespace used for keyring-based entries
_CREDS_WRITE_LOCK = threading.Lock()

def _keyring():
    """Best-effort import of the optional `keyring` package.

    Returns the module when a usable backend is configured, else None (callers
    then fall back to the credentials file / macOS `security`).
    """
    try:
        import keyring

        keyring.get_keyring()  # raises if no backend is available
        return keyring
    except Exception:
        return None

def _dedupe(values: Iterable[str]) -> list[str]:
    """Non-empty, stripped, order-preserving unique strings."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out

def _read_creds_file(path: Optional[Path] = None) -> Optional[tuple[str, str]]:
    path = path or MEGA_CREDS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    email = password = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip().upper() == "MEGA_EMAIL":
            email = value
        elif key.strip().upper() == "MEGA_PASSWORD":
            password = value
    if email and password:
        return email, password
    return None

def _write_creds_file(
    email: str, password: str, path: Optional[Path] = None
) -> None:
    path = path or MEGA_CREDS_FILE
    if any("\n" in str(value) or "\r" in str(value) for value in (email, password)):
        raise ValueError("MEGA credentials must not contain newlines")
    with _CREDS_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            _chmod_fd_private(fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(
                    f"# mokuro-bridge MEGA credentials — keep this file private.\n"
                    f"MEGA_EMAIL={email}\nMEGA_PASSWORD={password}\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if fd != -1:
                os.close(fd)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

def _keychain_mega_account() -> Optional[str]:
    """The account of the first mega.nz item `security` returns, or None.

    `security` has no "list every match" mode, so `-g` on the default query is
    how the stored email is discovered. The password is then fetched scoped to
    exactly this account (see _keychain_mega_password) so the email and the
    password can never be read from two different items — which is what happens
    when a stale entry for an older address is still in the keychain.
    """
    try:
        result = subprocess.run(
            ["security", "find-internet-password", "-s", "mega.nz", "-r", "htps", "-g"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    email_match = re.search(r'"acct"<blob>="([^"]+)"', result.stdout)
    return email_match.group(1) if email_match else None

def _keychain_mega_password(account: Optional[str]) -> Optional[str]:
    """The stored password, scoped to `account` when given.

    Uses `-w` (the raw value) instead of parsing `-g`'s quoted output, so a
    password containing quotes or backslashes survives intact.
    """
    cmd = ["security", "find-internet-password", "-s", "mega.nz", "-r", "htps", "-w"]
    if account:
        cmd += ["-a", account]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None

def _keychain_mega_creds_for(email: str) -> Optional[tuple[str, str]]:
    """Keychain credentials for exactly this account (macOS only)."""
    email = str(email or "").strip()
    if sys.platform != "darwin" or not email:
        return None
    password = _keychain_mega_password(email)
    return (email, password) if password else None

def _keychain_mega_creds() -> Optional[tuple[str, str]]:
    """Look up the default MEGA account's credentials in the OS store.

    Tries, in order: MEGA_EMAIL (explicit override), the email recorded for the
    default account by the setup wizard, then the item macOS returns first
    (keeps setup-keychain.sh entries working) — and finally any `keyring`
    backend (macOS Keychain, Windows Credential Manager, Linux Secret Service).
    """
    if sys.platform == "darwin":
        first = _keychain_mega_account()
        recorded = ""
        default = accounts.load_instance("mega", accounts.DEFAULT_NAME)
        if default is not None:
            recorded = default.email
        candidates = _dedupe(
            [os.environ.get("MEGA_EMAIL", ""), recorded, first]
        )
        for account in candidates:
            password = _keychain_mega_password(account)
            if password:
                return account, password

    kr = _keyring()
    if kr is not None:
        try:
            email = kr.get_password(_KEYRING_SERVICE, "email")
            if email:
                password = kr.get_password(_KEYRING_SERVICE, email)
                if password:
                    return email, password
        except Exception:
            pass
    return None

def _delete_mega_creds_keychain(account: str) -> bool:
    """Best-effort removal of the mega.nz item for `account` (macOS only)."""
    if sys.platform != "darwin" or not account:
        return False
    try:
        result = subprocess.run(
            [
                "security",
                "delete-internet-password",
                "-s", "mega.nz",
                "-r", "htps",
                "-a", account,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0

def _store_mega_creds_keychain(
    email: str, password: str, stale_accounts: Iterable[str] = ()
) -> None:
    # macOS only: store via the `security` binary (setup-keychain.sh parity).
    #
    # `-U` only updates an item that already carries the same account, so a
    # changed MEGA email adds a *second* mega.nz item instead of replacing the
    # old one — and an account-less `find-internet-password` used to keep
    # handing back the older item, silently pinning the bridge to a stale
    # address (seen with `<user>+bridge@gmail.com` shadowing `<user>@gmail.com`:
    # every login failed with ENOENT).
    #
    # `stale_accounts` is the caller's judgement about which items are now
    # orphaned; an empty value deletes nothing. That is what keeps a second
    # account from ever removing the first account's item.
    subprocess.run(
        [
            "security",
            "add-internet-password",
            "-s", "mega.nz",
            "-r", "htps",
            "-a", email,
            "-w", password,
            "-T", "/usr/bin/security",
            "-U",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    for account in _dedupe(stale_accounts):
        if account == str(email).strip():
            continue
        if not _delete_mega_creds_keychain(account):
            print(
                f"warning: could not remove the old MEGA keychain entry for "
                f"{account}; delete it in Keychain Access so it cannot shadow "
                f"{email}.",
                file=sys.stderr,
            )

def _store_mega_creds_os(
    email: str, password: str, stale_accounts: Iterable[str] = ()
) -> str:
    """Store credentials in the OS credential store.

    Returns the backend name. Raises RuntimeError when no usable store is
    available (caller falls back to the credentials file).
    """
    if sys.platform == "darwin":
        try:
            _store_mega_creds_keychain(email, password, stale_accounts)
            return "macOS Keychain"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass  # fall through to keyring
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_KEYRING_SERVICE, "email", email)
            kr.set_password(_KEYRING_SERVICE, email, password)
            return "system keyring"
        except Exception as exc:  # pragma: no cover - backend-specific
            raise RuntimeError(f"system keyring store failed: {exc}") from exc
    raise RuntimeError("no OS credential store is available")

def _mega_creds_source(name: str = accounts.DEFAULT_NAME) -> Optional[str]:
    """Where creds would come from: 'env', 'file', 'keychain', or None.

    For a named account the 'file' source is that account's own 0600 file
    (accounts.secret_path), never the default account's credentials.env.
    """
    if name == accounts.DEFAULT_NAME:
        if os.environ.get("MEGA_EMAIL", "").strip() and os.environ.get(
            "MEGA_PASSWORD", ""
        ).strip():
            return "env"
        if _read_creds_file() is not None:
            return "file"
        if _keychain_mega_creds() is not None:
            return "keychain"
        return None
    instance = accounts.load_instance("mega", name)
    email = instance.email if instance is not None else ""
    if email and _keychain_mega_creds_for(email) is not None:
        return "keychain"
    if _read_creds_file(accounts.secret_path("mega", name, "credentials.env")) is not None:
        return "file"
    return None
