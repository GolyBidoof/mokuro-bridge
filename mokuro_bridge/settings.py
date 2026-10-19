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
        "label": "Faster downloads in the browser",
        "help": "Opens extra local ports so the page downloader can fetch more "
        "pages at once. 0 turns the extra speed off.",
        "type": "int", "min": 0, "max": 256, "default": 48, "restart": True,
        "group": "Downloading",
    },
    {
        "env": "MOKURO_BRIDGE_FETCH_CONCURRENCY",
        "label": "Pages fetched at the same time",
        "help": "How many pages the accelerator requests at once. 0 means no "
        "cap; lower it if the store's CDN starts refusing or throttling a large "
        "burst.",
        "type": "int", "min": 0, "max": 64, "default": 0, "restart": True,
        "group": "Downloading",
    },
    {
        "env": "MOKURO_BRIDGE_UPDATE_CHECK",
        "label": "Check for updates",
        "help": "Asks GitHub whether a newer release exists and shows it in the "
        "footer. Nothing is downloaded or installed. Turn it off on an offline "
        "machine; this takes effect immediately.",
        "type": "bool", "default": True, "restart": False,
        "group": "Updates",
    },
    {
        "env": "OCR_BATCH_SIZE",
        "label": "Pages the OCR engine reads at once",
        "help": "Bigger batches keep a fast machine busier and a small one "
        "slower: lower it when memory is tight. It applies after a restart.",
        "type": "int", "min": 1, "max": 256, "default": 32, "restart": True,
        "group": "Reading pages",
    },
    {
        "env": "MOKURO_BRIDGE_OUTPUT_DIR",
        "label": "Where finished volumes are saved on this computer",
        "help": "Each series gets its own folder here. Leave it empty to use the "
        "default folder next to the install. Only used when no client names a "
        "folder, and each destination card's Change folder wins over it.",
        "type": "path", "default": "", "restart": True,
        "group": "Advanced",
    },
    {
        "env": "MOKURO_BRIDGE_INGEST_ROOTS",
        "label": "Other folders the bridge may read",
        "help": "Only needed if a library lives outside your home folder, for "
        "example on another drive. Separate several with commas.",
        "type": "csv", "default": "", "restart": True,
        "group": "Advanced",
    },
)

