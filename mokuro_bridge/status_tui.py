"""Live terminal status display for a running mokuro-bridge.

``render_status`` is pure and TTY-free; ``run_status`` owns the terminal and restores it in a ``finally``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import threading
import unicodedata
import urllib.parse
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from . import APP_NAME, __version__

try:  # wcwidth knows emoji and width-ambiguous characters; fall back to unicodedata
    from wcwidth import wcwidth as _wcwidth
except Exception:  # pragma: no cover - exercised where wcwidth is not installed
    _wcwidth = None

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
HOME = "\x1b[H"
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"

DEFAULT_URL = "http://127.0.0.1:62642"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "62642"
# Below this width the compact body is used; at and above it the full body must
# fit without dropping destinations or the OCR worker state (see _pipeline_rows).
NARROW_WIDTH = 46
FOOTER = "q quit   r refresh   c dashboard   ? help"
FOOTER_SHORT = "q quit  ? help"
FETCH_TIMEOUT_S = 3.0
HELP_TEXT = (
    "\n  mokuro-bridge status\n"
    "\n  q quit    r refresh    c dashboard URL    ? this help\n"
    "\n  ★  default destination\n"
    "  ●  destination configured\n"
    "  ○  destination not configured\n"
    "\n  Settings, destination folders and accounts are edited in the web\n"
    "  dashboard the bridge serves (press c for its address).\n"
    "\n  press any key to return\n"
)
DASHBOARD_TEXT = (
    "\n  dashboard: {url}\n"
    "  Settings, destination folders and accounts are edited in that page.\n"
    "  press any key to return, q to quit\n"
)


# --- display width ---------------------------------------------------------


def _char_width(char):
    if _wcwidth is not None:
        width = _wcwidth(char)
        return width if width > 0 else 0
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    """Terminal columns *text* occupies (wide characters count 2, combining marks 0)."""
    return sum(_char_width(char) for char in str(text))


def _take_width(text, limit):
    """Longest prefix of *text* within *limit* columns, never splitting a combining mark."""
    if limit <= 0:
        return ""
    text = str(text)
    used = 0
    end = 0
    for index, char in enumerate(text):
        width = _char_width(char)
        if used + width > limit:
            break
        used += width
        end = index + 1
    return text[:end]


def _text(value, default="-"):
    if value is None or value == "":
        return default
    return str(value)


def _yes(value, default="-"):
    if value is None:
        return default
    return "yes" if value else "no"


def _int(value, default="-"):
    if value is None or isinstance(value, bool):
        return default
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return default


def _seconds(value, default="-"):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return f"{value:g}s"


def _worker_state(value, default="-"):
    """The OCR worker's own state word, never its raw metrics dict."""
    if not isinstance(value, dict):
        return default
    state = value.get("state") or ("started" if value.get("started") else "idle")
    return str(state)


