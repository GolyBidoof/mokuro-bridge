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
