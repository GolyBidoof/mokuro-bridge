"""Install, configure and run mokuro-bunko for the bridge's cloud destination.

mokuro-bunko 0.8 remote storage is not released yet, so this module pins the
branch commit, builds the server-only ("lite") binary from source, writes a
minimal config for the destination the bridge uploads to, signs Bunko in to
the same account through Bunko's own local control API, and leaves the server
running. From then on the bridge's marker detection skips `series.json` for
that destination and Bunko compiles it in the cloud.

The pieces are deliberately separate and all I/O is injectable, because the
build takes minutes and the cloud sign-in is the user's account: tests cover
the config, the connect payload and the control conversation, not a real
build. `MOKURO_BUNKO_BIN` points at an existing binary and skips the build.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from . import bunko
from . import providers
from .providers import method_provider, method_root

BUNKO_REPO = "https://github.com/Gnathonic/mokuro-bunko.git"
# Remote storage is a development branch until 0.8 ships. The installer follows
# its head, so a fresh checkout or a rerun picks up the newest development build;
# MOKURO_BUNKO_REF pins a branch, tag or commit when reproducibility matters.
BUNKO_DEV_REF = "0.8/remote-storage"
BUNKO_BUILD_LABEL = "0.8 remote-storage (development)"
BUNKO_LIBRARY_FOLDER = "mokuro-reader"

STATE_FILE = "state.json"
CONFIG_FILE = "config.yaml"
LOG_FILE = "bunko.log"
INSTALL_FILE = "install.json"
MARKER_WAIT_SECONDS = 60.0

_PROVIDER_BACKEND = {
    "mega": "mega",
    "drive": "gdrive",
    "onedrive": "onedrive",
    "webdav": "webdav",
    # An rclone destination reaches Bunko through the shared local gateway, so
    # Bunko speaks WebDAV to the same session the bridge uploads through.
    "rclone": "webdav",
}


class BunkoAgentError(RuntimeError):
    """A user-facing setup or control failure."""


def data_home() -> Path:
    override = os.environ.get("MOKURO_BRIDGE_BUNKO_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mokuro-bridge" / "bunko"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return Path(base or Path.home()) / "mokuro-bridge" / "bunko"
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(base or (Path.home() / ".local" / "share")) / "mokuro-bridge" / "bunko"


def storage_dir() -> Path:
    return data_home() / "storage"


def config_path() -> Path:
    return data_home() / CONFIG_FILE


def log_path() -> Path:
    return data_home() / LOG_FILE


def state_path() -> Path:
    return data_home() / STATE_FILE


def binary_path() -> Path:
    override = os.environ.get("MOKURO_BUNKO_BIN", "").strip()
    if override:
        return Path(override).expanduser()
    name = "mokuro-bunko.exe" if os.name == "nt" else "mokuro-bunko"
    return data_home() / "bin" / name


@dataclass
class InstallInfo:
    binary: str
    commit: str
    label: str
    built_at: str = ""
    source: str = ""

    def usable(self) -> bool:
        path = Path(self.binary)
        return path.is_file() and os.access(path, os.X_OK)


@dataclass
class AgentState:
    method: str
    backend: str
    folder: str
    storage: str
    config: str
    log: str
    binary: str
    commit: str
    port: int = 0
    pid: int = 0
    connected_at: str = ""
    admin_user: str = ""

    def save(self) -> None:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)


def load_state() -> Optional[AgentState]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return AgentState(
            method=str(data.get("method", "") or ""),
            backend=str(data.get("backend", "") or ""),
            folder=str(data.get("folder", "") or ""),
            storage=str(data.get("storage", "") or ""),
            config=str(data.get("config", "") or ""),
            log=str(data.get("log", "") or ""),
            binary=str(data.get("binary", "") or ""),
            commit=str(data.get("commit", "") or ""),
            port=int(data.get("port", 0) or 0),
            pid=int(data.get("pid", 0) or 0),
            connected_at=str(data.get("connected_at", "") or ""),
            admin_user=str(data.get("admin_user", "") or ""),
        )
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---- install -----------------------------------------------------------------


def _tail(path: Path, lines: int = 25) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(text[-lines:])


def _run(
    command: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    log: Optional[Path] = None,
) -> int:
    log_path_ = log
    handle = None
    try:
        if log_path_ is not None:
            log_path_.parent.mkdir(parents=True, exist_ok=True)
            handle = log_path_.open("a", encoding="utf-8")
            out = handle
        else:
            out = subprocess.DEVNULL
        return subprocess.run(
            command, cwd=str(cwd) if cwd else None, env=env, stdout=out, stderr=out, check=False
        ).returncode
    finally:
        if handle is not None:
            handle.close()


def installed() -> Optional[InstallInfo]:
    """The binary the last install left behind, or None."""
    try:
        data = json.loads((data_home() / INSTALL_FILE).read_text(encoding="utf-8"))
        info = InstallInfo(**data)
    except (OSError, ValueError, TypeError):
        return None
    return info if info.usable() else None


def _ls_remote(ref: str) -> Optional[str]:
    """Ask GitHub for the commit a branch/tag currently points at."""
    patterns = [f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", ref]
    for pattern in patterns:
        try:
            result = subprocess.run(
                ["git", "ls-remote", BUNKO_REPO, pattern],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and len(parts[0]) == 40:
                return parts[0].lower()
    return None


def value_is_commit(value: str) -> bool:
    text = value.strip().lower()
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text)


def resolve_ref(ref: Optional[str] = None, *, log: Callable[[str], None] = print) -> str:
    """The commit to build: a 40-hex ref is accepted as is, else GitHub resolves it."""
    value = (ref or os.environ.get("MOKURO_BUNKO_REF", "") or BUNKO_DEV_REF).strip()
    if value_is_commit(value):
        return value.lower()
    commit = _ls_remote(value)
    if commit is None:
        raise BunkoAgentError(
            f"could not resolve mokuro-bunko ref {value!r} (offline?). Set "
            "MOKURO_BUNKO_OFFLINE=1 to use the installed build, or MOKURO_BUNKO_REF "
            "to a commit."
        )
    log(f"mokuro-bunko {value} is at {commit[:9]}")
    return commit


def install(
    *,
    force: bool = False,
    ref: Optional[str] = None,
    log: Callable[[str], None] = print,
    runner: Callable[..., int] = _run,
) -> InstallInfo:
    """Make the newest build of the development branch available.

    The branch head is resolved through `git ls-remote` on every call, so a
    reinstall or `--update-bunko` moves to the newest development version. The
    installed commit is reused when it already matches. `MOKURO_BUNKO_REF`
    pins a branch, tag or commit; `MOKURO_BUNKO_OFFLINE=1` skips the network and
    keeps whatever is installed; `MOKURO_BUNKO_BIN` skips the build entirely.
    """
    override = os.environ.get("MOKURO_BUNKO_BIN", "").strip()
    if override:
        path = Path(override).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise BunkoAgentError(f"MOKURO_BUNKO_BIN={path} is not an executable file")
        return InstallInfo(
            binary=str(path), commit="external", label="MOKURO_BUNKO_BIN",
            built_at=_now(), source="MOKURO_BUNKO_BIN",
        )

    saved = installed()
    offline = os.environ.get("MOKURO_BUNKO_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}
    if offline:
        if saved is None:
            raise BunkoAgentError("MOKURO_BUNKO_OFFLINE=1 but no mokuro-bunko build is installed")
        return saved

    ref_value = (ref or os.environ.get("MOKURO_BUNKO_REF", "") or BUNKO_DEV_REF).strip()
    target_commit = resolve_ref(ref_value, log=log)
    if saved is not None and saved.commit == target_commit and not force:
        return saved

    if os.name == "nt":
        raise BunkoAgentError(
            "mokuro-bunko 0.8 has no release binary yet, and the automatic source build "
            "is Unix-only. Build it with cargo (cargo build --release --no-default-features "
            "-p mokuro-bunko) and set MOKURO_BUNKO_BIN to the binary."
        )
    if shutil.which("cargo") is None or shutil.which("git") is None:
        raise BunkoAgentError(
            "mokuro-bunko 0.8 is not released yet, so the bridge builds it from source: "
            "install git and the Rust toolchain (https://rustup.rs), or set "
            "MOKURO_BUNKO_BIN to an existing 0.8 binary."
        )

    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    source = home / "source"
    cargo_home = home / "cargo"
    target_dir = home / "target"
    build_log = home / "build.log"
    if force:
        shutil.rmtree(source, ignore_errors=True)
    if not (source / "Cargo.toml").is_file():
        source.parent.mkdir(parents=True, exist_ok=True)
        clone = ["git", "clone", "--filter=blob:none", "--no-checkout", BUNKO_REPO, str(source)]
        if runner(clone, cwd=home, log=build_log) != 0:
            raise BunkoAgentError(f"git clone failed; see {build_log}\n{_tail(build_log)}")
    # Fetch the ref, not the raw commit: GitHub serves a branch tip reliably even
    # when it will not serve an arbitrary object. A branch that moved between the
    # ls-remote and the fetch supplies its newer head and is built instead, which
    # is exactly what following a development branch means.
    pinned = value_is_commit(ref_value)
    fetch_ref = target_commit if pinned else ref_value
    fetch = ["git", "-C", str(source), "fetch", "--depth", "1", "origin", fetch_ref]
    if runner(fetch, cwd=home, log=build_log) != 0:
        raise BunkoAgentError(
            f"git fetch of {fetch_ref} failed; see {build_log}\n{_tail(build_log)}"
        )
    verify = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "FETCH_HEAD"],
        capture_output=True, text=True, check=False,
    )
    actual = verify.stdout.strip() if verify.returncode == 0 else ""
    if not actual:
        raise BunkoAgentError("could not read the fetched commit")
    if pinned and actual != target_commit:
        raise BunkoAgentError(
            f"the fetched commit {actual[:9]} does not match the pinned {target_commit[:9]}"
        )
    target_commit = actual
    checkout = ["git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"]
    if runner(checkout, cwd=home, log=build_log) != 0:
        raise BunkoAgentError(f"git checkout failed; see {build_log}\n{_tail(build_log)}")

    env = dict(os.environ)
    env["CARGO_HOME"] = str(cargo_home)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    env["CARGO_TERM_COLOR"] = "never"
    old = f" (replacing {saved.commit[:9]})" if saved is not None and saved.commit != target_commit else ""
    log(f"Building mokuro-bunko {target_commit[:9]}{old}; the first build takes several minutes…")
    log(f"Build log: {build_log}")
    build = [
        "cargo", "build", "--release", "--no-default-features",
        "-p", "mokuro-bunko", "--locked",
    ]
    if runner(build, cwd=source, env=env, log=build_log) != 0:
        raise BunkoAgentError(f"cargo build failed; see {build_log}\n{_tail(build_log)}")

    built = target_dir / "release" / ("mokuro-bunko.exe" if os.name == "nt" else "mokuro-bunko")
    if not built.is_file():
        raise BunkoAgentError(f"cargo reported success but {built} does not exist")
    target = binary_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, target)
    target.chmod(0o755)
    info = InstallInfo(
        binary=str(target), commit=target_commit,
        label=f"{BUNKO_BUILD_LABEL} {target_commit[:9]}",
        built_at=_now(), source=str(source),
    )
    (data_home() / INSTALL_FILE).write_text(json.dumps(asdict(info), indent=2), encoding="utf-8")
    return info


# ---- configuration -----------------------------------------------------------


def config_yaml(
    *,
    base_path: Path,
    port: int,
    backend: str,
    folder: str = BUNKO_LIBRARY_FOLDER,
    webdav: Optional[dict] = None,
) -> str:
    """The minimal Bunko config for a cloud library with OCR on this machine off.

    `serve_readers: false` because readers open the cloud library directly; OCR
    is `skip`/false because the bridge uploads an already OCR'd volume, and
    Bunko's own compiler needs no model download.
    """
    lines = [
        "# Written by mokuro-bridge --setup-bunko.",
        "# The bridge runs Bunko in cloud mode: it compiles series.json/catalog.json",
        "# in the cloud folder and leaves OCR to the bridge.",
        "server:",
        '  host: "127.0.0.1"',
        f"  port: {int(port)}",
        "  serve_readers: false",
        "storage:",
        f"  base_path: {json.dumps(str(base_path))}",
        "  library:",
        f"    backend: {backend}",
        f"    folder: {json.dumps(folder)}",
    ]
    if backend == "webdav" and webdav:
        lines.extend([
            "    webdav:",
            f"      url: {json.dumps(str(webdav.get('url', '')))}",
            f"      username: {json.dumps(str(webdav.get('username', '')))}",
            f"      allow_http: {str(bool(webdav.get('allow_http', False))).lower()}",
        ])
    lines.extend([
        "ocr:",
        '  backend: "skip"',
        "  local_processing: false",
        "update:",
        "  auto: false",
    ])
    return "\n".join(lines) + "\n"


def _admin_credentials() -> tuple[str, str]:
    return "bridge-admin", secrets.token_urlsafe(18)


def ensure_config(state: AgentState) -> None:
    path = Path(state.config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        raise BunkoAgentError(f"Bunko config is missing: {path}")


def ensure_admin(state: AgentState, *, runner: Callable[..., int] = _run) -> None:
    """Create the local admin user on first setup (Bunko needs one to run)."""
    if state.admin_user:
        return
    if (Path(state.storage) / "mokuro.db").is_file():
        state.admin_user = "bridge-admin"
        return
    user, password = _admin_credentials()
    code = runner(
        [state.binary, "-c", state.config, "admin", "add-user", user, "--role", "admin", "--password", password],
        log=Path(state.log),
    )
    if code != 0:
        raise BunkoAgentError(f"could not create the Bunko admin user; see {state.log}\n{_tail(Path(state.log))}")
    state.admin_user = user


# ---- control API -------------------------------------------------------------


def control_file(storage: Path) -> Optional[dict]:
    try:
        data = json.loads((storage / ".control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("port"):
        return None
    return data


def wait_for_control(storage: Path, *, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = control_file(storage)
        if data:
            return data
        time.sleep(0.25)
    raise BunkoAgentError(f"Bunko did not write {storage / '.control.json'} within {timeout:.0f}s")


def _control_client(control: dict, client_factory=None):
    import httpx

    headers = {"Authorization": f"Bearer {control.get('token', '')}"}
    if client_factory is not None:
        return client_factory(control.get("port", 0), headers)
    return httpx.Client(
        base_url=f"http://127.0.0.1:{int(control['port'])}",
        headers=headers,
        timeout=120.0,
        trust_env=False,
    )


def _json_or_error(response, what: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        message = body.get("error") if isinstance(body, dict) else ""
        raise BunkoAgentError(f"{what}: HTTP {response.status_code} {message or response.text[:300]}")
    return body if isinstance(body, dict) else {}


def connect_payload(state: AgentState, *, totp: Optional[str] = None) -> dict:
    """Build Bunko's `/app/api/library/connect` body for the bridge's account."""
    provider = _PROVIDER_BACKEND.get(method_provider(state.method) or "")
    if provider is None:
        raise BunkoAgentError(f"mokuro-bunko has no remote storage backend for {state.method}")
    payload: dict = {
        "provider": provider,
        "folder": state.folder,
        "flow": None,
        "use_this_account": False,
    }
    instance = providers.instance_for(state.method)
    if instance is None:
        raise BunkoAgentError(f"no account behind {state.method}")
    name = instance.name
    if instance.provider == "rclone":
        from . import rclone_gateway

        try:
            gateway = rclone_gateway.ensure(name)
        except rclone_gateway.RcloneGatewayError as error:
            raise BunkoAgentError(str(error)) from error
        payload["webdav"] = gateway.bunko_payload()
        return payload
    if provider == "mega":
        from .providers.mega import _get_mega_creds

        try:
            email, password = _get_mega_creds(name)
        except (RuntimeError, OSError) as error:
            raise BunkoAgentError(str(error)) from error
        totp = (totp or os.environ.get("MOKURO_BUNKO_MEGA_TOTP", "")).strip()
        if not totp and sys.stdin.isatty():
            try:
                totp = input("MEGA two-factor code (leave empty if the account has none): ").strip()
            except EOFError:
                totp = ""
        payload["mega"] = {"email": email, "password": password, "totp": totp or None}
    elif provider == "webdav":
        try:
            from .providers import webdav as webdav_provider
        except ImportError as error:
            raise BunkoAgentError("the WebDAV provider is not available in this build") from error
        payload["webdav"] = {
            "url": webdav_provider.base_url(name),
            "username": webdav_provider.username(name),
            "password": webdav_provider.password(name),
            "allow_http": False,
        }
    else:
        # Drive and OneDrive are OAuth: Bunko has to run its own sign-in. Pick
        # the flow the CLI would: browser on a desktop, a code elsewhere.
        payload["flow"] = "browser"
        payload["reader"] = os.environ.get("MOKURO_BUNKO_GOOGLE_PROJECT", "release")
    return payload


def finish_connect(
    client,
    *,
    payload: dict,
    create: bool = True,
    on_state: Optional[Callable[[dict], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 600.0,
) -> dict:
    """Drive Bunko's connect session to `connected`, creating the folder if asked."""
    body = _json_or_error(client.post("/app/api/library/connect", json=payload), "Bunko connect")
    session = body.get("session")
    deadline = time.monotonic() + timeout
    last_state = None
    asked_account = False
    while time.monotonic() < deadline:
        now = _json_or_error(client.get("/app/api/library/connect"), "Bunko connect status")
        state = str(now.get("state", ""))
        if state != last_state and on_state is not None:
            on_state(now)
        last_state = state
        if state == "connected":
            if now.get("check", {}).get("found") is False and create:
                _json_or_error(client.post("/app/api/library/connect/create"), "Bunko create folder")
            return now
        if state in {"failed", "stopped", "cancelled"}:
            raise BunkoAgentError(
                str(now.get("error") or now.get("message") or f"Bunko sign-in {state}")
            )
        if state == "account-mismatch" and not asked_account:
            asked_account = True
            _json_or_error(
                client.post("/app/api/library/connect/account", json={"use_this_account": True}),
                "Bunko account switch",
            )
        if state == "paste":
            raise BunkoAgentError(
                "Bunko's sign-in needs a pasted redirect address, which this automatic "
                "setup cannot supply. Run `mokuro-bridge --setup-bunko " + payload.get("provider", "") +
                "` in a terminal, or use MEGA or WebDAV."
            )
        if now.get("session") not in (None, session):
            raise BunkoAgentError("another Bunko sign-in replaced this one")
        sleep(1.0)
    raise BunkoAgentError(f"Bunko sign-in did not finish within {timeout:.0f}s")