def _clip(text, limit):
    """Truncate display text to *limit* columns, with an ellipsis when it does not fit."""
    if limit <= 0:
        return ""
    text = _text(text, "")
    if display_width(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return _take_width(text, limit - 1) + "…"


def _pad(text, width):
    """Clip *text* to *width* columns, then pad with spaces to exactly *width*."""
    width = max(0, int(width))
    piece = _clip(text, width)
    return piece + " " * max(0, width - display_width(piece))


def _bytes_human(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(value)
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    return f"{number:.0f} {units[index]}" if index == 0 else f"{number:.1f} {units[index]}"


def _percent(upload):
    pct = upload.get("percent")
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        return max(0.0, min(100.0, float(pct)))
    total = upload.get("total_bytes")
    current = upload.get("current_bytes")
    if isinstance(total, (int, float)) and total and isinstance(current, (int, float)):
        return max(0.0, min(100.0, float(current) / float(total) * 100))
    return 0.0


def _bar(pct, width):
    width = max(1, width)
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def _update_text(health):
    state = _text(health.get("update_check"), "unknown")
    if state == "disabled":
        return "checks disabled"
    if state == "pending":
        return "checking…"
    if state == "error":
        return f"check failed ({_text(health.get('update_error'), 'error')})"
    if health.get("update_available"):
        return f"update available: {_text(health.get('latest_version'), '?')}"
    return f"up to date ({_text(health.get('latest_version'), _text(health.get('version')))})"


def _pill(health):
    status = _text(health.get("status"), "-")
    if health.get("error") or status.lower() in ("unreachable", "error"):
        reason = health.get("error")
        return (f"● unreachable: {_text(reason)}" if reason else "● unreachable"), RED
    if health.get("busy"):
        return f"● busy · {_text(health.get('busy_stage'))}", YELLOW
    return f"● {status}", GREEN if status == "ok" else YELLOW


def _now_text(health):
    if health.get("busy") or health.get("busy_stage"):
        stage = _text(health.get("busy_stage"), "busy")
        detail = health.get("busy_detail")
        return stage if detail in (None, "") else f"{stage} · {detail}"
    return "idle"


# --- URLs ------------------------------------------------------------------


def _normalize_base_url(value):
    """One URL shape for display and fetching: scheme once, no trailing slash, IPv6 bracketed."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    scheme, _, rest = text.partition("://")
    authority, slash, path = rest.partition("/")
    if authority and ":" in authority and not authority.startswith("[") and authority.count(":") >= 2:
        authority = f"[{authority}]"
    tail = "/" + path.rstrip("/") if slash and path.strip("/") else ""
    return f"{scheme}://{authority}{tail}"


def _url_for(host, port):
    """Build a base URL from a host/port pair, preserving an explicit scheme and bracketing IPv6."""
    host = str(host or "").strip()
    port = str(port or "").strip()
    if "://" in host:
        parts = urlsplit(host)
        scheme = parts.scheme or "http"
        netloc = parts.netloc
        if port and parts.port is None:
            netloc = f"{netloc}:{port}"
        path = "/" + parts.path.strip("/") if parts.path.strip("/") else ""
        return urlunsplit((scheme, netloc, path, "", ""))
    if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        host, host_port = host.rsplit(":", 1)
        port = host_port
    if not host:
        host = DEFAULT_HOST
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port or DEFAULT_PORT}"


def _host_url(host_header):
    if not host_header:
        return ""
    return _normalize_base_url(host_header)


def _env_url():
    host = os.environ.get("MOKURO_BRIDGE_HOST")
    port = os.environ.get("MOKURO_BRIDGE_PORT")
    if not host and not port:
        return DEFAULT_URL
    return _url_for(host, port)


def _no_color():
    if os.environ.get("NO_COLOR"):
        return True
    return os.environ.get("TERM", "").strip() == "dumb"


# --- layout helpers --------------------------------------------------------


def _trim(segments, width):
    out = []
    left = width
    for text, style in segments:
        if left <= 0:
            break
        piece = _take_width(text, left)
        out.append((piece, style))
        left -= display_width(piece)
    return out


def _fit_line(segments, width):
    segments = _trim(segments, width)
    used = sum(display_width(text) for text, _ in segments)
    if used < width:
        segments = segments + [(" " * (width - used), None)]
    return segments


def _top_border(width, title, url, pill, pill_style):
    """Top rule with the status pill, an optional URL and an always-present ``┐``."""
    if width <= 0:
        return []
    if width < 4:
        return [("─" * width, DIM)]
    if width < 10:
        return [("┌" + "─" * (width - 2) + "┐", DIM)]
    reserve = 2  # "─┐"
    budget = width - reserve
    pill_text = f" {pill} "
    pill_room = min(display_width(pill_text), max(4, budget // 2))
    right = [(_clip(pill_text, pill_room), pill_style)]
    if url:
        url_text = f" {url} "
        url_room = min(display_width(url_text), max(4, width // 4))
        url_piece = _clip(url_text, url_room)
        if display_width(url_piece) + 1 < budget - display_width(right[0][0]):
            right.insert(0, (url_piece, None))
    right_w = sum(display_width(text) for text, _ in right)
    head_room = budget - right_w - 1  # keep at least one dash between the sides
    if head_room < 4 and len(right) > 1:
        right = right[-1:]
        right_w = display_width(right[0][0])
        head_room = budget - right_w - 1
    if head_room < 4:
        pill_room = max(2, budget - 5)
        right = [(_clip(pill_text, pill_room), pill_style)]
        right_w = display_width(right[0][0])
        head_room = budget - right_w - 1
    title_room = max(0, head_room - 4)
    left = [("┌─ ", DIM), (_clip(title, title_room), BOLD), (" ", None)]
    used = 4 + display_width(left[1][0]) + right_w
    fill = max(1, width - used - reserve)
    return _fit_line(left + [("─" * fill, DIM)] + right + [("─┐", DIM)], width)


def _bottom_border(width):
    """Bottom rule; keeps the quit hint and an always-present ``┘``."""
    if width <= 0:
        return []
    if width < 4:
        return [("─" * width, DIM)]
    if width < 10:
        return [("└" + "─" * (width - 2) + "┘", DIM)]
    budget = width - 2
    footer = FOOTER if budget >= display_width(FOOTER) + 5 else FOOTER_SHORT
    if 3 + display_width(footer) + 1 <= budget:
        left = [("└─ ", DIM), (footer, None), (" ", None)]
    else:
        left = [("└─ ", DIM), (_clip(footer, max(0, budget - 3)), None)]
    used = sum(display_width(text) for text, _ in left)
    fill = max(0, width - used - 2)
    return _fit_line(left + [("─" * fill, DIM), ("─┘", DIM)], width)


def _row(segments, inner):
    out = [("│ ", DIM)]
    left = inner
    for text, style in segments:
        if left <= 0:
            break
        piece = _clip(text, left)
        out.append((piece, style))
        left -= display_width(piece)
    if left > 0:
        out.append((" " * left, None))
    out.append((" │", DIM))
    return out


_USAGE_LOCK = threading.Lock()
_USAGE_CACHE: dict[str, tuple[float, dict]] = {}
_USAGE_INFLIGHT: set[str] = set()
_USAGE_TTL_S = 300.0


def _format_usage(data):
    if not isinstance(data, dict) or not data.get("available"):
        return ""
    about = data.get("about") or {}
    folder = data.get("folder") or {}
    parts = []
    if about.get("total"):
        parts.append(
            f"{_bytes_human(about.get('used'))} used · {_bytes_human(about.get('free'))} free"
        )
    if isinstance(folder.get("bytes"), (int, float)):
        parts.append(f"folder {_bytes_human(folder.get('bytes'))}")
    return " · ".join(parts)


def _prefetch_usage(base_url, health, fetch):
    """Refresh usage in the background so a slow cloud never stalls the frame."""
    if fetch is not _http_get_json:
        return
    methods = health.get("upload_methods")
    if not isinstance(methods, list):
        return
    now = time.monotonic()
    ids = ["local"] + [
        method.get("id") for method in methods
        if isinstance(method, dict) and method.get("id") and method.get("id") != "local"
    ]
    for method_id in ids:
        if not method_id:
            continue
        cached = _USAGE_CACHE.get(method_id)
        if cached is not None and now - cached[0] < _USAGE_TTL_S:
            continue
        with _USAGE_LOCK:
            if method_id in _USAGE_INFLIGHT:
                continue
            _USAGE_INFLIGHT.add(method_id)

        def load(method_id=method_id):
            try:
                data = fetch(
                    base_url.rstrip("/")
                    + "/config/usage?method="
                    + urllib.parse.quote(method_id, safe="")
                )
            except Exception:
                data = None
            with _USAGE_LOCK:
                _USAGE_INFLIGHT.discard(method_id)
                if isinstance(data, dict):
                    _USAGE_CACHE[method_id] = (time.monotonic(), data)

        threading.Thread(target=load, name=f"usage-{method_id}", daemon=True).start()


def _usage_text(method_id):
    cached = _USAGE_CACHE.get(method_id)
    return _format_usage(cached[1]) if cached is not None else ""


def _destination_rows(health, inner):
    methods = health.get("upload_methods")
    if not isinstance(methods, list) or not methods:
        return [[("  -", None)]]
    name_width = max(12, min(30, inner - 30))
    default_id = health.get("upload_method_default")
    rows = []
    for method in methods:
        if not isinstance(method, dict):
            continue
        star = "★" if method.get("id") == default_id else " "
        dot = "●" if method.get("configured") else "○"
        name = _pad(method.get("name") or method.get("id"), name_width)
        path = (
            method.get("root")
            or method.get("library_root")
            or method.get("current_folder")
            or method.get("email")
            or "-"
        )
        detail = _usage_text(method.get("id"))
        email = str(method.get("email") or "")
        if email and "·" not in email and email not in path:
            detail = f"{email} · {detail}" if detail else email
        suffix = f"  {detail}" if detail else ""
        rows.append([(f"  {star} {dot} {name}  {_clip(path + suffix, inner - name_width - 8)}", None)])
    return rows or [[("  -", None)]]


def _default_destination(health):
    methods = health.get("upload_methods")
    default_id = health.get("upload_method_default")
    if not isinstance(methods, list):
        return "-"
    for method in methods:
        if not isinstance(method, dict):
            continue
        if method.get("id") == default_id or (not default_id and method.get("default")):
            return _text(method.get("name") or method.get("id"))
    return "-"


def _upload_line(upload, inner):
    pct = _percent(upload)
    bar_width = max(8, min(26, inner - 60))
    name_width = max(12, inner - bar_width - 42)
    text = (
        f"  {_bar(pct, bar_width)} {pct:3.0f}%  {_clip(upload.get('file'), name_width)}"
        f"  {_bytes_human(upload.get('current_bytes'))} / {_bytes_human(upload.get('total_bytes'))}"
        f"  {_text(upload.get('speed_human'))}"
    )
    return (text, None)


def _pipeline_rows(health, inner):
    """Queue metrics; wraps instead of hiding the worker state word at mid widths."""
    queue = (
        f"  queue {_int(health.get('ocr_queue_depth'))}"
        f"   processing {_int(health.get('ocr_processing_depth'))}"
        f"   sessions {_int(health.get('active_sessions'))}"
        f"   chunk {_int(health.get('ocr_chunk_size'))}"
        f"   idle-flush {_seconds(health.get('ocr_idle_flush_s'))}"
        f"   worker {_worker_state(health.get('ocr_worker'))}"
    )
    if display_width(queue) <= inner:
        return [[(queue, None)]]
    first = (
        f"  queue {_int(health.get('ocr_queue_depth'))}"
        f"   processing {_int(health.get('ocr_processing_depth'))}"
        f"   sessions {_int(health.get('active_sessions'))}"
    )
    second = (
        f"  worker {_worker_state(health.get('ocr_worker'))}"
        f"   chunk {_int(health.get('ocr_chunk_size'))}"
        f"   idle-flush {_seconds(health.get('ocr_idle_flush_s'))}"
    )
    return [[(_clip(line, inner), None)] for line in (first, second)]


def _normal_body(health, upload, inner):
    rows = []
    flavor = _text(health.get("mokuro_flavor"))
    mver = health.get("mokuro_version")
    engine = flavor if mver in (None, "") else f"{flavor} {_text(mver)}"
    rows.append([("ENGINE", CYAN)])
    rows.append([(f"  engine     {_clip(engine, inner - 13)}", None)])
    rows.append([(f"  tools      mokuro {_yes(health.get('mokuro_installed'))}"
                  f"   megatools {_yes(health.get('megatools_installed'))}", None)])
    rows.append([(f"  update     {_clip(_update_text(health), inner - 13)}", None)])
    bunko = health.get("bunko")
    if isinstance(bunko, dict):
        commit = str(bunko.get("commit") or "")[:8] or "installed"
        library = str(bunko.get("method") or "-")
        state_text = "running" if bunko.get("running") else "stopped"
        rows.append([(f"  bunko      {commit} {state_text} · library on {library}", None)])
        if bunko.get("update_available"):
            rows.append([("  bunko      update available: run mokuro-bridge --update-bunko", None)])
    else:
        rows.append([("  bunko      not installed", None)])
    if health.get("mokuro_installed") is False:
        rows.append([("  paused     OCR needs mokuro: run mokuro-bridge --install-mokuro", None)])
    if not isinstance(bunko, dict):
        rows.append([("  paused     uploads need mokuro-bunko: run mokuro-bridge --setup-bunko", None)])

    rows.append([])
    rows.append([("DESTINATIONS", CYAN)])
    rows.extend(_destination_rows(health, inner))

    rows.append([])
    rows.append([("PIPELINE", CYAN)])
    rows.extend(_pipeline_rows(health, inner))
    if isinstance(upload, dict):
        rows.append([_upload_line(upload, inner)])

    ports = health.get("fetchProxyPorts")
    port_count = len(ports) if isinstance(ports, (list, tuple)) else 0
    upstreams = health.get("fetchUpstreams")
    upstream_count = len(upstreams) if isinstance(upstreams, (list, tuple)) else 0
    rows.append([])
    rows.append([("FETCH PROXY", CYAN),
                 (
                     f"  {port_count} ports  (+{6 * port_count} browser sockets)  "
                     f"{upstream_count} upstream{'s' if upstream_count != 1 else ''}",
                     None,
                 )])

    rows.append([])
    rows.append([("NOW", CYAN), (f"  {_clip(_now_text(health), inner - 6)}", None)])
    return rows


def _narrow_body(health, upload, inner):
    """Compact body: status, engine, queue + worker, default destination, activity."""
    rows = [
        [(f"status   {_text(health.get('status'))}", None)],
        [(f"engine   {_text(health.get('mokuro_flavor'))}", None)],
        [(f"queue    {_int(health.get('ocr_queue_depth'))} / {_int(health.get('ocr_processing_depth'))}"
          f"   worker {_worker_state(health.get('ocr_worker'))}", None)],
        [(f"output   {_clip(_default_destination(health), inner - 11)}", None)],
        [(f"now      {_now_text(health)}", None)],
    ]
    if isinstance(upload, dict):
        rows.insert(4, [(f"upload   {_percent(upload):3.0f}%  "
                         f"{_clip(upload.get('file'), inner - 16)}", None)])
    return rows


def _render(lines, width, height, color):
    out = []
    for segments in lines[:height]:
        segments = _fit_line(segments, width)
        if color:
            out.append("".join(f"{style or ''}{text}{RESET if style else ''}" for text, style in segments))
        else:
            out.append("".join(text for text, _ in segments))
    while len(out) < height:
        out.append(" " * width)
    return "\n".join(out)


def render_status(health: dict, *, width: int = 100, height: int = 30, color: bool = False,
                  upload: dict | None = None, host_header: str | None = None) -> str:
    """Render one complete frame of *height* lines, each exactly *width* columns."""
    health = health if isinstance(health, dict) else {}
    if width <= 0 or height <= 0:
        return ""
    width, height = int(width), int(height)
    inner = max(1, width - 4)
    title = f"{_text(health.get('app'), APP_NAME)} {_text(health.get('version'), __version__)}"
    pill, pill_style = _pill(health)
    if width < NARROW_WIDTH:
        body = _narrow_body(health, upload, inner)
    else:
        body = _normal_body(health, upload, inner)
    capacity = max(0, height - 2)
    rows = [_row(row, inner) for row in body[:capacity]]
    rows += [_row([], inner)] * (capacity - len(rows))
    lines = [_top_border(width, title, _host_url(host_header), pill, pill_style)]
    lines += rows
    lines.append(_bottom_border(width))
    return _render(lines, width, height, color)


# --- polling and terminal ownership ---------------------------------------


def _http_get_json(url):
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_health(fetch, base_url):
    try:
        data = fetch(base_url.rstrip("/") + "/health")
    except Exception as exc:
        return {"status": "unreachable", "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"status": "unreachable", "error": "malformed /health payload"}
    return data


def _key_reader(keys):
    if keys is None:
        return None
    if callable(keys):
        return lambda: keys() or ""
    iterator = iter(keys)

    def _next():
        try:
            return next(iterator)
        except StopIteration:
            return ""

    return _next


def _enter_raw():
    try:
        if os.name == "nt":
            return ("nt",)
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        return ("posix", fd, old)
    except Exception:
        return None


def _leave_raw(state):
    if not state or state[0] != "posix":
        return
    try:
        import termios
        termios.tcsetattr(state[1], termios.TCSADRAIN, state[2])
    except Exception:
        pass


def _read_key(state, reader, timeout):
    if reader is not None:
        key = reader()
        if not key and timeout > 0:
            time.sleep(min(0.05, timeout))
        return key
    if not state:
        return ""
    if state[0] == "nt":
        import msvcrt
        end = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < end:
            if msvcrt.kbhit():
                return msvcrt.getwch()
            time.sleep(0.05)
        return ""
    import select
    ready, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout))
    if not ready:
        return ""
    return sys.stdin.read(1) or ""


def _terminal_size(fallback_width=100, fallback_height=30, *, reserve_last=True):
    size = shutil.get_terminal_size((fallback_width, fallback_height))
    height = size.lines - 1 if reserve_last else size.lines
    return max(20, size.columns), max(6, height)


def _single_frame(stream, health, width, height, host_header):
    stream.write(render_status(health, width=width, height=height,
                               upload=health.get("upload"), host_header=host_header) + "\n")
    stream.flush()


def _pause(stream, state, reader, text):
    """Leave the alt screen, show *text*, and wait up to five seconds for a key."""
    stream.write(ALT_OFF + CURSOR_SHOW + text)
    stream.flush()
    key = _read_key(state, reader, 5.0)
    stream.write(ALT_ON + CURSOR_HIDE)
    stream.flush()
    return key


def run_status(base_url: str, *, interval_s: float = 2.0, stream=None, fetch=None, keys=None) -> int:
    """Poll ``<base_url>/health`` and draw frames until the user presses ``q``.

    ``fetch`` is ``(url) -> dict``; ``keys`` is a no-arg callable or an iterable
    of keys, so tests need neither a socket nor a TTY.
    """
    stream = sys.stdout if stream is None else stream
    fetch = _http_get_json if fetch is None else fetch
    reader = _key_reader(keys)
    interval = max(0.05, float(interval_s))
    base_url = _normalize_base_url(base_url) or DEFAULT_URL
    try:
        interactive = bool(stream.isatty())
    except Exception:
        interactive = False

    if not interactive:
        # A snapshot is full-height: it is not fighting the terminal for space.
        width, height = _terminal_size(reserve_last=False)
        snapshot = _fetch_health(fetch, base_url)
        _prefetch_usage(base_url, snapshot, fetch)
        _single_frame(stream, snapshot, width, height, base_url)
        return 0

    state = None
    if reader is None:
        state = _enter_raw()
        if state is None:
            width, height = _terminal_size()
            snapshot = _fetch_health(fetch, base_url)
            _prefetch_usage(base_url, snapshot, fetch)
            _single_frame(stream, snapshot, width, height, base_url)
            return 0

    color = not _no_color()
    stream.write(ALT_ON + CURSOR_HIDE)
    stream.flush()
    try:
        deadline = 0.0
        while True:
            width, height = _terminal_size()  # re-read each frame so a resize is picked up
            now = time.monotonic()
            if now >= deadline:
                health = _fetch_health(fetch, base_url)
                _prefetch_usage(base_url, health, fetch)
                stream.write(HOME + render_status(health, width=width, height=height, color=color,
                                                  upload=health.get("upload"), host_header=base_url))
                stream.flush()
                deadline = time.monotonic() + interval
            key = _read_key(state, reader, max(0.0, deadline - time.monotonic()))
            if key in ("q", "Q"):
                break
            if key == "r":
                deadline = 0.0
            elif key == "c":
                if _pause(stream, state, reader, DASHBOARD_TEXT.format(url=base_url)) in ("q", "Q"):
                    break
                deadline = 0.0
            elif key == "?":
                if _pause(stream, state, reader, HELP_TEXT) in ("q", "Q"):
                    break
                deadline = 0.0
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        _leave_raw(state)
        stream.write(CURSOR_SHOW + ALT_OFF)
        stream.flush()


def main(argv: list[str] | None = None, *, host=None, port=None,
         status_url: str = "", status_interval: float | None = None) -> int:
    """Entry point for ``mokuro-bridge --status``.

    The CLI passes *host*/*port*/*status_url*/*status_interval* from its own parse so
    ``--status`` honours the same flags as the server; a direct call may pass only
    *argv* and falls back to the environment and ``DEFAULT_URL``.
    """
    parser = argparse.ArgumentParser(prog="mokuro-bridge --status", add_help=False, allow_abbrev=False)
    parser.add_argument("--status-url", default=None)
    parser.add_argument("--status-interval", type=float, default=None)
    args, _ = parser.parse_known_args(argv)
    url = status_url or args.status_url
    if status_interval is not None:
        interval = status_interval
    elif args.status_interval is not None:
        interval = args.status_interval
    else:
        interval = 2.0
    if url:
        base_url = _normalize_base_url(url)
    elif host is not None or port is not None:
        base_url = _url_for(host, port)
    else:
        base_url = _env_url()
    return run_status(base_url, interval_s=interval)
