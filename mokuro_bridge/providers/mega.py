from __future__ import annotations
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from .. import accounts
from ..accounts import DEFAULT_NAME
from ..util import _chmod_fd_private
from ..config import MEGA_CREDS_FILE, WORK_DIR
from ..creds import (
    _keychain_mega_account,
    _keychain_mega_creds,
    _keychain_mega_creds_for,
    _mega_creds_source,
    _read_creds_file,
    _store_mega_creds_os,
    _write_creds_file,
)

def _mega_configured(name: str = DEFAULT_NAME) -> bool:
    """Whether one MEGA account is usable right now."""
    return bool(shutil.which("megatools")) and _mega_creds_source(name) is not None

def _mega_account_secret_file(name: str) -> Path:
    """The 0600 fallback credential file for a non-default MEGA account."""
    return accounts.secret_path("mega", name, "credentials.env")

def _get_mega_creds(name: str = DEFAULT_NAME) -> tuple[str, str]:
    """Credentials for one MEGA account.

    The default account keeps the historical order (env → credentials file →
    OS keychain). A named account uses the email recorded in its account file
    and looks the password up in the OS store scoped to that email, falling
    back to its own 0600 credential file.
    """
    if name == DEFAULT_NAME:
        env_email = os.environ.get("MEGA_EMAIL", "").strip()
        env_password = os.environ.get("MEGA_PASSWORD", "").strip()
        if env_email and env_password:
            return env_email, env_password
        file_creds = _read_creds_file()
        if file_creds is not None:
            return file_creds
        keychain_creds = _keychain_mega_creds()
        if keychain_creds is not None:
            return keychain_creds
        raise RuntimeError(
            "MEGA credentials not configured. Set MEGA_EMAIL + MEGA_PASSWORD "
            "environment variables, run `python server.py --setup-mega` (stores "
            "them in your OS keychain/credential store or a 0600 file), or write "
            "them to the credentials file."
        )

    instance = accounts.load_instance("mega", name)
    if instance is None:
        raise RuntimeError(
            f"MEGA account '{name}' is not configured. Add it with "
            f"`python server.py --setup-upload mega --name {name}`."
        )
    email = instance.email
    if email:
        keychain_creds = _keychain_mega_creds_for(email)
        if keychain_creds is not None:
            return keychain_creds
    file_creds = _read_creds_file(_mega_account_secret_file(name))
    if file_creds is not None:
        return file_creds
    raise RuntimeError(
        f"MEGA account '{name}' has no usable password: nothing in the OS "
        f"keychain for {email or 'its recorded email'} and no "
        f"{_mega_account_secret_file(name)}. Re-run `python server.py "
        f"--setup-upload mega --name {name}`."
    )

def _run_setup_mega(
    name: Optional[str] = None, root: str = "", label: str = ""
) -> None:
    """Interactive wizard: store credentials for one MEGA account.

    Run it again with a different `name` to add a second account; the default
    account keeps using the bare `mega` method id, additional ones are
    addressed as `mega:<name>`.
    """
    import getpass

    if not shutil.which("megatools"):
        print(
            "MEGA upload needs the megatools command-line tool. "
            "Install it with:  brew install megatools   (macOS)\n"
            "                   apt install megatools   (Debian/Ubuntu)\n"
            "then re-run: python server.py --setup-upload mega"
        )
        return

    name = accounts.ask_account_name(name, "mega")
    try:
        accounts.parse_method_id(accounts.method_id("mega", name))
    except ValueError as exc:
        print(f"error: {exc}")
        return

    print("MEGA upload setup for mokuro-bridge")
    print("-" * 40)
    print(f"Account: {accounts.method_id('mega', name)}")
    instance = accounts.load_instance("mega", name)
    existing = _mega_creds_source(name)
    if instance is not None and instance.tracked:
        whose = instance.email or "(no email recorded)"
        answer = input(
            f"Account {instance.id} already exists ({whose}). Overwrite? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing credentials.")
            return
    elif existing:
        answer = input(
            f"MEGA credentials already found ({existing}) for the default "
            "account. Overwrite? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing credentials.")
            return

    if name == DEFAULT_NAME and (
        os.environ.get("MEGA_EMAIL", "") and os.environ.get("MEGA_PASSWORD", "")
    ):
        print(
            "MEGA_EMAIL/MEGA_PASSWORD are set in the environment — the wizard "
            "cannot (and should not) override those. Export them instead."
        )
        return

    email = input("MEGA email: ").strip()
    if not email:
        print("No email given — aborting.")
        return
    password = getpass.getpass("MEGA password: ")

    # Verify the credentials against MEGA BEFORE storing anything. megatools ls
    # on the root succeeds only with valid login.
    ok, verify_err = _mega_verify_creds(email, password)
    if not ok:
        print(f"error: MEGA login failed — credentials not stored. {verify_err or ''}".strip())
        return

    # Which keychain items this store leaves orphaned: the email this account
    # used before, unless some *other* tracked account still uses it. Empty
    # unless there really is something to drop, so adding an account never
    # deletes a sibling account's item.
    previous_email = ""
    if instance is not None and instance.tracked:
        previous_email = instance.email
    if not previous_email and name == DEFAULT_NAME:
        previous_email = _keychain_mega_account() or ""
    others = {
        i.email
        for i in accounts.instances_for("mega")
        if i.email and i.name != name
    }
    stale = (
        [previous_email]
        if previous_email and previous_email != email and previous_email not in others
        else []
    )

    secret_file = (
        MEGA_CREDS_FILE if name == DEFAULT_NAME else _mega_account_secret_file(name)
    )
    backend = ""
    try:
        backend = _store_mega_creds_os(email, password, stale)
    except RuntimeError as exc:
        print(f"{exc}; falling back to a credentials file.")

    # Record the instance first: the source check below resolves a *named*
    # account through its account file (name → email → keychain item).
    stored = accounts.save_instance(
        "mega",
        name,
        label=(label or None),
        root=(root or None),
        extra={"email": email},
    )
    if backend:
        print(f"Stored in {backend}.")
    if not _mega_creds_source(name):
        _write_creds_file(email, password, secret_file)
        print(f"Stored in {secret_file} (permissions 0600).")
    if stored is not None and stored.root:
        print(f"Remote root: {stored.root_path}")
    print(
        f"Account ready: {stored.id if stored else accounts.method_id('mega', name)}"
    )
    print(
        "Tip: you can also use environment variables MEGA_EMAIL / MEGA_PASSWORD "
        "instead of storing anything."
    )