# ---- process and orchestration ----------------------------------------------


def start_server(state: AgentState, *, runner: Callable[..., subprocess.Popen] = subprocess.Popen) -> AgentState:
    if _pid_alive(state.pid):
        return state
    log = Path(state.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("ab")
    env = dict(os.environ)
    env.update({
        "MOKURO_OCR_BACKEND": "skip",
        "MOKURO_OCR_LOCAL_PROCESSING": "false",
        "MOKURO_SERVE_READERS": "false",
        "MOKURO_CONFIG": state.config,
    })
    try:
        process = runner(
            [state.binary, "serve"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    finally:
        handle.close()
    state.pid = int(process.pid)
    return state


def stop_server(state: AgentState, *, timeout: float = 10.0) -> None:
    pid = state.pid
    if not _pid_alive(pid):
        state.pid = 0
        return
    try:
        os.kill(pid, 15)
    except OSError:
        state.pid = 0
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if _pid_alive(pid):
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    state.pid = 0


def status_line(state: Optional[AgentState] = None) -> str:
    state = state or load_state()
    if state is None:
        return "mokuro-bunko: not configured (run mokuro-bridge --setup-bunko)"
    running = _pid_alive(state.pid)
    parts = [
        f"mokuro-bunko: {'running' if running else 'stopped'}",
        f"backend {state.backend}",
        f"folder {state.folder}",
    ]
    if state.pid:
        parts.append(f"pid {state.pid}")
    if state.connected_at:
        parts.append(f"connected {state.connected_at}")
    return ", ".join(parts)


def _folder_for(method: str) -> str:
    root = method_root(method) or BUNKO_LIBRARY_FOLDER
    return root.rstrip("/").rsplit("/", 1)[-1] or BUNKO_LIBRARY_FOLDER


def setup(
    method: Optional[str] = None,
    *,
    log: Callable[[str], None] = print,
    install_runner: Callable[..., int] = _run,
    start_runner: Callable[..., subprocess.Popen] = subprocess.Popen,
    client_factory=None,
    create: bool = True,
    wait_for_marker: bool = True,
    totp: Optional[str] = None,
) -> AgentState:
    """Install, configure, sign in and start Bunko for one bridge destination."""
    method = (method or providers._default_upload_method()).strip()
    provider_id = method_provider(method)
    backend = _PROVIDER_BACKEND.get(provider_id or "")
    if backend is None:
        raise BunkoAgentError(
            "mokuro-bunko remote storage needs a cloud destination; "
            f"'{method}' is local or unknown. Configure a MEGA, Drive, OneDrive "
            "or WebDAV account and pass its method id."
        )
    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    folder = _folder_for(method)
    port = free_port()
    # Check the destination's credentials before the multi-minute build: an
    # unconfigured account should fail in a second, not after compiling Bunko.
    state = AgentState(
        method=method, backend=backend, folder=folder,
        storage=str(storage_dir()), config=str(config_path()), log=str(log_path()),
        binary="", commit="", port=port,
    )
    payload = connect_payload(state, totp=totp)
    info = install(log=log, runner=install_runner)
    state = replace(state, binary=info.binary, commit=info.commit)
    config = config_yaml(
        base_path=storage_dir(),
        port=port,
        backend=backend,
        folder=folder,
        webdav=payload.get("webdav"),
    )
    config_path().write_text(config, encoding="utf-8")
    ensure_admin(state, runner=install_runner)
    log(f"Starting mokuro-bunko on 127.0.0.1:{port} (log: {state.log})…")
    state = start_server(state, runner=start_runner)
    control = wait_for_control(storage_dir())
    client = _control_client(control, client_factory=client_factory)
    try:
        log(f"Connecting mokuro-bunko to {backend} ({method})…")
        finish_connect(client, payload=payload, create=create, on_state=_print_connect_state(log))
        state.connected_at = _now()
        state.save()
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    if wait_for_marker:
        _wait_for_marker(state, log=log)
    state.save()
    return state


def _print_connect_state(log: Callable[[str], None]):
    def show(value: dict) -> None:
        state = value.get("state", "")
        if state == "code":
            log(
                f"Open {value.get('verification_url', '?')} and enter code "
                f"{value.get('user_code', '?')} to sign in to Bunko."
            )
        elif state == "browser":
            log(f"Open this address to sign in to Bunko: {value.get('auth_url', '?')}")
        elif state in {"checking", "connected"}:
            log(f"Bunko sign-in: {state}")
    return show


def _wait_for_marker(state: AgentState, *, log: Callable[[str], None] = print) -> bool:
    instance = providers.instance_for(state.method)
    if instance is None:
        return False
    provider = providers._PROVIDERS.get(instance.provider)
    if provider is None:
        return False
    root = instance.root_path
    deadline = time.monotonic() + MARKER_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            raw = provider.download(instance.name, root, bunko.MARKER_NAME)
        except Exception:
            raw = None
        marker = bunko.parse_marker(raw) if raw else None
        if marker and marker.manages(bunko.SERIES_FILE_NAME):
            log("mokuro-bunko owns the library; the bridge will leave series.json to it.")
            return True
        time.sleep(2.0)
    log(
        "mokuro-bunko is running and signed in; the cloud marker appears after its "
        "first sync, which can take a while on a large library. The bridge writes "
        "series.json until the marker is there."
    )
    return False


def ensure_running(*, log: Callable[[str], None] = print) -> Optional[AgentState]:
    """Start the configured Bunko if it is not running (called by the bridge)."""
    state = load_state()
    if state is None:
        return None
    if _pid_alive(state.pid):
        return state
    if not Path(state.binary).is_file():
        return state
    try:
        state = start_server(state)
        state.save()
    except OSError as error:
        log(f"mokuro-bunko did not start: {error}")
    return state


def update(
    *,
    log: Callable[[str], None] = print,
    install_runner: Callable[..., int] = _run,
    start_runner: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> tuple[InstallInfo, bool]:
    """Move the installed build to the newest development commit.

    Returns (installed info, whether the commit changed). A running instance is
    restarted onto the new binary; a stopped one stays stopped.
    """
    state = load_state()
    before = state.commit if state is not None else ""
    info = install(log=log, runner=install_runner)
    changed = info.commit != before
    if state is not None and changed:
        was_running = _pid_alive(state.pid)
        if was_running:
            stop_server(state)
        state.binary = info.binary
        state.commit = info.commit
        if was_running:
            state = start_server(state, runner=start_runner)
        state.save()
    return info, changed
