"""Console logging for mokuro-bridge.

A tiny leveled, timestamped logger that writes user-friendly lines to the
server's console (stdout/stderr, which launchd captures into
~/Library/Logs/mokuro-bridge{,.err}.log). Progress lines are throttled so a
busy OCR/upload session doesn't flood the log.
"""
from __future__ import annotations
import sys
import time
from typing import Optional

_LEVEL_ORDER = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_LEVEL = "info"  # configurable via set_level()
_LAST_THROTTLE: dict[str, float] = {}
_THROTTLE_S = 1.0


def set_level(level: str) -> None:
    """Set the minimum level printed: debug|info|warn|error."""
    global _LEVEL
    _LEVEL = level if level in _LEVEL_ORDER else "info"


def _enabled(level: str) -> bool:
    return _LEVEL_ORDER.get(level, 20) >= _LEVEL_ORDER.get(_LEVEL, 20)


def _emit(level: str, tag: str, msg: str) -> None:
    if not _enabled(level):
        return
    stream = sys.stderr if level == "error" else sys.stdout
    ts = time.strftime("%H:%M:%S")
    label = {"debug": "DBG", "info": " • ", "warn": " ⚠ ", "error": " ✗ "}.get(level, " • ")
    try:
        print(f"{ts}{label}[{tag}] {msg}", file=stream, flush=True)
    except Exception:
        pass  # never let logging break the server


def info(tag: str, msg: str) -> None:
    _emit("info", tag, msg)


def debug(tag: str, msg: str) -> None:
    _emit("debug", tag, msg)


def warn(tag: str, msg: str) -> None:
    _emit("warn", tag, msg)


def error(tag: str, msg: str) -> None:
    _emit("error", tag, msg)


def progress(tag: str, msg: str, throttle_s: float = _THROTTLE_S) -> None:
    """Throttled, in-place progress line: each update overwrites the previous
    one on the same line (carriage return + clear-line), so a stream of
    progress doesn't scroll the terminal. Real completion should use info()/
    error(), which print a proper newline-terminated line."""
    if not _enabled("info"):
        return
    now = time.time()
    last = _LAST_THROTTLE.get(tag, 0.0)
    if now - last < throttle_s:
        return
    _LAST_THROTTLE[tag] = now
    try:
        line = f"{time.strftime('%H:%M:%S')} • [{tag}] {msg}"
        print(f"\r\x1b[K{line}", end="", flush=True)
    except Exception:
        pass
