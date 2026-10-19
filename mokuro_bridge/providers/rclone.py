"""rclone-backed upload destination.

Why this provider exists: a self-hosted bridge cannot ship an Azure app
registration, and creating one requires an Entra (Azure/365) tenant that a
personal Microsoft account cannot make. rclone already ships its own Microsoft
app registration, so `rclone config` can authorize OneDrive with just a
personal Microsoft account. This provider lets the bridge use any remote from
that config, which also covers Google Drive, S3 and the rest without adding an
SDK per backend.

An account here is an rclone remote name (``onedrive:``) plus a folder inside
it. Credentials stay in rclone's own config: the bridge never sees or stores a
token. Every operation shells out to the rclone binary, the same way the MEGA
provider shells out to megatools.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .. import accounts
from ..accounts import DEFAULT_NAME
from ..config import RCLONE_BINARY, RCLONE_ROOT_NAME
from .base import (
    DOWNLOAD_LIMIT_BYTES,
    LIST_LIMIT_ENTRIES,
    Provider,
    ProviderAccount,
    listing_entry,
    provider_account_lock,
    safe_provider_error,
)

_RCLONE_HINT = (
    "rclone destinations need the rclone command-line tool. Install it with "
    "`brew install rclone` (macOS), `winget install Rclone.Rclone` (Windows) "
    "or `apt install rclone` (Debian/Ubuntu), then run `rclone config` in a "
    "terminal to sign in to OneDrive, Google Drive or another service."
)

# `rclone listremotes` is cheap but /health asks per account on every poll, so
# the answer is reused for a few seconds.
_REMOTES_TTL_S = 5.0
_remotes_cache: Optional[tuple[float, list[str]]] = None
_remotes_lock = threading.Lock()

# copyto progress lines, one per stats interval (stdout, one line):
#   1.234 MiB / 5.678 MiB, 21%, 1.234 MiB/s, ETA 3s
_PROGRESS_PCT_RE = re.compile(r"(\d+)%")
_PROGRESS_SPEED_RE = re.compile(r"([\d.]+)\s*([KMGTP]?)i?B/s")
_SIZE_FACTORS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}

# rclone's "file not found" exit code for lsjson/cat.
_NOT_FOUND = 3


def binary() -> Optional[str]:
    """The rclone executable, or None when it is not installed."""
    name = os.environ.get("RCLONE_BINARY", "").strip() or RCLONE_BINARY
    return shutil.which(name)


def _require_binary() -> str:
    found = binary()
    if found is None:
        raise RuntimeError(_RCLONE_HINT)
    return found


def _run(
    args: list[str], timeout: float = 60.0, config: Optional[str] = None
) -> subprocess.CompletedProcess:
    """Run rclone and capture text output; never raises for a non-zero exit."""
    base = [_require_binary()]
    if config:
        base += ["--config", config]
    return subprocess.run(
        [*base, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _detail(result: subprocess.CompletedProcess) -> str:
    """The most useful part of rclone's output, with URLs redacted."""
    text = (result.stderr or result.stdout or "").strip()
    return safe_provider_error(text[-500:])


def list_remotes(*, refresh: bool = False) -> list[str]:
    """Every remote in rclone's config, as ``name:`` strings (cached briefly)."""
    global _remotes_cache
    now = time.monotonic()
    with _remotes_lock:
        if (
            not refresh
            and _remotes_cache is not None
            and now - _remotes_cache[0] < _REMOTES_TTL_S
        ):
            return list(_remotes_cache[1])
    if binary() is None:
        return []
    try:
        result = _run(["listremotes"], timeout=10.0)
    except (OSError, subprocess.TimeoutExpired):
        return []
    remotes = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    remotes = [value if value.endswith(":") else f"{value}:" for value in remotes]
    with _remotes_lock:
        _remotes_cache = (now, remotes)
    return list(remotes)


_dump_cache: Optional[tuple[float, dict]] = None
_dump_lock = threading.Lock()


def config_dump(*, refresh: bool = False) -> dict:
    """rclone's config as {remote: {key: value}} (cached briefly).

    Used for non-secret status only: never log or return token values.
    """
    global _dump_cache
    now = time.monotonic()
    with _dump_lock:
        if not refresh and _dump_cache is not None and now - _dump_cache[0] < _REMOTES_TTL_S:
            return dict(_dump_cache[1])
    if binary() is None:
        return {}
    try:
        result = _run(["config", "dump"], timeout=15.0)
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    dump = data if isinstance(data, dict) else {}
    with _dump_lock:
        _dump_cache = (now, dump)
    return dict(dump)


def _instance(name: str = DEFAULT_NAME) -> Optional[accounts.Instance]:
    return accounts.load_instance("rclone", name)