def _mega_verify_creds(email: str, password: str) -> tuple[bool, Optional[str]]:
    """Check MEGA credentials by listing the account root with a temp megarc.

    Returns (True, None) on success, or (False, error_message) when the login
    is rejected (wrong email/password). The temporary megarc is deleted even
    on failure.
    """
    megarc_path = create_megarc(email, password)
    try:
        result = subprocess.run(
            ["megatools", "ls", "--config", str(megarc_path), "/"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True, None
        err = (result.stderr or result.stdout or "").strip()
        if not err:
            err = f"megatools exited with code {result.returncode}"
        return False, err
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, str(exc)
    finally:
        megarc_path.unlink(missing_ok=True)

def create_megarc(email: str, password: str) -> Path:
    if any("\n" in str(value) or "\r" in str(value) for value in (email, password)):
        raise ValueError("MEGA credentials must not contain newlines")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".megarc_", dir=str(WORK_DIR))
    megarc = Path(name)
    try:
        _chmod_fd_private(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(f"[Login]\nUsername = {email}\nPassword = {password}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return megarc
    except Exception:
        if fd != -1:
            os.close(fd)
        megarc.unlink(missing_ok=True)
        raise

def mega_mkdir(megarc_path: Path, remote_dir: str) -> subprocess.CompletedProcess:
    # mkdir takes remote paths as positional args (no --path)
    return subprocess.run(
        ["megatools", "mkdir", "--config", str(megarc_path), remote_dir],
        capture_output=True,
        text=True,
        timeout=30,
    )

def mega_put(megarc_path: Path, local_path: Path, remote_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "megatools",
            "put",
            "--config",
            str(megarc_path),
            "--path",
            remote_path,
            str(local_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )

# Progress lines from `megatools put` (progress bar disabled when stdout is not
# a TTY — each update becomes a newline-terminated plain line, ~1/sec).
# NOTE: megatools uses the C locale's decimal separator — on a locale that
# uses a comma (e.g. de_DE) lines look like "x: 42,50% - 12,4 MiB of 29,2 MiB
# (5,2 MiB/s)". The regexes accept both "." and "," so progress is parsed
# regardless of locale.
#   My Manga 1巻.cbz: 42.50% - 12.4 MiB of 29.2 MiB (5.2 MiB/s)
#   My Manga 1巻.cbz: 100.00% - done 29.2 MiB (avg. 5.2 MiB/s)
# and the completion line:
#   Uploaded My Manga 1巻.cbz
_MEGATOOLS_PROGRESS_RE = re.compile(
    r"^([^:]+):\s+(\d+(?:[.,]\d+)?)%\s*-\s*(.*?)(?:\s+\(([^)]+)\))?$"
)
_MEGATOOLS_UPLOADED_RE = re.compile(r"^Uploaded\s+(.+)$")


def _parse_megatools_size(s: str) -> int:
    """Parse a megatools human size ("29,2 MiB", "12.4 MiB", "1024 Bytes") → bytes.

    Accepts "." or "," as the decimal separator (locale-dependent output), and
    strips thousand-group separators ("1.406.482" → 1406482, "8,388,608" →
    8388608).
    """
    s = s.strip()
    m = re.match(r"^([\d.,]+)\s*([A-Za-z]*)$", s)
    if not m:
        return 0
    raw, unit = m.group(1), m.group(2).upper()
    # Normalize the decimal separator: if both appear, the LAST one is the
    # decimal point (e.g. "1.406.482" → 1406482, "8,388,608" → 8388608).
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(".", "")
    try:
        value = float(raw)
    except ValueError:
        return 0
    factors = {
        "": 1,
        "B": 1,
        "BYTES": 1,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
    }
    return int(value * factors.get(unit, 1))

def _parse_megatools_speed(s: str) -> int:
    """Parse "5.2 MiB/s" → bytes per second (0 when unparseable)."""
    return _parse_megatools_size(s.rstrip("/s"))


def _mega_remote_exists(megarc_path: Path, remote_path: str) -> bool:
    """Whether a file already exists at remote_path (megatools ls)."""
    try:
        result = subprocess.run(
            ["megatools", "ls", "--config", str(megarc_path), remote_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    # ls succeeds and lists the node when it exists; fails when missing.
    return result.returncode == 0 and bool(result.stdout.strip())

def _mega_upload_file(
    megarc_path: Path,
    local_path: Path,
    remote_path: str,
    on_progress: Optional[callable],
    overwrite: str = "fail",
) -> tuple[bool, Optional[str]]:
    """Upload one file with `megatools put`, streaming progress.

    Runs megatools with stdout piped (stderr still goes to the process's
    stderr). Progress is throttled by megatools to ~1 update/second and each
    line is flushed by glib, so `on_progress(bytes_done, total_bytes,
    speed_bps)` fires live (the caller derives the percent). Returns
    (success, error_message).

    overwrite: "fail" → an existing remote file is an error (a clear,
    method-agnostic message is returned); "skip" → existing file counts as
    success (nothing uploaded); "overwrite" → upload a versioned sibling and
    keep the prior valid copy (MEGA CLI has no atomic replace operation).
    """
    total_bytes = local_path.stat().st_size

    # Existing-file policy. megatools put refuses to overwrite (exit code 2,
    # "File already exists"), so implement skip/overwrite explicitly here.
    exists = _mega_remote_exists(megarc_path, remote_path)
    staged_path = ""
    if exists:
        if overwrite == "skip":
            # Already there — treat as success; try to surface its link too.
            url = None
            try:
                exp = subprocess.run(
                    ["megatools", "export", "--config", str(megarc_path), remote_path],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if exp.returncode == 0 and exp.stdout.strip():
                    url = exp.stdout.strip().splitlines()[0]
            except (subprocess.TimeoutExpired, FileNotFoundError):
                url = None
            return True, None, url
        if overwrite != "overwrite":
            return (
                False,
                f"destination already exists: {remote_path} "
                "(send overwrite=overwrite to replace it, or overwrite=skip "
                "to keep the existing copy)",
                None,
            )
        # megatools 1.x has no move/replace command.  Never delete the old
        # valid artifact before the replacement has uploaded successfully;
        # expose a versioned sibling instead.
        staged_path = f"{remote_path}.replace-{uuid.uuid4().hex[:10]}"

    put_path = staged_path or remote_path
    proc = subprocess.Popen(
        [
            "megatools",
            "put",
            "--config",
            str(megarc_path),
            "--path",
            put_path,
            str(local_path),
        ],
        stdout=subprocess.PIPE,
        stderr=None,  # inherit → megatools errors land on our stderr
        text=True,
        bufsize=1,  # line-buffered reads
    )
    assert proc.stdout is not None
    success = False
    error_msg: Optional[str] = None
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            m = _MEGATOOLS_UPLOADED_RE.match(line)
            if m:
                success = True
                if on_progress:
                    on_progress(total_bytes, total_bytes, 0)
                continue
            m = _MEGATOOLS_PROGRESS_RE.match(line)
            if m and on_progress:
                rest = m.group(3)
                speed_bps = _parse_megatools_speed(m.group(4) or "")
                # "12.4 MiB of 29.2 MiB" → done/total. The done part may carry
                # a parenthetical byte count: "1,3 MiB (1.406.482 Bytes) of
                # 8,0 MiB" — strip the "(…)" before parsing.
                size_match = re.match(r"^(.+?)\s+of\s+(.+)$", rest)
                if size_match:
                    done_str = re.sub(r"\s*\([^)]*\)", "", size_match.group(1))
                    bytes_done = min(_parse_megatools_size(done_str), total_bytes)
                    on_progress(bytes_done, total_bytes, speed_bps)
                elif rest.startswith("done "):
                    on_progress(total_bytes, total_bytes, speed_bps)
        proc.wait(timeout=300)
    except Exception as e:
        error_msg = str(e)
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
    if not success and error_msg is None:
        error_msg = f"megatools exited with code {proc.returncode}" if proc.returncode else "no completion line"
    # Shareable link for the uploaded file (best-effort; None on failure).
    url = None
    if success:
        try:
            exp = subprocess.run(
                ["megatools", "export", "--config", str(megarc_path), put_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if exp.returncode == 0:
                url = (exp.stdout or "").strip().splitlines()[0] if exp.stdout.strip() else None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            url = None
    return success, error_msg, url
