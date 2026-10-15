"""Persisted settings: precedence, validation and the snapshot rows."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from mokuro_bridge import config, settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_cache", None)
    for spec in settings.SPECS:
        monkeypatch.delenv(spec["env"], raising=False)
    yield


def test_defaults_come_from_the_specs():
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "48"
    assert settings.source("MOKURO_BRIDGE_FETCH_PORTS") == "default"
    row = next(r for r in settings.snapshot() if r["env"] == "MOKURO_BRIDGE_FETCH_PORTS")
    assert row["display"] == "48" and row["restart"] is True
    assert row["default"] == 48, "the page states the built-in default beside each value"


def test_the_environment_wins_over_the_file(monkeypatch):
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 5})
    monkeypatch.setenv("MOKURO_BRIDGE_FETCH_PORTS", "7")
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "7"
    assert settings.source("MOKURO_BRIDGE_FETCH_PORTS") == "environment"


def test_the_file_wins_over_the_default():
    settings.write({"OCR_CHUNK_SIZE": 32})
    assert settings.value("OCR_CHUNK_SIZE") == "32"
    assert settings.source("OCR_CHUNK_SIZE") == "settings file"


def test_update_coerces_types_and_persists():
    saved, problems = settings.update(
        {
            "MOKURO_BRIDGE_FETCH_PORTS": "12",
            "OCR_IDLE_FLUSH_S": "2.5",
            "MOKURO_BRIDGE_INGEST_ROOTS": " /a/one , , /b/two ",
            "MOKURO_BRIDGE_OUTPUT_DIR": "~/library",
        }
    )
    assert problems == []
    assert saved["MOKURO_BRIDGE_FETCH_PORTS"] == 12
    assert saved["OCR_IDLE_FLUSH_S"] == 2.5
    assert saved["MOKURO_BRIDGE_INGEST_ROOTS"] == "/a/one,/b/two"
    assert saved["MOKURO_BRIDGE_OUTPUT_DIR"] == str(Path("~/library").expanduser())
    stored = json.loads(settings.SETTINGS_FILE.read_text())
    assert stored["MOKURO_BRIDGE_FETCH_PORTS"] == 12


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({"MOKURO_BRIDGE_FETCH_PORTS": "9999"}, "between 0 and 256"),
        ({"MOKURO_BRIDGE_FETCH_PORTS": "many"}, "could not convert"),
        ({"OCR_CHUNK_SIZE": "2.5"}, "whole number"),
        ({"NOT_A_SETTING": "1"}, "unknown setting"),
    ],
)
def test_update_rejects_bad_values_without_writing(payload, fragment):
    saved, problems = settings.update(payload)
    assert saved == {}
    assert problems and fragment in problems[0].lower()
    assert not settings.SETTINGS_FILE.exists()


def test_an_empty_value_clears_the_stored_key():
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 12})
    settings.update({"MOKURO_BRIDGE_FETCH_PORTS": ""})
    assert settings.stored("MOKURO_BRIDGE_FETCH_PORTS") is None
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "48"


def test_the_file_is_written_privately_and_parses_as_json():
    settings.write({"OCR_CHUNK_SIZE": 4})
    assert json.loads(settings.SETTINGS_FILE.read_text()) == {"OCR_CHUNK_SIZE": 4}
    if os.name == "posix":
        assert stat.S_IMODE(settings.SETTINGS_FILE.stat().st_mode) == 0o600


def test_a_corrupt_file_is_ignored_rather_than_fatal():
    settings.SETTINGS_FILE.write_text("{not json")
    settings._cache = None
    assert settings.load() == {}
    assert settings.value("OCR_CHUNK_SIZE") == "8"


def test_the_knob_helper_prefers_environment_then_file(monkeypatch):
    settings.write({"OCR_CHUNK_SIZE": 12})
    assert config._knob("OCR_CHUNK_SIZE", 8) == "12"
    monkeypatch.setenv("OCR_CHUNK_SIZE", "20")
    assert config._knob("OCR_CHUNK_SIZE", 8) == "20"
    monkeypatch.delenv("OCR_CHUNK_SIZE")
    assert config._knob("OCR_UNSET_SETTING", 8) == "8"


def test_display_for_an_empty_csv_is_explicit():
    row = next(r for r in settings.snapshot() if r["env"] == "MOKURO_BRIDGE_INGEST_ROOTS")
    assert row["display"] == "(none)"
