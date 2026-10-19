"""Recognise a mokuro-bunko instance that compiles a library's metadata.

mokuro-bunko writes `<Series>/series.json` and the root `catalog.json` from the
library itself, so a bridge target it manages must not have its manifest written
by this process. Two independent signals identify such a target:

* **The cloud marker.** Bunko 0.8 remote storage writes
  `<library root>/.mokuro-bunko.json` while it manages a library and rewrites it
  every `ttl_s / 3`. The marker names the files it compiles. This is the same
  protocol the reader uses to stop writing metadata
  (bunko `docs/design/0.8-remote-storage.md` section 9.5).
* **The health fingerprint.** A bunko 0.7 server answers `GET /api/health` with
  a body carrying `library_status`, `total_volumes` and an `ocr` object. There is
  no marker before 0.8, so a WebDAV destination pointed at a 0.7 server is
  recognised from that answer instead.

Everything here is fail-open: an unreadable, stale, released or unknown marker
means "the bridge still owns series.json". The alternative, skipping the file
for a library no one compiles, would leave a series invisible in the reader.

The marker name, field names and freshness rules are a protocol, not copied
code: bunko is MPL-2.0 and this project is MIT, so only the documented interface
is implemented here.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

MARKER_NAME = ".mokuro-bunko.json"
MARKER_KIND = "mokuro-bunko"
MARKER_VERSION = 1
SERIES_FILE_NAME = "series.json"

# Bunko rewrites the marker every ttl_s/3 and considers a node stale after one
# ttl. The reader gives a cold start ttl_s + 5 min of grace for clock skew, and
# this module uses the same cold-start rule because it does not track marker
# versions across finalizes.
HEARTBEAT_GRACE_SECONDS = 5 * 60

DEFAULT_TTL_SECONDS = 900
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 60 * 60

ACTIVE_STATE = "active"

_HEALTHY_STATES = {"ok", "degraded", "error", "unavailable"}


def _parse_iso_milliseconds(value: Any) -> Optional[float]:
    """Unix seconds for an ISO-8601 timestamp, or None when it is not one."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _coerce_ttl(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, seconds))


@dataclass(frozen=True)
class Marker:
    """One parsed `.mokuro-bunko.json` marker."""

    kind: str
    version: int = MARKER_VERSION
    state: str = ACTIVE_STATE
    ttl_s: int = DEFAULT_TTL_SECONDS
    heartbeat_at: str = ""
    compiles: tuple[str, ...] = ()
    instance_id: str = ""
    instance_name: str = ""

    @property
    def ttl(self) -> int:
        return self.ttl_s

    def heartbeat_at_seconds(self) -> Optional[float]:
        return _parse_iso_milliseconds(self.heartbeat_at)

    def fresh(self, now: Optional[float] = None) -> bool:
        """Whether a cold-start reader would treat this marker as live."""
        if self.state != ACTIVE_STATE:
            return False
        heartbeat = self.heartbeat_at_seconds()
        if heartbeat is None:
            return False
        current = time.time() if now is None else now
        return current - heartbeat <= self.ttl_s + HEARTBEAT_GRACE_SECONDS

    def manages(self, filename: str = SERIES_FILE_NAME, now: Optional[float] = None) -> bool:
        """Whether this marker says another program compiles ``filename``."""
        return self.fresh(now) and filename in self.compiles


def parse_marker(raw: Any) -> Optional[Marker]:
    """Parse a marker body; None when it is not a bunko marker.

    Lenient on purpose, matching bunko's own reader: unknown fields are ignored
    and a missing ``state`` reads as active. Nothing is accepted without the
    bunko ``kind``.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != MARKER_KIND:
        return None
    version = payload.get("version", MARKER_VERSION)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = MARKER_VERSION
    compiles_value = payload.get("compiles")
    compiles = (
        tuple(str(item) for item in compiles_value if isinstance(item, str))
        if isinstance(compiles_value, list)
        else ()
    )
    return Marker(
        kind=MARKER_KIND,
        version=version,
        state=str(payload.get("state", ACTIVE_STATE) or ACTIVE_STATE).strip().lower(),
        ttl_s=_coerce_ttl(payload.get("ttl_s", DEFAULT_TTL_SECONDS)),
        heartbeat_at=str(payload.get("heartbeat_at", "") or ""),
        compiles=compiles,
        instance_id=str(payload.get("instance_id", "") or ""),
        instance_name=str(payload.get("instance_name", "") or ""),
    )


def is_bunko_health(payload: Any) -> bool:
    """Whether a `/api/health` JSON body is a mokuro-bunko answer.

    The shape is bunko-specific: a library status, volume count and OCR block.
    A generic WebDAV server, or a reverse proxy error page, does not produce it.
    """
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("ocr"), dict):
        return False
    if "library_status" not in payload:
        return False
    if not isinstance(payload.get("total_volumes"), (int, float)) and "total_volumes" not in payload:
        return False
    status = str(payload.get("status", "") or "").strip().lower()
    return status in _HEALTHY_STATES or "db_status" in payload


def probe_marker(
    download: Callable[[str, str, str], Optional[bytes]],
    name: str,
    root: str,
    *,
    now: Optional[float] = None,
) -> bool:
    """Read the marker at ``root`` and answer whether bunko owns series.json.

    ``download`` is a provider's sidecar reader: (account name, remote dir,
    filename) -> bytes or None. Errors propagate so the caller can decide the
    fallback; the provider helper treats them as "not bunko".
    """
    raw = download(name, root, MARKER_NAME)
    if raw is None:
        return False
    marker = parse_marker(raw)
    return bool(marker and marker.manages(SERIES_FILE_NAME, now=now))
