"""One local rclone WebDAV gateway per cloud account, shared by the bridge and Bunko.

Why a gateway exists at all: rclone re-authenticates on every invocation, so a
MEGA account with two-factor sign-in would need a fresh single-use code for each
upload. `rclone serve webdav` signs in once, holds the remote session, and
exposes it on loopback. The bridge then talks WebDAV to that local port, and
Bunko points its WebDAV backend at the same port, so both processes share one
authenticated session and one view of the folder.

The gateway binds 127.0.0.1 only, uses a random Basic-auth pair per account, and
records its state in a 0600 JSON file under the bridge data directory. The state
file holds a loopback password, never a cloud token: the cloud credentials stay
in rclone's own config.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import accounts
from .accounts import DEFAULT_NAME

# The gateway is loopback-only. A short timeout keeps /health responsive when a
# gateway process has died without cleaning up its state file.
_HEALTH_TIMEOUT_S = 2.0
_START_TIMEOUT_S = 25.0


class RcloneGatewayError(RuntimeError):
    """A user-facing gateway failure."""


@dataclass
class Gateway:
    """A running (or recorded) gateway for one account."""

    name: str
    method: str
    remote: str
    root: str
    port: int
    username: str
    password: str
    obscured: str
    pid: int = 0
    started_at: float = 0.0
    client_remote: str = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def running(self) -> bool:
        return bool(self.pid) and _pid_alive(self.pid) and self.healthy()

    def healthy(self) -> bool:
        return _health(self.port, self.username, self.password)

    def bunko_payload(self) -> dict:
        """The WebDAV block Bunko's connect call expects."""
        return {
            "url": self.url,
            "username": self.username,
            "password": self.password,
            "allow_http": True,
        }


def data_home() -> Path:
    override = os.environ.get("MOKURO_BRIDGE_RCLONE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mokuro-bridge" / "rclone"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return Path(base or Path.home()) / "mokuro-bridge" / "rclone"
    return Path.home() / ".local" / "share" / "mokuro-bridge" / "rclone"


def state_path() -> Path:
    return data_home() / "gateways.json"


def client_config_path() -> Path:
    """The bridge-owned rclone config holding one remote per gateway.

    A named remote in a dedicated config avoids the on-the-fly
    ":webdav,url=...:" form, which mangled file-to-file copies.
    """
    return data_home() / "rclone.conf"


def client_remote_name(name: str = DEFAULT_NAME) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name)
    return f"mokuro-gateway-{safe or 'default'}"