# Settings that exist but are not on the page: OCR batch tuning and the
# minimum-page guard are set from the environment (see .env.example) rather
# than offered to someone configuring their uploads for the first time.
_ENV_ONLY = (
    "OCR_CHUNK_SIZE",
    "OCR_IDLE_FLUSH_S",
    "MIN_PAGES_FOR_MEGA",
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


def env_pinned(env: str) -> bool:
    """True when a non-empty environment variable owns this setting right now."""
    raw = os.environ.get(env)
    return raw is not None and str(raw).strip() != ""


def pinned_keys(values: dict) -> list[str]:
    """The keys in `values` that the environment currently owns.

    `update` skips these keys silently rather than returning an error, because
    the environment wins at read time anyway and one pinned key must not stop
    the other, editable keys from being saved. A caller that wants to say so
    (the dashboard does) can report this list beside the saved keys.
    """
    return [env for env in values if env_pinned(env)]


def _absolute_path(text: str) -> Path:
    """Parse one path value and require it to be absolute.

    Raises ValueError with a user-facing sentence, never a bare OSError.
    """
    value = str(text).strip()
    if "\x00" in value:
        raise ValueError("must not contain a NUL byte")
    try:
        candidate = Path(value).expanduser()
    except (OSError, ValueError) as error:
        raise ValueError(f"is not a usable path ({error})") from error
    if not candidate.is_absolute():
        raise ValueError(
            "must be an absolute path, for example /Users/you/Books/mokuro"
        )
    return candidate


def _output_dir_value(raw: Any) -> str:
    """A folder that the bridge can create and write at the next start.

    An existing directory is accepted. A path that does not exist yet is
    accepted only when its parent exists, is a directory and is writable,
    because config.py does `OUTPUT_DIR.mkdir(parents=True, exist_ok=True)` at
    import time and a path pointing at an existing *file* makes that line raise
    FileExistsError and the whole bridge fail to start.
    """
    candidate = _absolute_path(raw)
    try:
        if candidate.is_symlink() and not candidate.exists():
            raise ValueError(
                "is a broken link; choose a folder that exists"
            )
        if candidate.exists():
            if not candidate.is_dir():
                raise ValueError(
                    "is a file, not a folder; choose a folder or a new name "
                    "inside an existing folder"
                )
            return str(candidate)
        parent = candidate.parent
        if not parent.exists() or not parent.is_dir():
            raise ValueError(
                "cannot be created because its parent folder does not exist"
            )
        if not os.access(parent, os.W_OK):
            raise ValueError(
                "cannot be created because its parent folder is not writable"
            )
    except OSError as error:
        raise ValueError(f"cannot be checked ({error})") from error
    return str(candidate)


def _csv_value(raw: Any) -> str:
    """A comma list whose every entry is an absolute path.

    config.py silently drops relative ingest roots, so accepting one here would
    report a saved setting that never takes effect. Entries are kept as the
    user wrote them (with `~` unexpanded) and expanded by the reader.
    """
    kept: list[str] = []
    for part in str(raw).split(","):
        entry = part.strip()
        if not entry:
            continue
        _absolute_path(entry)
        kept.append(entry)
    return ",".join(kept)


def _bool_value(raw: Any) -> str:
    """Normalise a switch to ``"true"`` or ``"false"``.

    A checkbox sends "true"/"false"; a hand-edited file may hold a boolean, 1/0
    or on/off, and all of those must keep working. Anything else is rejected
    rather than silently read as off.
    """
    if isinstance(raw, bool):
        return "true" if raw else "false"
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return "true"
    if text in ("0", "false", "no", "off"):
        return "false"
    raise ValueError("must be on or off")


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
        return _output_dir_value(raw)
    if kind == "csv":
        return _csv_value(raw)
    if kind == "bool":
        return _bool_value(raw)
    return str(raw).strip()


def update(values: dict) -> tuple[dict, list[str]]:
    """Validate and persist wizard changes. Returns (saved, problems).

    `problems` holds only rejected values, and when it is non-empty nothing is
    written, as before. Keys whose environment variable is set and non-empty
    are skipped before validation: they are left out of `saved` and their
    stored file value is left untouched, because the environment wins at read
    time and a page or curl request must never be able to clobber the value
    that is kept for the day the variable is unset. `pinned_keys(values)` names
    those skipped keys so a caller can report them without turning the save
    into an error.
    """
    saved: dict = {}
    problems: list[str] = []
    for env, raw in values.items():
        spec = _BY_ENV.get(env)
        if spec is None:
            problems.append(f"unknown setting: {env}")
            continue
        if env_pinned(env):
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
    if not saved:
        return {}, []
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
        env = spec["env"]
        env_set = env_pinned(env)
        current = value(env, spec["default"])
        rows.append(
            {
                "env": env,
                "label": spec["label"],
                "help": spec["help"],
                "type": spec["type"],
                "group": spec.get("group", "Preferences"),
                "value": current,
                "display": _display(spec, current),
                "default": spec["default"],
                "source": source(env),
                # A row the environment pins cannot be changed from the page: the
                # file is written, but the environment keeps winning until it is
                # unset. Say so instead of reporting a change that never applies.
                "env_set": env_set,
                "pinned": env_set,
                "file_value": stored(env),
                "restart": spec["restart"],
                "min": spec.get("min"),
                "max": spec.get("max"),
            }
        )
    return rows


def _display(spec: dict, current: Any) -> str:
    if spec["type"] == "bool":
        return "On" if str(current).strip().lower() in ("1", "true", "yes", "on") else "Off"
    if spec["type"] == "csv":
        return ", ".join(str(current).split(",")) if current else "(none)"
    if spec["type"] in ("int", "float"):
        return str(current)
    return str(current) if str(current).strip() else "(not set)"
