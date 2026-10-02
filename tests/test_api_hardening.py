from __future__ import annotations

import json
from pathlib import Path

import pytest

from mokuro_bridge import api


def _artifact(path: Path, pages: int, title: str = "Series", volume: str = "Series 1") -> None:
    path.write_text(
        json.dumps(
            {
                "title": title,
                "volume": volume,
                "pages": [{"blocks": []} for _ in range(pages)],
            }
        ),
        encoding="utf-8",
    )


def test_mokuro_artifact_validation_checks_coverage_and_identity(tmp_path):
    path = tmp_path / "book.mokuro"
    _artifact(path, 2)
    api._validate_mokuro_artifact(path, 2, "Series", "Series 1")
    with pytest.raises(RuntimeError, match="coverage"):
        api._validate_mokuro_artifact(path, 3, "Series", "Series 1")
    with pytest.raises(RuntimeError, match="identity"):
        api._validate_mokuro_artifact(path, 2, "Other", "Series 1")


def test_provider_urls_drop_signed_query_and_fragment():
    assert api._safe_provider_url("https://host/path?sig=secret#key") == "https://host/path"
    assert api._safe_provider_url("https://user:secret@host/path?sig=secret") == "https://host/path"
    assert api._safe_provider_url("file:///tmp/secret") is None
    assert api._safe_provider_url("not a url") is None


def test_page_filename_rejects_controls_and_overlong_names():
    with pytest.raises(Exception):
        api._validated_page_name("page\n.jpg", "fallback.jpg")
    with pytest.raises(Exception):
        api._validated_page_name("a" * 256 + ".jpg", "fallback.jpg")


def _session_with(tmp_path, names, received):
    from mokuro_bridge.sessions import Session

    for name in names:
        (tmp_path / name).write_bytes(b"x")
    session = Session(session_id="s1", title="T", safe_title="T", vol_dir=tmp_path)
    session.pages_received.update(received)
    return session


def test_a_page_resent_under_a_new_name_retires_the_old_copy(tmp_path):
    """
    Pages are tracked by name, so a client that changes its naming scheme looks
    like it is sending new pages. Both sets stayed, and a 249-page volume was
    finalized and shipped as 498 pages -- the book once, then a jumbled partial
    copy. The page number is the identity, so the older file has to go.
    """
    session = _session_with(
        tmp_path, ["page_000.webp", "page_0001.webp"],
        {"page_000.webp", "page_0001.webp"},
    )
    session.pages_ocr_done.add("page_000.webp")

    api._supersede_stale_page(session, 0, "page_0001.webp")

    assert not (tmp_path / "page_000.webp").exists()
    assert (tmp_path / "page_0001.webp").exists()
    assert "page_000.webp" not in session.pages_received
    assert "page_000.webp" not in session.pages_ocr_done
    assert session.page_names[0] == "page_0001.webp"


def test_a_legacy_client_resending_its_own_name_loses_nothing(tmp_path):
    session = _session_with(tmp_path, ["page_000.webp"], {"page_000.webp"})

    api._supersede_stale_page(session, 0, "page_000.webp")

    assert (tmp_path / "page_000.webp").exists()
    assert "page_000.webp" in session.pages_received


def test_reconciling_never_deletes_when_no_page_number_was_sent(tmp_path):
    """A client that omits `page_num` must keep working exactly as it did."""
    session = _session_with(
        tmp_path, ["page_000.webp", "page_0001.webp"],
        {"page_000.webp", "page_0001.webp"},
    )

    api._supersede_stale_page(session, None, "page_0001.webp")

    assert (tmp_path / "page_000.webp").exists()
    assert (tmp_path / "page_0001.webp").exists()
    assert session.page_names == {}


def test_a_three_digit_page_is_not_mistaken_for_a_four_digit_one(tmp_path):
    """
    "page_001" is page 1 under the old scheme; "page_0001" is page 0 under the
    new one. Matching on the whole stem keeps the first from being eaten by the
    second.
    """
    session = _session_with(tmp_path, ["page_001.webp"], {"page_001.webp"})

    api._supersede_stale_page(session, 0, "page_0001.webp")

    assert (tmp_path / "page_001.webp").exists()
    assert "page_001.webp" in session.pages_received


def test_a_page_already_being_ingested_is_left_alone(tmp_path):
    """Never pull a file out from under the request that is still using it."""
    session = _session_with(
        tmp_path, ["page_000.webp", "page_0001.webp"],
        {"page_000.webp", "page_0001.webp"},
    )
    session.page_reservations.add("page_000.webp")

    api._supersede_stale_page(session, 0, "page_0001.webp")

    assert (tmp_path / "page_000.webp").exists()
    assert "page_000.webp" in session.pages_received
