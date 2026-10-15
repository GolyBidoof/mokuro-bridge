"""Persisted configuration for the wizard, with environment variables winning.

The bridge reads its settings from the environment, which is right for a
service but useless for a dashboard: a launchd plist cannot be edited from a
web page. A JSON file next to the accounts directory (override with
MOKURO_BRIDGE_SETTINGS_FILE) holds the values the wizard writes, and every
reader asks in this order: environment, then file, then built-in default.

Keys are environment variable names, so the file stays hand-editable and a
value's provenance is always answerable. Values read at import time (the
proxy port count, the OCR chunk size) need a restart to take effect; the
snapshot says so per key.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

SETTINGS_FILE = Path(
    os.environ.get("MOKURO_BRIDGE_SETTINGS_FILE", "").strip()
    or Path.home() / ".config" / "mokuro-bridge" / "settings.json"
).expanduser()

SPECS: tuple[dict, ...] = (
    {
        "env": "MOKURO_BRIDGE_FETCH_PORTS",
        "label": "Proxy ports",
        "help": "Extra localhost ports the downloader fetches pages through. "
        "Each is a separate browser origin worth 6 more sockets. 0 disables.",
        "type": "int", "min": 0, "max": 256, "default": 48, "restart": True,
    },
    {
        "env": "MOKURO_BRIDGE_FETCH_CONCURRENCY",
        "label": "Upstream fetch concurrency",
        "help": "Simultaneous upstream fetches the proxy may make. 0 means no cap.",
        "type": "int", "min": 0, "max": 64, "default": 0, "restart": True,
    },
    {
        "env": "MOKURO_BRIDGE_OUTPUT_DIR",
        "label": "Default local output folder",
        "help": "Where a local finalize writes <series>/<volume>.{cbz,mokuro,webp} "
        "and series.json when no folder has been chosen for a volume.",
        "type": "path", "default": "", "restart": True,
    },
    {
        "env": "MOKURO_BRIDGE_INGEST_ROOTS",
        "label": "Extra folder read roots",
        "help": "Comma-separated extra roots the bridge may read a finished "
        "volume folder from, beyond your home directory.",
        "type": "csv", "default": "", "restart": True,
    },
    {
        "env": "OCR_CHUNK_SIZE",
        "label": "OCR chunk size",
        "help": "Pages batched into one recognition pass before the queue flushes.",
        "type": "int", "min": 1, "max": 256, "default": 8, "restart": True,
    },
    {
        "env": "OCR_IDLE_FLUSH_S",
        "label": "OCR idle flush",
        "help": "Seconds a partially filled chunk waits before it is processed.",
        "type": "float", "min": 0.0, "max": 60.0, "default": 1.5, "restart": True,
    },
    {
        "env": "MIN_PAGES_FOR_MEGA",
        "label": "Minimum pages for a remote upload",
        "help": "A remote finalize below this page count is refused as a failed "
        "scrape; a local one is not.",
        "type": "int", "min": 1, "max": 500, "default": 1, "restart": True,
    },
)

_BY_ENV = {spec["env"]: spec for spec in SPECS}

_cache: Optional[dict] = None


def load(*, refresh: bool = False) -> dict:
    global _cache
    if _cache is not None and not refresh:
        return _cache
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        _cache = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _cache = {}
    return _cache


def stored(env: str) -> Optional[str]:
    """The value the settings file holds for one env key, or None."""
    value = load().get(env)
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value)
    return text if text.strip() else None


def value(env: str, default: Any = None) -> str:
    """The setting in force as raw text: environment, then file, then default.

    Callers parse it (`config._knob` hands it to the typed helpers), so this
    stays text on every path rather than being an int here and a string there.
    """
    raw = os.environ.get(env)
    if raw is not None and str(raw).strip():
        return str(raw)
    stored_value = stored(env)
    if stored_value is not None:
        return stored_value
    spec = _BY_ENV.get(env)
    fallback = default if default is not None else (spec or {}).get("default")
    return "" if fallback is None else str(fallback)


def source(env: str) -> str:
    raw = os.environ.get(env)
    if raw is not None and str(raw).strip():
        return "environment"
    if stored(env) is not None:
        return "settings file"
    return "default"


def _coerce(spec: dict, raw: Any) -> Any:
    kind = spec["type"]
    if kind in ("int", "float"):
        number = float(str(raw).strip())
        if kind == "int" and number != int(number):
            raise ValueError("must be a whole number")
        low, high = spec.get("min", 0), spec.get("max")
        if number < low or (high is not None and number > high):
            raise ValueError(f"must be between {low} and {high}")
        return int(number) if kind == "int" else number
    if kind == "path":
        return str(Path(str(raw)).expanduser())
    if kind == "csv":
        return ",".join(part.strip() for part in str(raw).split(",") if part.strip())
    return str(raw).strip()


def update(values: dict) -> tuple[dict, list[str]]:
    """Validate and persist wizard changes. Returns (saved, problems)."""
    saved: dict = {}
    problems: list[str] = []
    for env, raw in values.items():
        spec = _BY_ENV.get(env)
        if spec is None:
            problems.append(f"unknown setting: {env}")
            continue
        if str(raw).strip() == "":
            saved[env] = ""  # an explicit clear falls back to the default
            continue
        try:
            saved[env] = _coerce(spec, raw)
        except (TypeError, ValueError) as error:
            problems.append(f"{spec['label']}: {error}")
    if problems:
        return {}, problems
    merged = dict(load(refresh=True))
    merged.update({env: value for env, value in saved.items() if value != ""})
    for env, value in saved.items():
        if value == "":
            merged.pop(env, None)
    write(merged)
    return saved, []


def write(data: dict) -> None:
    global _cache
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".settings-", dir=str(SETTINGS_FILE.parent))
    temporary = Path(name)
    try:
        try:
            os.fchmod(handle, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, SETTINGS_FILE)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    _cache = data


def snapshot() -> list[dict]:
    """One row per setting, for the dashboard."""
    rows = []
    for spec in SPECS:
        current = value(spec["env"], spec["default"])
        rows.append(
            {
                "env": spec["env"],
                "label": spec["label"],
                "help": spec["help"],
                "type": spec["type"],
                "value": current,
                "display": _display(spec, current),
                "default": spec["default"],
                "source": source(spec["env"]),
                "restart": spec["restart"],
                "min": spec.get("min"),
                "max": spec.get("max"),
                "effective": spec["env"] not in load(),
            }
        )
    return rows


def _display(spec: dict, current: Any) -> str:
    if spec["type"] == "csv":
        return ", ".join(str(current).split(",")) if current else "(none)"
    if spec["type"] in ("int", "float"):
        return str(current)
    return str(current) if str(current).strip() else "(not set)"
