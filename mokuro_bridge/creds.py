from __future__ import annotations
import os
import re
import subprocess
import sys
from typing import Optional

from .config import MEGA_CREDS_FILE

# ── MEGA / helpers ─────────────────────────────────────────────────────

# ── MEGA credentials ──────────────────────────────────────────────────
# Resolution order:
#   1. MEGA_EMAIL / MEGA_PASSWORD env vars
#   2. MEGA_CREDS_FILE (KEY=VALUE, chmod 600)
#   3. the OS credential store, when one is available:
#        macOS  — Keychain (via `security`, or `keyring`)
#        Windows— Credential Manager (via `keyring`)
#        Linux  — Secret Service / gnome-keyring (via `keyring`)
# All optional: MEGA upload is disabled by default and can be skipped entirely.

_KEYRING_SERVICE = "mokuro-bridge"  # namespace used for keyring-based entries

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

def _read_creds_file() -> Optional[tuple[str, str]]:
    try:
        text = MEGA_CREDS_FILE.read_text(encoding="utf-8")
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

def _write_creds_file(email: str, password: str) -> None:
    MEGA_CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEGA_CREDS_FILE.write_text(
        f"# mokuro-bridge MEGA credentials — keep this file private.\n"
        f"MEGA_EMAIL={email}\nMEGA_PASSWORD={password}\n",
        encoding="utf-8",
    )
    try:
        MEGA_CREDS_FILE.chmod(0o600)
    except OSError:
        pass

def _keychain_mega_creds() -> Optional[tuple[str, str]]:
    """Look up MEGA credentials in the OS credential store.

    Tries, in order: macOS Keychain via `security` (keeps setup-keychain.sh
    entries working), then any `keyring` backend (macOS Keychain, Windows
    Credential Manager, Linux Secret Service).
    """
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-internet-password", "-s", "mega.nz", "-r", "htps", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                password = result.stdout.strip()
                acct_result = subprocess.run(
                    ["security", "find-internet-password", "-s", "mega.nz", "-r", "htps", "-g"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                email_match = re.search(r'"acct"<blob>="([^"]+)"', acct_result.stdout)
                if email_match:
                    return email_match.group(1), password
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # fall through to keyring

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

def _store_mega_creds_keychain(email: str, password: str) -> None:
    # macOS only: store via the `security` binary (setup-keychain.sh parity).
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

def _store_mega_creds_os(email: str, password: str) -> str:
    """Store credentials in the OS credential store.

    Returns the backend name. Raises RuntimeError when no usable store is
    available (caller falls back to the credentials file).
    """
    if sys.platform == "darwin":
        try:
            _store_mega_creds_keychain(email, password)
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

def _mega_creds_source() -> Optional[str]:
    """Where creds would come from: 'env', 'file', 'keychain', or None."""
    if os.environ.get("MEGA_EMAIL", "").strip() and os.environ.get(
        "MEGA_PASSWORD", ""
    ).strip():
        return "env"
    if _read_creds_file() is not None:
        return "file"
    if _keychain_mega_creds() is not None:
        return "keychain"
    return None
