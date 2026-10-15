"""Write and merge the reader's `<Series>/series.json`.

The format is Gnathonic's mokuro-reader (pinned in data/series_json_pin.json,
documented in docs/series-json.md). Values come from docs, not from guesswork:
the merge identity, the validators and the wire order are the reader's.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any, Optional

SERIES_FILE_NAME = "series.json"

# A file the bridge writes has no series facts, so it must not carry a write
# clock either: this stamp loses every facts comparison, as upstream intends.
FACTLESS_UPDATED_AT = "1970-01-01T00:00:00.000Z"

FUTURE_TOLERANCE_MS = 5 * 60 * 1000
SPINE_OFFSET_LIMIT = 50
VOLUME_OFFSET_LIMIT = 500
TITLE_KEYS = ("native", "romaji", "english")
ID_KEYS = ("anilist", "mal")
TRACKING_UNITS = ("volumes", "chapters")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The code points upstream counts as Japanese, probed from its regex and
# recorded in tests/fixtures/series_json/upstream_parity.json.
_JAPANESE_RANGES: tuple[tuple[int, int], ...] = (
    (9675, 9675), (9711, 9711), (11904, 11929), (11931, 12019), (12032, 12245),
    (12293, 12295), (12321, 12329), (12344, 12347), (12353, 12438), (12445, 12447),
    (12449, 12538), (12540, 12543), (12784, 12799), (13008, 13054), (13056, 13143),
    (13312, 19903), (19968, 40959), (63744, 64109), (64112, 64217), (65382, 65391),
    (65393, 65437), (94178, 94179), (94192, 94198), (110576, 110579),
    (110581, 110587), (110589, 110590), (110592, 110882), (110898, 110898),
    (110928, 110930), (110933, 110933), (110948, 110951), (127488, 127488),
    (131072, 173791), (173824, 178205), (178208, 183981), (183984, 191456),
    (191472, 192093), (194560, 195101), (196608, 201546), (201552, 210041),
)
_RANGE_STARTS = tuple(start for start, _end in _JAPANESE_RANGES)


def is_japanese_char(char: str) -> bool:
    code = ord(char)
    index = bisect_right(_RANGE_STARTS, code) - 1
    return index >= 0 and code <= _JAPANESE_RANGES[index][1]


def count_chars_in_lines(lines: Any) -> int:
    if not isinstance(lines, list):
        return 0
    return sum(
        1
        for line in lines
        if isinstance(line, str)
        for char in line
        if is_japanese_char(char)
    )


def build_page_char_counts(pages: Any) -> int:
    if not isinstance(pages, list):
        return 0
    total = 0
    for page in pages:
        blocks = page.get("blocks") if isinstance(page, dict) else None
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict):
                    total += count_chars_in_lines(block.get("lines"))
    return total


def _utf16_units(text: str):
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            code -= 0x10000
            yield 0xD800 + (code >> 10)
            yield 0xDC00 + (code & 0x3FF)
        else:
            yield code


def deterministic_uuid(value: str) -> str:
    """Upstream `generateDeterministicUUID`, over UTF-16 units."""
    hash1 = 5381
    hash2 = 52711
    for unit in _utf16_units(value.lower().strip()):
        hash1 = (((hash1 * 33) & 0xFFFFFFFF) ^ unit) & 0xFFFFFFFF
        hash2 = (((hash2 * 33) & 0xFFFFFFFF) ^ unit) & 0xFFFFFFFF
    hex1 = f"{hash1:08x}"
    hex2 = f"{hash2:08x}"
    hash3 = f"{hash1 ^ hash2:08x}"
    hash4 = f"{(hash1 + hash2) & 0xFFFFFFFF:08x}"
    variant = 8 + (int(hash3[0], 16) % 4)
    return f"{hex1}-{hex2[:4]}-4{hex2[5:8]}-{variant:x}{hash3[1:4]}-{hash3[4:]}{hash4[:4]}"


def normalize_series_key(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip()).lower()


def normalize_volume_title_key(title: str) -> str:
    return normalize_series_key(unicodedata.normalize("NFC", title))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_archive_size(value: Any) -> bool:
    return _is_int(value) and value > 0


def _is_epoch_seconds(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _clamp(value: float, limit: int) -> float:
    return min(float(limit), max(float(-limit), float(value)))


def sanitize_external_ids(value: Any) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {k: raw[k] for k in ID_KEYS if _is_int(raw.get(k)) and raw[k] > 0}


def sanitize_titles(value: Any) -> dict:
    raw = value if isinstance(value, dict) else {}
    return {
        k: raw[k]
        for k in TITLE_KEYS
        if isinstance(raw.get(k), str) and raw[k].strip()
    }


def sanitize_synonyms(value: Any) -> list:
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, str) and s.strip() != ""]


def sanitize_tag(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def sanitize_tracking_unit(value: Any) -> Optional[str]:
    return value if value in TRACKING_UNITS else None


def sanitize_spine_offset(value: Any) -> Optional[float]:
    return _clamp(value, SPINE_OFFSET_LIMIT) if _is_number(value) else None


def sanitize_volume_offset(value: Any) -> Optional[float]:
    return _clamp(value, VOLUME_OFFSET_LIMIT) if _is_number(value) else None


def normalize_updated_at(value: Any, *, now_ms: Optional[float] = None) -> Optional[str]:
    """Canonical ISO 8601, clamped when further ahead than the tolerance."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if now_ms is None:
        now_ms = time.time() * 1000.0
    if parsed.timestamp() * 1000.0 > now_ms + FUTURE_TOLERANCE_MS:
        parsed = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    millis = f"{parsed.microsecond // 1000:03d}"
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + millis + "Z"