def log_path(name: str = DEFAULT_NAME) -> Path:
    return data_home() / f"gateway-{name}.log"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _health(port: int, username: str, password: str) -> bool:
    """Whether the gateway answers WebDAV with our Basic-auth pair."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/", method="OPTIONS")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=_HEALTH_TIMEOUT_S) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        # 401 means something else owns the port; 2xx/4xx from the gateway is fine.
        return error.code != 401
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _load() -> dict:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 from the first byte: the file holds loopback passwords.
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if fd != -1:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


def _from_record(record: dict) -> Optional[Gateway]:
    try:
        return Gateway(
            name=str(record["name"]),
            method=str(record.get("method") or ""),
            remote=str(record.get("remote") or ""),
            root=str(record.get("root") or ""),
            port=int(record["port"]),
            username=str(record["username"]),
            password=str(record["password"]),
            obscured=str(record["obscured"]),
            pid=int(record.get("pid") or 0),
            started_at=float(record.get("started_at") or 0.0),
            client_remote=str(record.get("client_remote") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def recorded(name: str = DEFAULT_NAME) -> Optional[Gateway]:
    """The recorded gateway for one account, running or not."""
    record = _load().get(name)
    if not isinstance(record, dict):
        return None
    return _from_record(record)


def _account(name: str = DEFAULT_NAME) -> accounts.Instance:
    instance = accounts.load_instance("rclone", name)
    if instance is None:
        raise RcloneGatewayError(
            f"no rclone account is configured for {name}; add the destination first"
        )
    remote = str(instance.extra.get("remote") or "").strip()
    if not remote:
        raise RcloneGatewayError("this rclone destination has no remote recorded")
    return instance


def _rclone_command() -> str:
    from .providers import rclone as rclone_provider

    found = rclone_provider.binary()
    if found is None:
        raise RcloneGatewayError(
            "rclone is not installed; install it with `brew install rclone` or the "
            "package manager for this system"
        )
    return found


def _obscure(binary: str, password: str) -> str:
    result = subprocess.run(
        [binary, "obscure", password], capture_output=True, text=True, timeout=15.0
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        raise RcloneGatewayError("rclone could not obscure the gateway password")
    return result.stdout.strip()


def _write_client_remote(gateway: Gateway, binary: str) -> str:
    """Create or update the named WebDAV remote that points at this gateway."""
    remote = client_remote_name(gateway.name)
    config = client_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            binary, "--config", str(config), "config", "create", remote, "webdav",
            "url", gateway.url,
            "vendor", "other",
            "user", gateway.username,
            "pass", gateway.obscured,
        ],
        capture_output=True, text=True, timeout=20.0,
    )
    if result.returncode != 0:
        raise RcloneGatewayError(
            "rclone could not write the gateway remote: " + safe_detail(result)
        )
    try:
        os.chmod(config, 0o600)
    except OSError:
        pass
    # The colon is what makes rclone read the remote from the config instead of
    # treating the name as a local relative path.
    return f"{remote}:"


def safe_detail(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[-300:]


def _spawn(gateway: Gateway) -> Gateway:
    binary = _rclone_command()
    gateway.obscured = _obscure(binary, gateway.password)
    gateway.client_remote = _write_client_remote(gateway, binary)
    cloud = gateway.remote if gateway.remote.endswith(":") else f"{gateway.remote}:"
    # The whole remote is served, not just the account folder, because the
    # bridge's remote_dir already carries the account root prefix. Bunko scopes
    # itself with its own folder setting instead.
    args = [
        binary,
        "serve",
        "webdav",
        cloud,
        "--addr",
        f"127.0.0.1:{gateway.port}",
        "--user",
        gateway.username,
        "--pass",
        gateway.password,
        "--log-level",
        "INFO",
        "--log-file",
        str(log_path(gateway.name)),
    ]
    configured = os.environ.get("RCLONE_CONFIG", "").strip()
    if configured:
        args += ["--config", configured]
    log_path(gateway.name).parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # survives a bridge restart, like Bunko
    )
    gateway.pid = process.pid
    gateway.started_at = time.time()
    return gateway


def stop(name: str = DEFAULT_NAME) -> bool:
    """Stop one gateway but keep its endpoint so a restart reuses it.

    Bunko's saved WebDAV URL must keep working after the bridge restarts, so the
    port and Basic-auth pair survive; only the pid is cleared.
    """
    state = _load()
    record = state.get(name)
    if record is None:
        return False
    gateway = _from_record(record)
    if gateway is not None and gateway.pid:
        try:
            os.kill(gateway.pid, 15)
        except (ProcessLookupError, PermissionError):
            pass
        record["pid"] = 0
        _save(state)
    return True


def stop_all() -> int:
    state = _load()
    names = list(state)
    for name in names:
        stop(name)
    return len(names)


def ensure(name: str = DEFAULT_NAME, *, force: bool = False) -> Gateway:
    """Start the gateway for one account, or return the running one.

    The remote and folder are read from the account record, so a re-pointed
    account restarts on the new remote. The recorded port and Basic-auth pair
    are reused across restarts to keep Bunko's saved WebDAV URL valid.
    """
    instance = _account(name)
    remote = str(instance.extra.get("remote") or "").strip()
    if not remote.endswith(":"):
        remote = f"{remote}:"
    root = instance.root_path or ""

    existing = recorded(name)
    if existing is not None and not force:
        same_target = existing.remote == remote and existing.root == root
        if same_target and existing.running():
            return existing

    if existing is not None and existing.running():
        # The target changed under a live gateway: stop it before rebinding.
        try:
            os.kill(existing.pid, 15)
        except (ProcessLookupError, PermissionError):
            pass

    gateway = Gateway(
        name=name,
        method=instance.id,
        remote=remote,
        root=root,
        port=existing.port if existing is not None else _free_port(),
        username=existing.username if existing is not None else f"bridge-{secrets.token_hex(4)}",
        password=existing.password if existing is not None else secrets.token_hex(16),
        obscured=existing.obscured if existing is not None else "",
    )
    gateway = _spawn(gateway)
    deadline = time.monotonic() + _START_TIMEOUT_S
    while time.monotonic() < deadline:
        if gateway.healthy():
            state = _load()
            state[name] = {
                "name": gateway.name,
                "method": gateway.method,
                "remote": gateway.remote,
                "root": gateway.root,
                "port": gateway.port,
                "username": gateway.username,
                "password": gateway.password,
                "obscured": gateway.obscured,
                "pid": gateway.pid,
                "started_at": gateway.started_at,
                "client_remote": gateway.client_remote,
            }
            _save(state)
            return gateway
        if not _pid_alive(gateway.pid):
            break
        time.sleep(0.25)
    try:
        os.kill(gateway.pid, 15)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    raise RcloneGatewayError(
        "the rclone gateway did not answer in time; see "
        f"{log_path(name)} for the reason"
    )


def status(name: str = DEFAULT_NAME) -> dict:
    """Non-secret gateway state for the dashboard and health output."""
    gateway = recorded(name)
    if gateway is None:
        return {"running": False, "port": None, "url": None, "pid": None}
    return {
        "running": gateway.running(),
        "port": gateway.port,
        "url": gateway.url,
        "pid": gateway.pid or None,
    }


def transport(name: str = DEFAULT_NAME) -> Optional[tuple[str, str]]:
    """(named remote, rclone config path) for gateway-backed transfers.

    None when a gateway cannot be started, so callers fall back to the cloud
    remote directly.
    """
    try:
        gateway = ensure(name)
        remote = gateway.client_remote or f"{client_remote_name(name)}:"
        return remote, str(client_config_path())
    except (RcloneGatewayError, OSError, subprocess.SubprocessError):
        return None
