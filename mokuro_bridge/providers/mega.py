from __future__ import annotations
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from ..config import MEGA_CREDS_FILE, WORK_DIR
from ..creds import (
    _keychain_mega_creds,
    _mega_creds_source,
    _read_creds_file,
    _store_mega_creds_os,
    _write_creds_file,
)

def _mega_configured() -> bool:
    """Whether the MEGA upload method is usable right now."""
    return bool(shutil.which("megatools")) and _mega_creds_source() is not None

def _get_mega_creds() -> tuple[str, str]:
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

def _run_setup_mega() -> None:
    """Interactive first-run wizard: ask for MEGA credentials and store them."""
    import getpass

    if not shutil.which("megatools"):
        print(
            "MEGA upload needs the megatools command-line tool. "
            "Install it with:  brew install megatools   (macOS)\n"
            "                   apt install megatools   (Debian/Ubuntu)\n"
            "then re-run: python server.py --setup-upload mega"
        )
        return

    print("MEGA upload setup for mokuro-bridge")
    print("-" * 40)
    existing = _mega_creds_source()
    if existing:
        answer = input(
            f"MEGA credentials already found ({existing}). Overwrite? [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing credentials.")
            return

    if os.environ.get("MEGA_EMAIL", "") and os.environ.get("MEGA_PASSWORD", ""):
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

    try:
        backend = _store_mega_creds_os(email, password)
    except RuntimeError as exc:
        print(f"{exc}; falling back to a credentials file.")
    else:
        print(f"Stored in {backend}.")
        return
    _write_creds_file(email, password)
    print(f"Stored in {MEGA_CREDS_FILE} (permissions 0600).")
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
    # megatools requires a [Login] section (not [DEFAULT]) with Username=
    megarc = WORK_DIR / f".megarc_{uuid.uuid4().hex[:8]}"
    megarc.write_text(f"[Login]\nUsername = {email}\nPassword = {password}\n")
    megarc.chmod(0o600)
    return megarc

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
# a TTY — each update becomes a newline-terminated plain line, ~1/sec):
#   My Manga 1巻.cbz: 42.50% - 12.4 MiB of 29.2 MiB (5.2 MiB/s)
#   My Manga 1巻.cbz: 100.00% - done 29.2 MiB (avg. 5.2 MiB/s)
# and the completion line:
#   Uploaded My Manga 1巻.cbz
_MEGATOOLS_PROGRESS_RE = re.compile(
    r"^([^:]+):\s+(\d+(?:\.\d+)?)%\s*-\s*(.*?)(?:\s+\(([^)]+)\))?$"
)
_MEGATOOLS_UPLOADED_RE = re.compile(r"^Uploaded\s+(.+)$")

def _parse_megatools_size(s: str) -> int:
    """Parse a megatools human size ("29.2 MiB", "4.0 KiB", "1024 B") → bytes."""
    s = s.strip()
    m = re.match(r"^([\d.]+)\s*([A-Za-z]*)$", s)
    if not m:
        return 0
    value, unit = float(m.group(1)), m.group(2).upper()
    factors = {
        "": 1,
        "B": 1,
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
    success (nothing uploaded); "overwrite" → the remote file is deleted
    first, then uploaded fresh.
    """
    total_bytes = local_path.stat().st_size

    # Existing-file policy. megatools put refuses to overwrite (exit code 2,
    # "File already exists"), so implement skip/overwrite explicitly here.
    exists = _mega_remote_exists(megarc_path, remote_path)
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
        rm = subprocess.run(
            ["megatools", "rm", "--config", str(megarc_path), remote_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if rm.returncode != 0:
            return (
                False,
                f"could not remove existing remote file {remote_path}: "
                f"{(rm.stderr or rm.stdout or '').strip()}",
                None,
            )

    proc = subprocess.Popen(
        [
            "megatools",
            "put",
            "--config",
            str(megarc_path),
            "--path",
            remote_path,
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
                # "12.4 MiB of 29.2 MiB" → done/total
                size_match = re.match(r"^(.+?)\s+of\s+(.+)$", rest)
                if size_match:
                    bytes_done = min(_parse_megatools_size(size_match.group(1)), total_bytes)
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
                ["megatools", "export", "--config", str(megarc_path), remote_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if exp.returncode == 0:
                url = (exp.stdout or "").strip().splitlines()[0] if exp.stdout.strip() else None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            url = None
    return success, error_msg, url
