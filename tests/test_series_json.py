"""The series.json port: format, validators and merge rules."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mokuro_bridge import series_json, series_reference

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "series_json" / "upstream_parity.json"
)

SPEC_EXAMPLE = (
    b'{"version":2,"series_title":"Bakemonogatari","external_ids":{},"titles":{},'
    b'"synonyms":[],"updated_at":"1970-01-01T00:00:00.000Z","volumes":'
    b'[{"volume_uuid":"cfb5220c-57db-4008-9f44-e659d794e381","volume_title":"v01",'
    b'"page_count":187,"character_count":13247,"mokuro_version":"0.2.2",'
    b'"archive_size":1234}]}'
)


@pytest.fixture(scope="module")
def parity() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_and_pin_describe_the_same_commit(parity):
    pin = series_reference.load_pin()
    reader = next(u for u in pin["upstream"] if u["repo"] == "Gnathonic/mokuro-reader")
    assert parity["reader_commit"] == reader["commit"], (
        "tests/fixtures/series_json/upstream_parity.json was probed from "
        f"{parity['reader_commit']} but the pin says {reader['commit']}: re-probe the "
        "fixture against the new commit (see docs/series-json.md)"
    )


def test_japanese_codepoint_table_matches_upstream(parity):
    assert tuple(tuple(r) for r in parity["japanese_ranges"]) == series_json._JAPANESE_RANGES


def test_character_count_matches_upstream(parity):
    mismatches = [
        (case["input"], case["count"], series_json.count_chars_in_lines([case["input"]]))
        for case in parity["chars"]
        if series_json.count_chars_in_lines([case["input"]]) != case["count"]
    ]
    assert mismatches == []


def test_deterministic_uuid_matches_upstream(parity):
    mismatches = [
        (case["input"], case["uuid"], series_json.deterministic_uuid(case["input"]))
        for case in parity["uuids"]
        if series_json.deterministic_uuid(case["input"]) != case["uuid"]
    ]
    assert mismatches == []


def test_spec_example_serializes_byte_for_byte():
    entry = series_json.volume_entry(
        volume_uuid="cfb5220c-57db-4008-9f44-e659d794e381",
        volume_title="v01",
        page_count=187,
        character_count=13247,
        mokuro_version="0.2.2",
        archive_size=1234,
    )
    file_obj = series_json.upsert_entry(
        None, series_title="Bakemonogatari", entry=entry
    )
    assert series_json.serialize(file_obj) == SPEC_EXAMPLE


def test_fresh_file_is_factless_and_ordered():
    entry = series_json.volume_entry(
        volume_uuid="abc",
        volume_title="vol 1",
        page_count=3,
        character_count=9,
        mokuro_version="0.2.2",
    )
    file_obj = series_json.upsert_entry(None, series_title="S", entry=entry)
    assert list(file_obj) == [
        "version",
        "series_title",
        "external_ids",
        "titles",
        "synonyms",
        "updated_at",
        "volumes",
    ]
    assert file_obj["updated_at"] == series_json.FACTLESS_UPDATED_AT
    assert file_obj["version"] == 2
    assert list(file_obj["volumes"][0]) == [
        "volume_uuid",
        "volume_title",
        "page_count",
        "character_count",
        "mokuro_version",
    ]


def test_entry_wire_order_puts_offset_last():
    entry = series_json.order_volume_entry_fields(
        {
            "offset": 12,
            "page_count": 1,
            "volume_title": "v",
            "character_count": 2,
            "volume_uuid": "u",
            "mokuro_version": "0.2.2",
            "archive_size": 5,
            "spine_width": 30,
        }
    )
    assert list(entry) == [
        "volume_uuid",
        "volume_title",
        "page_count",
        "character_count",
        "mokuro_version",
        "spine_width",
        "archive_size",
        "offset",
    ]


def test_entry_from_mokuro_measures_pages_and_characters():
    data = {
        "version": "0.2.2",
        "volume_uuid": "uuid-1",
        "title": "Wrong Title",
        "volume": "Wrong Volume",
        "pages": [
            {"blocks": [{"lines": ["こんにちは"]}, {"lines": ["abc", "漢"]}]},
            {"blocks": [{"lines": []}]},
        ],
    }
    entry = series_json.entry_from_mokuro(
        data, volume_title="Volume 1", archive_size=100, mokuro_size=50
    )
    assert entry["volume_title"] == "Volume 1"  # the archive stem, never the mokuro's own
    assert entry["volume_uuid"] == "uuid-1"
    assert entry["page_count"] == 2
    assert entry["character_count"] == 6  # 5 hiragana + 1 kanji
    assert entry["mokuro_version"] == "0.2.2"
    assert entry["archive_size"] == 100
    assert entry["mokuro_size"] == 50


def test_entry_from_mokuro_needs_a_uuid_and_tolerates_a_missing_version():
    assert series_json.entry_from_mokuro({}, volume_title="v") is None
    entry = series_json.entry_from_mokuro(
        {"volume_uuid": "u", "pages": []}, volume_title="v"
    )
    assert entry["mokuro_version"] == ""
    assert entry["page_count"] == 0


@pytest.mark.parametrize(
    "value",
    [
        {"version": 3, "series_title": "S", "updated_at": "2020-01-01T00:00:00.000Z"},
        {"version": 2, "series_title": "  ", "updated_at": "2020-01-01T00:00:00.000Z"},
        "not an object",
        b"{not json",
    ],
)
def test_parse_rejects_unusable_files(value):
    assert series_json.parse(value) is None


def test_parse_revives_an_unusable_stamp_instead_of_dropping_the_file():
    raw = {
        "version": 2,
        "series_title": "S",
        "updated_at": "Aug 16 2020",
        "volumes": [
            {"volume_uuid": "u", "volume_title": "v1", "page_count": 1,
             "character_count": 2, "mokuro_version": ""},
        ],
    }
    parsed = series_json.parse(raw)
    assert parsed is not None
    assert parsed["updated_at"] == series_json.FACTLESS_UPDATED_AT
    assert [entry["volume_uuid"] for entry in parsed["volumes"]] == ["u"]


def test_parse_accepts_version_1_and_drops_bad_entries_individually():
    raw = {
        "version": 1,
        "series_title": "S",
        "updated_at": "2020-01-01T00:00:00.000Z",
        "volumes": [
            {"volume_uuid": "keep", "volume_title": "v1", "page_count": 1,
             "character_count": 2, "mokuro_version": ""},
            {"volume_uuid": "", "volume_title": "v2", "page_count": 1,
             "character_count": 2, "mokuro_version": ""},
            {"volume_uuid": "u3", "volume_title": "v3", "page_count": -1,
             "character_count": 2, "mokuro_version": ""},
            {"volume_uuid": "u4", "volume_title": "v4", "page_count": 1,
             "character_count": 2, "mokuro_version": 7},
        ],
    }
    parsed = series_json.parse(raw)
    assert [entry["volume_uuid"] for entry in parsed["volumes"]] == ["keep"]
    assert parsed["version"] == 2


def test_parse_collapses_doubled_entries_by_exact_title():
    raw = {
        "version": 2,
        "series_title": "S",
        "updated_at": "2020-01-01T00:00:00.000Z",
        "volumes": [
            {
                "volume_uuid": series_json.deterministic_uuid("S/v1"),
                "volume_title": "v1",
                "page_count": 0,
                "character_count": 0,
                "mokuro_version": "",
                "mokuro_size": 10,
            },
            {"volume_uuid": "real", "volume_title": "v1", "page_count": 5,
             "character_count": 50, "mokuro_version": "0.2.2"},
        ],
    }
    parsed = series_json.parse(raw)
    assert len(parsed["volumes"]) == 1
    survivor = parsed["volumes"][0]
    assert survivor["volume_uuid"] == "real"
    assert survivor["page_count"] == 5
    assert survivor["character_count"] == 50
    assert survivor["mokuro_size"] == 10


def test_parse_does_not_fold_case_distinct_titles():
    raw = {
        "version": 2,
        "series_title": "S",
        "updated_at": "2020-01-01T00:00:00.000Z",
        "volumes": [
            {"volume_uuid": "a", "volume_title": "Vol 1", "page_count": 1,
             "character_count": 1, "mokuro_version": ""},
            {"volume_uuid": "b", "volume_title": "VOL 1", "page_count": 1,
             "character_count": 1, "mokuro_version": ""},
        ],
    }
    assert len(series_json.parse(raw)["volumes"]) == 2


def test_parse_drops_unknown_keys_and_keeps_facts():
    raw = {
        "version": 2,
        "series_title": "S",
        "external_ids": {"anilist": 42, "mal": 7, "kitsu": 1},
        "titles": {"native": "題", "english": "  ", "romaji": "Dai"},
        "synonyms": ["alt", "", 3],
        "tag": "  tag  ",
        "unit": "chapters",
        "spine_offset": 999,
        "updated_at": "2020-01-01T00:00:00.000Z",
        "volumes": [],
        "per_user_state": {"read": True},
    }
    parsed = series_json.parse(raw)
    assert parsed["external_ids"] == {"anilist": 42, "mal": 7}
    assert parsed["titles"] == {"native": "題", "romaji": "Dai"}
    assert parsed["synonyms"] == ["alt"]
    assert parsed["tag"] == "tag"
    assert parsed["unit"] == "chapters"
    assert parsed["spine_offset"] == series_json.SPINE_OFFSET_LIMIT
    assert "per_user_state" not in parsed


def test_upsert_preserves_other_volumes_and_facts():
    existing = series_json.parse(
        {
            "version": 2,
            "series_title": "S",
            "titles": {"english": "Series"},
            "tag": "tag",
            "updated_at": "2024-05-05T00:00:00.000Z",
            "volumes": [
                {"volume_uuid": "old", "volume_title": "v1", "page_count": 1,
                 "character_count": 2, "mokuro_version": "0.2.2", "archive_size": 9}
            ],
        }
    )
    entry = series_json.volume_entry(
        volume_uuid="new",
        volume_title="v2",
        page_count=3,
        character_count=4,
        mokuro_version="0.2.2",
    )
    file_obj = series_json.upsert_entry(existing, series_title="S", entry=entry)
    assert file_obj["titles"] == {"english": "Series"}
    assert file_obj["tag"] == "tag"
    assert file_obj["updated_at"] == "2024-05-05T00:00:00.000Z"
    assert [v["volume_title"] for v in file_obj["volumes"]] == ["v1", "v2"]
    assert file_obj["volumes"][0]["archive_size"] == 9


def test_upsert_replaces_the_same_volume_by_title_and_by_uuid():
    existing = series_json.parse(
        {
            "version": 2,
            "series_title": "S",
            "updated_at": "2024-05-05T00:00:00.000Z",
            "volumes": [
                {"volume_uuid": "held-title", "volume_title": "VOL 1",
                 "page_count": 1, "character_count": 1, "mokuro_version": ""},
                {"volume_uuid": "same", "volume_title": "other",
                 "page_count": 1, "character_count": 1, "mokuro_version": ""},
            ],
        }
    )
    by_title = series_json.volume_entry(
        volume_uuid="fresh", volume_title="vol 1", page_count=9,
        character_count=9, mokuro_version="0.2.2",
    )
    by_uuid = series_json.volume_entry(
        volume_uuid="same", volume_title="renamed", page_count=9,
        character_count=9, mokuro_version="0.2.2",
    )
    file_obj = series_json.upsert_entry(existing, series_title="S", entry=by_title)
    assert [v["volume_title"] for v in file_obj["volumes"]] == ["other", "vol 1"]
    assert [v["volume_uuid"] for v in file_obj["volumes"]] == ["same", "fresh"]

    file_obj = series_json.upsert_entry(existing, series_title="S", entry=by_uuid)
    titles = sorted(v["volume_title"] for v in file_obj["volumes"])
    assert titles == ["VOL 1", "renamed"]
    assert file_obj["volumes"][0]["volume_uuid"] == "same"


def test_render_is_a_no_op_when_the_file_is_already_correct():
    entry = series_json.volume_entry(
        volume_uuid="u", volume_title="v", page_count=1, character_count=1,
        mokuro_version="0.2.2",
    )
    first = series_json.render(None, series_title="S", entry=entry)
    assert first is not None
    assert series_json.render(first, series_title="S", entry=entry) is None
    assert series_json.render(
        json.dumps(json.loads(first), indent=2).encode(), series_title="S", entry=entry
    ) == first


@pytest.mark.parametrize(
    "field,value",
    [
        ("spine_width", 0),
        ("archive_size", 0),
        ("archive_size", -5),
        ("archive_size", True),
        ("mokuro_size", 0),
        ("page_count", True),
        ("mokuro_sha256", "nothex"),
        ("offset", 0),
    ],
)
def test_entry_validation_drops_unusable_fields(field, value):
    raw = {
        "volume_uuid": "u",
        "volume_title": "v",
        "page_count": 1,
        "character_count": 1,
        "mokuro_version": "",
        field: value,
    }
    entry = series_json.parse_volume_entry(raw)
    if field == "page_count":
        assert entry is None  # a bool is not a page count
    else:
        assert field not in entry


def test_entry_validation_clamps_and_lowercases():
    entry = series_json.parse_volume_entry(
        {
            "volume_uuid": "u",
            "volume_title": "v",
            "page_count": 1,
            "character_count": 1,
            "mokuro_version": "",
            "offset": -99999,
            "mokuro_sha256": "A" * 64,
        }
    )
    assert entry["offset"] == -series_json.VOLUME_OFFSET_LIMIT
    assert entry["mokuro_sha256"] == "a" * 64


def test_normalize_updated_at_clamps_the_future_and_keeps_iso():
    now = 1_700_000_000_000.0
    assert (
        series_json.normalize_updated_at("2023-11-14T22:13:20.000Z", now_ms=now)
        == "2023-11-14T22:13:20.000Z"
    )
    far_future = "2999-01-01T00:00:00.000Z"
    assert series_json.normalize_updated_at(far_future, now_ms=now) != far_future
    assert series_json.normalize_updated_at(7) is None
    assert series_json.normalize_updated_at("Aug 16 2020", now_ms=now) is None
    assert (
        series_json.parse(
            {"version": 2, "series_title": "S", "updated_at": "Aug 16 2020"}
        )["updated_at"]
        == series_json.FACTLESS_UPDATED_AT
    )


def test_is_series_file_name_is_case_insensitive_and_basename_only():
    assert series_json.is_series_file_name("series.json")
    assert series_json.is_series_file_name("Series.JSON")
    assert series_json.is_series_file_name("mokuro-reader/S/series.json")
    assert not series_json.is_series_file_name("my.series.json")
    assert not series_json.is_series_file_name("series.json.bak")
