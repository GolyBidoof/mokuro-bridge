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
import urllib.request

from . import APP_NAME, __version__

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
NARROW_WIDTH = 60
FOOTER = "q quit   r refresh   c config URL"


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
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…" if limit > 1 else "…"


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


def _host_url(host_header):
    if not host_header:
        return ""
    text = str(host_header)
    return text if "://" in text else f"http://{text}"


def _trim(segments, width):
    out = []
    left = width
    for text, style in segments:
        if left <= 0:
            break
        piece = text[:left]
        out.append((piece, style))
        left -= len(piece)
    return out


def _top_border(width, title, url, pill, pill_style):
    head = [("┌─ ", DIM), (_clip(title, max(4, width - 24)), BOLD), (" ", None)]
    tail = []
    if url:
        tail.append((f" {_clip(url, max(0, width // 4))} ", None))
    tail.append((f" {_clip(pill, max(4, width // 2))} ", pill_style))
    tail.append(("─┐", DIM))
    used = sum(len(text) for text, _ in head + tail)
    return _trim(head + [("─" * max(1, width - used), DIM)] + tail, width)


def _bottom_border(width):
    head = [("└─ ", DIM), (FOOTER, None), (" ", None)]
    tail = [("─┘", DIM)]
    used = sum(len(text) for text, _ in head + tail)
    return _trim(head + [("─" * max(1, width - used), DIM)] + tail, width)


def _row(segments, inner):
    out = [("│ ", DIM)]
    left = inner
    for text, style in segments:
        if left <= 0:
            break
        piece = _clip(text, left)
        out.append((piece, style))
        left -= len(piece)
    if left > 0:
        out.append((" " * left, None))
    out.append((" │", DIM))
    return out


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
        name = _clip(method.get("name") or method.get("id"), name_width).ljust(name_width)
        path = (
            method.get("root")
            or method.get("library_root")
            or method.get("current_folder")
            or method.get("email")
            or "-"
        )
        rows.append([(f"  {star} {dot} {name}  {_clip(path, inner - name_width - 8)}", None)])
    return rows or [[("  -", None)]]


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

    rows.append([])
    rows.append([("DESTINATIONS", CYAN)])
    rows.extend(_destination_rows(health, inner))

    rows.append([])
    rows.append([("PIPELINE", CYAN)])
    rows.append([(f"  queue {_int(health.get('ocr_queue_depth'))}"
                  f"   processing {_int(health.get('ocr_processing_depth'))}"
                  f"   sessions {_int(health.get('active_sessions'))}"
                  f"   chunk {_int(health.get('ocr_chunk_size'))}"
                  f"   idle-flush {_seconds(health.get('ocr_idle_flush_s'))}"
                  f"   worker {_worker_state(health.get('ocr_worker'))}", None)])
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


def _narrow_body(health):
    return [
        [(f"status   {_text(health.get('status'))}", None)],
        [(f"engine   {_text(health.get('mokuro_flavor'))}", None)],
        [(f"queue    {_int(health.get('ocr_queue_depth'))} / {_int(health.get('ocr_processing_depth'))}", None)],
        [(f"now      {_now_text(health)}", None)],
    ]


def _render(lines, width, height, color):
    out = []
    for segments in lines[:height]:
        segments = _trim(segments, width)
        visible = sum(len(text) for text, _ in segments)
        if visible < width:
            segments = segments + [(" " * (width - visible), None)]
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
    body = _narrow_body(health) if width < NARROW_WIDTH else _normal_body(health, upload, inner)
    capacity = max(0, height - 2)
    rows = [_row(row, inner) for row in body[:capacity]]
    rows += [_row([], inner)] * (capacity - len(rows))
    lines = [_top_border(width, title, _host_url(host_header), pill, pill_style)]
    lines += rows
    lines.append(_bottom_border(width))
    return _render(lines, width, height, color)


def _http_get_json(url):
    with urllib.request.urlopen(url, timeout=10) as response:
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


def _terminal_size(fallback_width=100, fallback_height=30):
    size = shutil.get_terminal_size((fallback_width, fallback_height))
    return max(20, size.columns), max(6, size.lines - 1)


def run_status(base_url: str, *, interval_s: float = 2.0, stream=None, fetch=None, keys=None) -> int:
    """Poll ``<base_url>/health`` and draw frames until the user presses ``q``.

    ``fetch`` is ``(url) -> dict``; ``keys`` is a no-arg callable or an iterable
    of keys, so tests need neither a socket nor a TTY.
    """
    stream = sys.stdout if stream is None else stream
    fetch = _http_get_json if fetch is None else fetch
    reader = _key_reader(keys)
    interval = max(0.05, float(interval_s))
    try:
        interactive = bool(stream.isatty())
    except Exception:
        interactive = False

    width, height = _terminal_size()
    if not interactive:
        frame = _fetch_health(fetch, base_url)
        stream.write(render_status(frame, width=width, height=height, host_header=base_url) + "\n")
        stream.flush()
        return 0

    state = None
    if reader is None:
        state = _enter_raw()
        if state is None:
            frame = _fetch_health(fetch, base_url)
            stream.write(render_status(frame, width=width, height=height, host_header=base_url) + "\n")
            stream.flush()
            return 0

    stream.write(ALT_ON + CURSOR_HIDE)
    stream.flush()
    try:
        deadline = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                health = _fetch_health(fetch, base_url)
                stream.write(HOME + render_status(health, width=width, height=height, color=True,
                                                  host_header=base_url))
                stream.flush()
                deadline = time.monotonic() + interval
            key = _read_key(state, reader, deadline - time.monotonic())
            if key == "q":
                break
            if key == "r":
                deadline = 0.0
            elif key == "c":
                stream.write(ALT_OFF + CURSOR_SHOW + f"\n  dashboard: {base_url}\n  press any key…")
                stream.flush()
                _read_key(state, reader, 5.0)
                stream.write(ALT_ON + CURSOR_HIDE)
                stream.flush()
                deadline = 0.0
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        _leave_raw(state)
        stream.write(CURSOR_SHOW + ALT_OFF)
        stream.flush()


def _env_url():
    if "MOKURO_BRIDGE_HOST" not in os.environ and "MOKURO_BRIDGE_PORT" not in os.environ:
        return DEFAULT_URL
    host = os.environ.get("MOKURO_BRIDGE_HOST") or "127.0.0.1"
    port = os.environ.get("MOKURO_BRIDGE_PORT") or "62642"
    return f"http://{host}:{port}"


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``mokuro-bridge --status``; URL/interval come from argv or the environment."""
    parser = argparse.ArgumentParser(prog="mokuro-bridge --status", add_help=False, allow_abbrev=False)
    parser.add_argument("--status-url", default=None)
    parser.add_argument("--status-interval", type=float, default=2.0)
    args, _ = parser.parse_known_args(argv)
    return run_status(args.status_url or _env_url(), interval_s=args.status_interval)