def order_volume_entry_fields(entry: dict) -> dict:
    """Canonical wire order, with `offset` last."""
    ordered = {
        "volume_uuid": entry["volume_uuid"],
        "volume_title": entry["volume_title"],
        "page_count": entry["page_count"],
        "character_count": entry["character_count"],
        "mokuro_version": entry["mokuro_version"],
    }
    for key in (
        "spine_width", "archive_size", "mokuro_size", "mokuro_modified",
        "mokuro_sha256", "cover_size", "cover_modified", "offset",
    ):
        if entry.get(key) is not None:
            ordered[key] = entry[key]
    return ordered


def has_measured_content(entry: dict) -> bool:
    return (
        entry.get("page_count", 0) > 0
        or entry.get("character_count", 0) > 0
        or entry.get("mokuro_version", "") != ""
    )


def is_metadata_less_entry(series_title: str, entry: dict) -> bool:
    return not has_measured_content(entry) and entry["volume_uuid"] == deterministic_uuid(
        f"{series_title}/{entry['volume_title']}"
    )


def parse_volume_entry(value: Any) -> Optional[dict]:
    """Validate one entry; a bad one is dropped, never the file."""
    if not isinstance(value, dict):
        return None
    volume_uuid = value.get("volume_uuid")
    volume_title = value.get("volume_title")
    page_count = value.get("page_count")
    character_count = value.get("character_count")
    mokuro_version = value.get("mokuro_version")
    if not isinstance(volume_uuid, str) or not volume_uuid.strip():
        return None
    if not isinstance(volume_title, str) or not volume_title.strip():
        return None
    if not _is_int(page_count) or page_count < 0:
        return None
    if not _is_int(character_count) or character_count < 0:
        return None
    if not isinstance(mokuro_version, str):
        return None
    entry = {
        "volume_uuid": volume_uuid,
        "volume_title": volume_title,
        "page_count": page_count,
        "character_count": character_count,
        "mokuro_version": mokuro_version,
    }
    if _is_number(value.get("spine_width")) and value["spine_width"] > 0:
        entry["spine_width"] = value["spine_width"]
    for key in ("archive_size", "mokuro_size", "cover_size"):
        if _is_archive_size(value.get(key)):
            entry[key] = value[key]
    for key in ("mokuro_modified", "cover_modified"):
        if _is_epoch_seconds(value.get(key)):
            entry[key] = value[key]
    sha = value.get("mokuro_sha256")
    if isinstance(sha, str) and _SHA256_RE.match(sha.lower()):
        entry["mokuro_sha256"] = sha.lower()
    offset = sanitize_volume_offset(value.get("offset"))
    if offset:
        entry["offset"] = offset
    return entry


def volume_entry(
    *,
    volume_uuid: str,
    volume_title: str,
    page_count: int,
    character_count: int,
    mokuro_version: str,
    archive_size: Optional[int] = None,
    mokuro_size: Optional[int] = None,
    mokuro_sha256: Optional[str] = None,
    cover_size: Optional[int] = None,
) -> dict:
    entry = {
        "volume_uuid": volume_uuid,
        "volume_title": volume_title,
        "page_count": page_count,
        "character_count": character_count,
        "mokuro_version": mokuro_version,
    }
    for key, value in (
        ("archive_size", archive_size),
        ("mokuro_size", mokuro_size),
        ("cover_size", cover_size),
    ):
        if _is_archive_size(value):
            entry[key] = value
    if isinstance(mokuro_sha256, str) and _SHA256_RE.match(mokuro_sha256.lower()):
        entry["mokuro_sha256"] = mokuro_sha256.lower()
    return order_volume_entry_fields(entry)