def remote_for(name: str = DEFAULT_NAME) -> str:
    """The account's rclone remote, with the trailing colon ("" when unset)."""
    instance = _instance(name)
    if instance is None:
        return ""
    raw = str(instance.extra.get("remote") or "").strip()
    if not raw:
        return ""
    return raw if raw.endswith(":") else f"{raw}:"


def configured(name: str = DEFAULT_NAME) -> bool:
    """Usable right now: rclone installed and the named remote still exists."""
    remote = remote_for(name)
    return bool(binary() and remote and remote in list_remotes())


def creds_source(name: str = DEFAULT_NAME) -> Optional[str]:
    """Where the credentials live. rclone owns them, so there is no secret here."""
    return "rclone config" if configured(name) else None


def account_identity(instance: accounts.Instance) -> str:
    """The non-secret identity shown for one account: what the remote is."""
    remote = str(instance.extra.get("remote") or "").strip()
    if not remote:
        return ""
    try:
        from .. import rclone_auth

        summary = rclone_auth.remote_summary(remote)
    except Exception:
        summary = ""
    return summary or remote


def account_insecure(*_args, **_kwargs) -> bool:  # symmetry with the HTTP providers
    return False


def gateway_enabled() -> bool:
    """False when MOKURO_BRIDGE_RCLONE_GATEWAY=0, for tests and debugging."""
    raw = os.environ.get("MOKURO_BRIDGE_RCLONE_GATEWAY")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def transport_for(name: str = DEFAULT_NAME) -> tuple[str, Optional[str]]:
    """(remote, rclone config) the bridge should transfer through right now.

    Prefers the shared loopback gateway so the bridge and Bunko use one cloud
    session. Falls back to the cloud remote directly when the gateway cannot
    start, which keeps a non-2FA account working even if `rclone serve` fails.
    """
    remote = remote_for(name)
    if not remote or not gateway_enabled():
        return remote, None
    try:
        from .. import rclone_gateway

        found = rclone_gateway.transport(name)
        return found if found else (remote, None)
    except Exception:
        return remote, None


def _target(remote: str, remote_dir: str, filename: str = "") -> str:
    """``<remote>:<root>/<series>/<file>`` with single separators."""
    parts = [part for part in str(remote_dir or "").strip("/").split("/") if part]
    if filename:
        parts.append(str(filename).strip("/"))
    # An on-the-fly remote (":webdav,...:") treats a path without a leading
    # slash inconsistently, so give those one. Named remotes ("demo:lib") keep
    # the form every backend has always accepted.
    prefix = "/" if remote.startswith(":") else ""
    return f"{remote}{prefix}{'/'.join(parts)}"


def _lsjson(path: str, config: Optional[str] = None) -> list[dict]:
    """One directory listing (``[]`` when the folder does not exist yet)."""
    result = _run(["lsjson", path], timeout=120.0, config=config)
    if result.returncode == _NOT_FOUND:
        return []
    if result.returncode != 0:
        raise RuntimeError(f"rclone could not list {path}: {_detail(result)}")
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise RuntimeError(f"rclone returned a listing that is not JSON: {error}") from error
    return data if isinstance(data, list) else []


def _stat(path: str, config: Optional[str] = None) -> Optional[dict]:
    """Metadata for one file/folder, or None when it does not exist."""
    result = _run(["lsjson", "--stat", path], timeout=60.0, config=config)
    if result.returncode == _NOT_FOUND:
        return None
    if result.returncode != 0:
        raise RuntimeError(f"rclone could not read {path}: {_detail(result)}")
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError as error:
        raise RuntimeError(f"rclone returned metadata that is not JSON: {error}") from error
    return data if isinstance(data, dict) else {}


def check_remote(remote: str) -> tuple[bool, Optional[str]]:
    """Prove that one remote is reachable and authorized, for setup."""
    if not remote.endswith(":"):
        remote += ":"
    if remote not in list_remotes():
        available = ", ".join(list_remotes()) or "none"
        return False, f"rclone has no remote named {remote} (available: {available})"
    try:
        result = _run(["lsf", remote, "--max-depth", "1"], timeout=60.0)
    except subprocess.TimeoutExpired:
        return False, f"{remote} did not answer in time"
    except OSError as error:
        return False, str(error)
    if result.returncode != 0:
        return False, f"{remote} is configured but not usable: {_detail(result)}"
    return True, None


