"""Directory listings: the megatools parser and the shared entry shape."""

from __future__ import annotations

from mokuro_bridge.providers.base import LIST_LIMIT_ENTRIES, listing_entry, parent_path
from mokuro_bridge.providers.mega import _parse_mega_listing

HEADER = (
    "=" * 83 + "\n"
    "Handle      Owner       T          Size   Mod. Date           Path\n"
    + "=" * 83 + "\n"
)


def _line(handle, owner, node_type, size, date, path):
    return f"{handle:<11} {owner:<11} {node_type} {size:>13} {date:>19} {path}\n"


def test_immediate_children_are_listed_with_their_type_and_size():
    listing = HEADER + "".join(
        [
            _line("7Fdi3ZjC", "a@b.com", 1, "-", "2026-01-01 12:00:00", "/Root/lib/Series A"),
            _line("mEU23aSD", "a@b.com", 0, "1234", "2026-01-02 09:30:00", "/Root/lib/vol 1.cbz"),
            _line("xYz12345", "a@b.com", 0, "500", "", "/Root/lib/vol 2.cbz"),
        ]
    )
    entries = _parse_mega_listing(listing, "/Root/lib")
    assert entries == [
        {"name": "Series A", "path": "/Root/lib/Series A", "type": "folder"},
        {"name": "vol 1.cbz", "path": "/Root/lib/vol 1.cbz", "type": "file", "size": 1234},
        {"name": "vol 2.cbz", "path": "/Root/lib/vol 2.cbz", "type": "file", "size": 500},
    ]


def test_grandchildren_and_the_parent_itself_are_skipped():
    listing = HEADER + "".join(
        [
            _line("aaa", "a@b.com", 2, "-", "", "/Root"),
            _line("ddd", "a@b.com", 1, "-", "", "/Root/lib"),
            _line("bbb", "a@b.com", 1, "-", "", "/Root/lib/Series A"),
            _line("ccc", "a@b.com", 0, "10", "", "/Root/lib/Series A/vol.cbz"),
        ]
    )
    assert [entry["name"] for entry in _parse_mega_listing(listing, "/")] == ["Root"]
    assert [entry["name"] for entry in _parse_mega_listing(listing, "/Root")] == ["lib"]
    assert [entry["name"] for entry in _parse_mega_listing(listing, "/Root/lib")] == ["Series A"]


def test_an_empty_listing_and_a_file_as_parent_are_both_empty():
    assert _parse_mega_listing("", "/Root/lib") == []
    assert _parse_mega_listing(HEADER, "/Root/lib") == []
    file_line = _line("mEU23aSD", "a@b.com", 0, "1234", "", "/Root/lib/vol 1.cbz")
    assert _parse_mega_listing(file_line, "/Root/lib/vol 1.cbz") == []


def test_paths_with_spaces_survive_the_column_parsing():
    listing = _line("aaa", "a@b.com", 1, "-", "2026-01-01 00:00:00", "/Root/lib/my series") + _line(
        "bbb", "a@b.com", 0, "5", "2026-01-01 00:00:00", "/Root/lib/my series/vol 1.cbz"
    )
    assert _parse_mega_listing(listing, "/Root/lib") == [
        {"name": "my series", "path": "/Root/lib/my series", "type": "folder"}
    ]
    assert _parse_mega_listing(listing, "/Root/lib/my series") == [
        {
            "name": "vol 1.cbz",
            "path": "/Root/lib/my series/vol 1.cbz",
            "type": "file",
            "size": 5,
        }
    ]


def test_garbage_lines_are_ignored():
    listing = "not a listing at all\n" + _line("aaa", "a@b.com", 1, "-", "", "/Root/lib/Series")
    assert [entry["name"] for entry in _parse_mega_listing(listing, "/Root/lib")] == ["Series"]


def test_the_listing_is_capped():
    rows = "".join(
        _line(f"h{i:08d}"[:8], "a@b.com", 1, "-", "", f"/Root/lib/Series {i}")
        for i in range(LIST_LIMIT_ENTRIES + 25)
    )
    assert len(_parse_mega_listing(rows, "/Root/lib")) == LIST_LIMIT_ENTRIES


def test_entry_and_parent_helpers():
    assert listing_entry("a", "/x/a", is_folder=True) == {"name": "a", "path": "/x/a", "type": "folder"}
    assert listing_entry("b", "/x/b", is_folder=False, size=3)["size"] == 3
    assert parent_path("/Root/lib/Series") == "/Root/lib"
    assert parent_path("/Root") == ""
    assert parent_path("mokuro-reader/series") == "mokuro-reader"