def entry_from_mokuro(
    data: Any,
    *,
    volume_title: str,
    archive_size: Optional[int] = None,
    mokuro_size: Optional[int] = None,
    mokuro_sha256: Optional[str] = None,
    cover_size: Optional[int] = None,
) -> Optional[dict]:
    """Measure one volume. The title is the archive stem, never the mokuro's."""
    if not isinstance(data, dict):
        return None
    volume_uuid = data.get("volume_uuid")
    if not isinstance(volume_uuid, str) or not volume_uuid.strip():
        return None
    pages = data.get("pages")
    pages = pages if isinstance(pages, list) else []
    version = data.get("version")
    return volume_entry(
        volume_uuid=volume_uuid,
        volume_title=volume_title,
        page_count=len(pages),
        character_count=build_page_char_counts(pages),
        mokuro_version=version if isinstance(version, str) else "",
        archive_size=archive_size,
        mokuro_size=mokuro_size,
        mokuro_sha256=mokuro_sha256,
        cover_size=cover_size,
    )


# Fields that describe the files beside an entry, so they survive a collapse.
_INHERITED_FILE_FACTS = (
    "archive_size", "mokuro_size", "mokuro_modified",
    "cover_size", "cover_modified", "offset",
)


class VolumeEntryMerger:
    """Upstream's one merge rule: identity by uuid or title key, per-match winner."""

    def __init__(self, series_title: str, title_key=None):
        self._series_title = series_title
        self._title_key = title_key or normalize_volume_title_key
        self._by_uuid: dict[str, dict] = {}
        self._by_title: dict[str, dict] = {}

    def _matches(self, entry: dict) -> list[dict]:
        matches = []
        uuid_match = self._by_uuid.get(entry["volume_uuid"])
        if uuid_match:
            matches.append(uuid_match)
        key = self._title_key(entry["volume_title"])
        title_match = self._by_title.get(key) if key else None
        if title_match and title_match is not uuid_match:
            matches.append(title_match)
        return matches

    def _inherit_file_facts(self, winner: dict, loser: dict) -> dict:
        enriched = dict(winner)
        changed = False
        other_sidecar = (
            winner.get("mokuro_sha256") is not None
            and loser.get("mokuro_sha256") is not None
            and winner["mokuro_sha256"] != loser["mokuro_sha256"]
        )
        for key in _INHERITED_FILE_FACTS:
            if other_sidecar and key in ("mokuro_size", "mokuro_modified"):
                continue
            if enriched.get(key) is None and loser.get(key) is not None:
                enriched[key] = loser[key]
                changed = True
        if enriched.get("mokuro_sha256") is None and loser.get("mokuro_sha256") is not None:
            if (
                enriched.get("mokuro_size") == loser.get("mokuro_size")
                and enriched.get("mokuro_modified") == loser.get("mokuro_modified")
            ):
                enriched["mokuro_sha256"] = loser["mokuro_sha256"]
                changed = True
        return order_volume_entry_fields(enriched) if changed else winner

    def _pick_winner(self, held: dict, incoming: dict) -> dict:
        held_less = is_metadata_less_entry(self._series_title, held)
        incoming_less = is_metadata_less_entry(self._series_title, incoming)
        if held_less != incoming_less:
            return (
                self._inherit_file_facts(incoming, held)
                if held_less
                else self._inherit_file_facts(held, incoming)
            )
        held_content = has_measured_content(held)
        incoming_content = has_measured_content(incoming)
        if held_content != incoming_content:
            return (
                self._inherit_file_facts(held, incoming)
                if held_content
                else self._inherit_file_facts(incoming, held)
            )
        return self._inherit_file_facts(incoming, held)

    def add(self, entry: dict, mode: str) -> None:
        matches = self._matches(entry)
        winner = entry
        for match in matches:
            winner = (
                self._pick_winner(match, winner)
                if mode == "replace"
                else self._pick_winner(winner, match)
            )
        if len(matches) == 1 and winner is matches[0]:
            return
        winner_title_key = self._title_key(winner["volume_title"])
        for match in matches:
            if (
                match["volume_uuid"] != winner["volume_uuid"]
                and self._by_uuid.get(match["volume_uuid"]) is match
            ):
                del self._by_uuid[match["volume_uuid"]]
            key = self._title_key(match["volume_title"])
            if key != winner_title_key and self._by_title.get(key) is match:
                del self._by_title[key]
        self._by_uuid[winner["volume_uuid"]] = winner
        if winner_title_key:
            self._by_title[winner_title_key] = winner

    def values(self) -> list[dict]:
        return list(self._by_uuid.values())