def _parse_progress(line: str, total: int) -> Optional[tuple[int, int, int]]:
    """One rclone stats line → (done, total, speed_bps), or None for other lines."""
    percent = _PROGRESS_PCT_RE.search(line)
    if percent is None or total <= 0:
        return None
    try:
        done = min(total, int(total * int(percent.group(1)) / 100))
    except ValueError:
        return None
    speed = 0
    found = _PROGRESS_SPEED_RE.search(line)
    if found:
        try:
            speed = int(float(found.group(1)) * _SIZE_FACTORS.get(found.group(2).upper(), 1))
        except ValueError:
            speed = 0
    return done, total, speed


def _run_with_progress(
    args: list[str], total: int, on_progress, config: Optional[str] = None
) -> tuple[int, str]:
    """Run a copy command, streaming its stats lines into on_progress."""
    base = [_require_binary()]
    if config:
        base += ["--config", config]
    proc = subprocess.Popen(
        [*base, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            tail.append(line)
            del tail[:-8]
            if on_progress is not None:
                parsed = _parse_progress(line, total)
                if parsed is not None:
                    on_progress(*parsed)
        proc.wait()
    except BaseException:
        proc.kill()
        raise
    return proc.returncode or 0, "\n".join(tail)


def _upload(
    name: str,
    root: str,
    remote_dir: str,
    local_path: Path,
    on_progress: Optional[callable],
    overwrite: str = "fail",
) -> tuple[bool, Optional[str], Optional[str]]:
    """Upload one file through ``rclone copyto``.

    The remote folder is created with ``rclone mkdir`` first (the copyto command
    itself does not guarantee parent folders on every backend), progress comes
    from rclone's own stats lines, and the shared overwrite policy is enforced
    with a metadata probe before anything is transferred.
    """
    try:
        remote, bridge_config = transport_for(name)
        if not remote:
            raise RuntimeError(_RCLONE_HINT)
        if not configured(name):
            raise RuntimeError(
                f"rclone remote {remote} is not available. Run `rclone config` "
                "to set it up, or choose another remote in the destination's settings."
            )
        total = local_path.stat().st_size
        target_dir = _target(remote, remote_dir)
        target = _target(remote, remote_dir, local_path.name)
        made = _run(["mkdir", target_dir], timeout=120.0, config=bridge_config)
        if made.returncode != 0:
            return False, safe_provider_error(f"rclone could not create {target_dir}: {_detail(made)}"), None
        existing = _stat(target, config=bridge_config)
        if existing is not None:
            if overwrite == "skip":
                if on_progress is not None:
                    on_progress(total, total, 0)
                return True, None, None
            if overwrite not in ("overwrite", "replace"):
                return (
                    False,
                    f"destination already exists: {local_path.name} "
                    "(send overwrite=overwrite to replace it, or overwrite=skip "
                    "to keep the existing copy)",
                    None,
                )
        args = ["copyto", str(local_path), target, "--stats", "1s", "--stats-one-line"]
        if existing is not None:
            # copyto skips a same-size, same-mtime file by default, which would
            # make "overwrite" a no-op for a re-rendered volume.
            args.append("--ignore-times")
        if on_progress is not None and total:
            on_progress(0, total, 0)
        returncode, tail = _run_with_progress(args, total, on_progress, config=bridge_config)
        if returncode != 0:
            return False, safe_provider_error(f"rclone copyto failed: {tail[-400:]}"), None
        if on_progress is not None:
            on_progress(total, total, 0)
        return True, None, None
    except Exception as error:
        return False, safe_provider_error(error), None


def ensure_account_root(name: str = DEFAULT_NAME) -> bool:
    """Create the account's upload folder now (called after setup)."""
    with provider_account_lock("rclone", name):
        remote, bridge_config = transport_for(name)
        if not remote:
            raise RuntimeError(_RCLONE_HINT)
        instance = _instance(name)
        root = instance.root_path if instance is not None else ""
        if not root:
            return False
        result = _run(["mkdir", _target(remote, root)], timeout=120.0, config=bridge_config)
        if result.returncode != 0:
            raise RuntimeError(f"rclone could not create {remote}{root}: {_detail(result)}")
        return True


def _remote_exists(name: str, remote_dir: str, filename: str) -> bool:
    """Whether a file exists, without creating a folder or uploading anything."""
    with provider_account_lock("rclone", name):
        remote, bridge_config = transport_for(name)
        if not remote:
            raise RuntimeError(_RCLONE_HINT)
        return _stat(_target(remote, remote_dir, filename), config=bridge_config) is not None


def _download(name: str, remote_dir: str, filename: str) -> Optional[bytes]:
    """Read one small sidecar back; None when it is absent."""
    with provider_account_lock("rclone", name):
        remote, bridge_config = transport_for(name)
        if not remote:
            raise RuntimeError(_RCLONE_HINT)
        target = _target(remote, remote_dir, filename)
        base = [_require_binary()]
        if bridge_config:
            base += ["--config", bridge_config]
        proc = subprocess.Popen(
            [*base, "cat", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        chunks: list[bytes] = []
        received = 0
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                received += len(chunk)
                if received > DOWNLOAD_LIMIT_BYTES:
                    proc.kill()
                    raise RuntimeError(
                        f"refusing to read {remote_dir}/{filename}: it exceeded the "
                        f"{DOWNLOAD_LIMIT_BYTES}-byte sidecar limit"
                    )
                chunks.append(chunk)
            proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
        if proc.returncode == _NOT_FOUND:
            return None
        if proc.returncode != 0:
            assert proc.stderr is not None
            detail = safe_provider_error(proc.stderr.read().decode("utf-8", "replace"))
            raise RuntimeError(f"rclone could not read {remote_dir}/{filename}: {detail}")
        return b"".join(chunks)


def _list_folders(name: str, remote_dir: str) -> list[dict]:
    """List one remote folder (read-only); an absent folder is an empty listing."""
    with provider_account_lock("rclone", name):
        remote, bridge_config = transport_for(name)
        if not remote:
            raise RuntimeError(_RCLONE_HINT)
        base = str(remote_dir or "").strip("/")
        entries: list[dict] = []
        for item in _lsjson(_target(remote, base), config=bridge_config):
            entry_name = str(item.get("Name") or "")
            if not entry_name:
                continue
            is_folder = bool(item.get("IsDir"))
            size = item.get("Size")
            entries.append(
                listing_entry(
                    entry_name,
                    f"{base}/{entry_name}".strip("/"),
                    is_folder=is_folder,
                    size=None if is_folder or not isinstance(size, int) else size,
                )
            )
            if len(entries) >= LIST_LIMIT_ENTRIES:
                break
        entries.sort(key=lambda entry: (entry["type"] != "folder", entry["name"].casefold()))
        return entries


def _account_view(instance: accounts.Instance) -> ProviderAccount:
    """Labels and health fields for one rclone account."""
    remote = remote_for(instance.name) or "no remote set"
    return ProviderAccount(
        label=instance.display_name_for("rclone"),
        extra={"remote": remote, "root": instance.root_path},
        current_folder=f"{instance.root_path} on {remote}",
    )


def _run_setup_rclone(name: Optional[str] = None, root: str = "", label: str = "") -> None:
    """Interactive wizard: pick one configured rclone remote and a folder."""
    if binary() is None:
        print(_RCLONE_HINT)
        return
    remotes = list_remotes(refresh=True)
    print("rclone upload setup for mokuro-bridge")
    print("-" * 40)
    if not remotes:
        print(
            "No rclone remotes are configured yet. In a terminal run:\n"
            "  rclone config\n"
            "  1. Choose 'n' for a new remote.\n"
            "  2. Name it, for example 'onedrive'.\n"
            "  3. Choose 'Microsoft OneDrive'. Leave the client ID and secret\n"
            "     blank: rclone has its own Microsoft app registration, so no\n"
            "     Azure account is needed.\n"
            "  4. Let rclone open the browser and sign in.\n"
            "Then run this setup again."
        )
        return
    print("Configured rclone remotes: " + ", ".join(remotes))
    name = accounts.ask_account_name(name, "rclone")
    current = remote_for(name or DEFAULT_NAME)
    answer = input(f"rclone remote [{current or remotes[0]}]: ").strip()
    remote = (answer or current or remotes[0]).rstrip(":")
    remote = f"{remote}:"
    if remote not in remotes:
        print(f"error: {remote} is not in rclone's config (available: {', '.join(remotes)})")
        return
    ok, error = check_remote(remote)
    if not ok:
        print(f"error: {error}")
        return
    current_root = root or RCLONE_ROOT_NAME
    answer = input(f"Upload folder [{current_root}]: ").strip()
    root_value = answer or current_root
    stored = accounts.save_instance(
        "rclone",
        name,
        label=(label or None),
        root=root_value,
        alias=accounts.alias_for_storage("rclone", name),
        extra={"remote": remote},
    )
    print(f"Remote: {remote}")
    print(f"Upload folder: {stored.root_path} (one folder per series inside it)")
    print(f"Account ready: {stored.id}")
    print("Credentials stay in rclone's own configuration; nothing secret is stored here.")


PROVIDER = Provider(
    id="rclone",
    label="rclone",
    configured=configured,
    creds_source=creds_source,
    upload=_upload,
    remote_exists=_remote_exists,
    download=_download,
    list_folders=_list_folders,
    run_setup=_run_setup_rclone,
    account=_account_view,
)