def _natural_key(title: str):
    folded = unicodedata.normalize("NFD", unicodedata.normalize("NFC", title)).casefold()
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", folded)
        if part != ""
    )


def parse(raw: Any) -> Optional[dict]:
    """Validate an untrusted file into the canonical shape, healing on read.

    An unusable `updated_at` is replaced with the factless sentinel rather than
    discarding the file, so a publisher cannot lose the entries inside it.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
    elif isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    if raw.get("version") not in (1, 2):
        return None
    series_title = raw.get("series_title")
    if not isinstance(series_title, str) or not series_title.strip():
        return None
    updated_at = normalize_updated_at(raw.get("updated_at")) or FACTLESS_UPDATED_AT
    volumes: list[dict] = []
    merger = VolumeEntryMerger(series_title, title_key=lambda title: title)
    raw_volumes = raw.get("volumes")
    if isinstance(raw_volumes, list):
        for item in raw_volumes:
            entry = parse_volume_entry(item)
            if entry is not None:
                merger.add(entry, "fill")
        volumes = merger.values()
    return _assemble_file(
        series_title=series_title,
        external_ids=sanitize_external_ids(raw.get("external_ids")),
        titles=sanitize_titles(raw.get("titles")),
        synonyms=sanitize_synonyms(raw.get("synonyms")),
        tag=sanitize_tag(raw.get("tag")),
        unit=sanitize_tracking_unit(raw.get("unit")),
        spine_offset=sanitize_spine_offset(raw.get("spine_offset")),
        updated_at=updated_at,
        volumes=volumes,
    )


def _assemble_file(
    *,
    series_title: str,
    external_ids: dict,
    titles: dict,
    synonyms: list,
    tag: Optional[str],
    unit: Optional[str],
    spine_offset: Optional[float],
    updated_at: str,
    volumes: list[dict],
) -> dict:
    file_obj = {
        "version": 2,
        "series_title": series_title,
        "external_ids": external_ids,
        "titles": titles,
        "synonyms": synonyms,
    }
    if tag:
        file_obj["tag"] = tag
    if unit:
        file_obj["unit"] = unit
    if spine_offset:
        file_obj["spine_offset"] = spine_offset
    file_obj["updated_at"] = updated_at
    file_obj["volumes"] = volumes
    return file_obj


def upsert_entry(existing: Optional[dict], *, series_title: str, entry: dict) -> dict:
    """Publish one volume, keeping the file's facts and its other entries."""
    if existing is None:
        return _assemble_file(
            series_title=series_title,
            external_ids={},
            titles={},
            synonyms=[],
            tag=None,
            unit=None,
            spine_offset=None,
            updated_at=FACTLESS_UPDATED_AT,
            volumes=[order_volume_entry_fields(entry)],
        )
    merger = VolumeEntryMerger(series_title)
    for held in existing.get("volumes", []):
        merger.add(held, "fill")
    merger.add(order_volume_entry_fields(entry), "replace")
    return _assemble_file(
        series_title=series_title,
        external_ids=sanitize_external_ids(existing.get("external_ids")),
        titles=sanitize_titles(existing.get("titles")),
        synonyms=sanitize_synonyms(existing.get("synonyms")),
        tag=sanitize_tag(existing.get("tag")),
        unit=sanitize_tracking_unit(existing.get("unit")),
        spine_offset=sanitize_spine_offset(existing.get("spine_offset")),
        updated_at=existing["updated_at"],
        volumes=sorted(merger.values(), key=lambda item: _natural_key(item["volume_title"])),
    )


def serialize(file_obj: dict) -> bytes:
    return json.dumps(file_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def render(existing_raw: Optional[bytes], *, series_title: str, entry: dict) -> Optional[bytes]:
    """The bytes to publish, or None when the destination is already correct."""
    existing = parse(existing_raw) if existing_raw is not None else None
    content = serialize(upsert_entry(existing, series_title=series_title, entry=entry))
    if existing_raw is not None and content == bytes(existing_raw):
        return None
    return content


def is_series_file_name(name: str) -> bool:
    return name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower() == SERIES_FILE_NAME
