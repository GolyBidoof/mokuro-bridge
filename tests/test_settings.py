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


def test_the_page_offers_the_settings_worth_changing():
    assert [spec["env"] for spec in settings.SPECS] == [
        "MOKURO_BRIDGE_FETCH_PORTS",
        "MOKURO_BRIDGE_FETCH_CONCURRENCY",
        "MOKURO_BRIDGE_UPDATE_CHECK",
        "OCR_BATCH_SIZE",
        "MOKURO_BRIDGE_OUTPUT_DIR",
        "MOKURO_BRIDGE_INGEST_ROOTS",
    ]
    assert all(spec["label"] and spec["help"] and spec.get("group") for spec in settings.SPECS)


def test_the_page_shows_short_named_groups_in_reading_order():
    groups = []
    for spec in settings.SPECS:
        if spec["group"] not in groups:
            groups.append(spec["group"])
    assert groups == ["Downloading", "Updates", "Reading pages", "Advanced"]
    assert [row["group"] for row in settings.snapshot()] == [
        "Downloading",
        "Downloading",
        "Updates",
        "Reading pages",
        "Advanced",
        "Advanced",
    ]


def test_a_switch_is_stored_as_true_or_false():
    saved, problems = settings.update({"MOKURO_BRIDGE_UPDATE_CHECK": "off"})
    assert problems == []
    assert saved == {"MOKURO_BRIDGE_UPDATE_CHECK": "false"}
    assert settings.stored("MOKURO_BRIDGE_UPDATE_CHECK") == "false"
    row = next(r for r in settings.snapshot() if r["env"] == "MOKURO_BRIDGE_UPDATE_CHECK")
    assert row["display"] == "Off"
    assert row["restart"] is False, "turning update checks off must not need a restart"


@pytest.mark.parametrize("value, expected", [("on", "true"), (True, "true"), (1, "true"), ("0", "false"), (False, "false")])
def test_switch_values_are_normalised(value, expected):
    saved, problems = settings.update({"MOKURO_BRIDGE_UPDATE_CHECK": value})
    assert problems == []
    assert saved["MOKURO_BRIDGE_UPDATE_CHECK"] == expected


def test_a_switch_that_is_not_on_or_off_is_rejected():
    saved, problems = settings.update({"MOKURO_BRIDGE_UPDATE_CHECK": "maybe"})
    assert saved == {} and problems
    assert "on or off" in problems[0]
    assert not settings.SETTINGS_FILE.exists()


def test_update_checks_follow_the_page(monkeypatch):
    from mokuro_bridge import update

    settings.write({"MOKURO_BRIDGE_UPDATE_CHECK": "false"})
    assert update.checks_enabled() is False
    monkeypatch.setenv("MOKURO_BRIDGE_UPDATE_CHECK", "1")
    assert update.checks_enabled() is True, "the environment still wins"


def test_the_download_and_ocr_rows_are_live_once_the_file_is_written():
    settings.write({"MOKURO_BRIDGE_FETCH_CONCURRENCY": 8, "OCR_BATCH_SIZE": 16})
    assert config._knob("MOKURO_BRIDGE_FETCH_CONCURRENCY", 0) == "8"
    assert config._knob("OCR_BATCH_SIZE", 32) == "16"


def test_env_only_settings_still_apply_but_are_not_on_the_page(monkeypatch):
    monkeypatch.setenv("OCR_CHUNK_SIZE", "32")
    assert config._knob("OCR_CHUNK_SIZE", 8) == "32"
    assert "OCR_CHUNK_SIZE" in settings._ENV_ONLY
    assert all(spec["env"] != "OCR_CHUNK_SIZE" for spec in settings.SPECS)
    # Fetch concurrency moved onto the page, so it must not still be env-only.
    assert "MOKURO_BRIDGE_FETCH_CONCURRENCY" not in settings._ENV_ONLY


def test_defaults_come_from_the_specs():
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "48"
    assert settings.source("MOKURO_BRIDGE_FETCH_PORTS") == "default"
    row = next(r for r in settings.snapshot() if r["env"] == "MOKURO_BRIDGE_FETCH_PORTS")
    assert row["display"] == "48" and row["restart"] is True
    assert row["default"] == 48, "the page states the built-in default beside each value"
    assert row["env_set"] is False and row["pinned"] is False
    assert row["file_value"] is None


def test_the_environment_wins_over_the_file(monkeypatch):
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 5})
    monkeypatch.setenv("MOKURO_BRIDGE_FETCH_PORTS", "7")
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "7"
    assert settings.source("MOKURO_BRIDGE_FETCH_PORTS") == "environment"


def test_an_environment_row_is_reported_as_pinned(monkeypatch):
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 12})
    monkeypatch.setenv("MOKURO_BRIDGE_FETCH_PORTS", "0")
    row = next(r for r in settings.snapshot() if r["env"] == "MOKURO_BRIDGE_FETCH_PORTS")
    assert row["value"] == "0", "the environment value is the one in force"
    assert row["pinned"] is True and row["env_set"] is True
    assert row["file_value"] == "12", "the file's own value is still reported"
    assert row["default"] == 48


def test_the_file_wins_over_the_default():
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 32})
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "32"
    assert settings.source("MOKURO_BRIDGE_FETCH_PORTS") == "settings file"


def test_update_coerces_types_and_persists():
    saved, problems = settings.update(
        {
            "MOKURO_BRIDGE_FETCH_PORTS": "12",
            "MOKURO_BRIDGE_INGEST_ROOTS": " /a/one , , /b/two ",
            "MOKURO_BRIDGE_OUTPUT_DIR": "~/library",
        }
    )
    assert problems == []
    assert saved["MOKURO_BRIDGE_FETCH_PORTS"] == 12
    assert saved["MOKURO_BRIDGE_INGEST_ROOTS"] == "/a/one,/b/two"
    assert saved["MOKURO_BRIDGE_OUTPUT_DIR"] == str(Path("~/library").expanduser())
    stored = json.loads(settings.SETTINGS_FILE.read_text())
    assert stored["MOKURO_BRIDGE_FETCH_PORTS"] == 12


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({"MOKURO_BRIDGE_FETCH_PORTS": "9999"}, "between 0 and 256"),
        ({"MOKURO_BRIDGE_FETCH_PORTS": "many"}, "could not convert"),
        ({"MOKURO_BRIDGE_FETCH_PORTS": "2.5"}, "whole number"),
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
    settings.write({"MOKURO_BRIDGE_OUTPUT_DIR": "/tmp/library"})
    assert json.loads(settings.SETTINGS_FILE.read_text()) == {
        "MOKURO_BRIDGE_OUTPUT_DIR": "/tmp/library"
    }
    if os.name == "posix":
        assert stat.S_IMODE(settings.SETTINGS_FILE.stat().st_mode) == 0o600


def test_a_corrupt_file_is_ignored_rather_than_fatal():
    settings.SETTINGS_FILE.write_text("{not json")
    settings._cache = None
    assert settings.load() == {}
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "48"


def test_the_knob_helper_prefers_environment_then_file(monkeypatch):
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 12})
    assert config._knob("MOKURO_BRIDGE_FETCH_PORTS", 48) == "12"
    monkeypatch.setenv("MOKURO_BRIDGE_FETCH_PORTS", "20")
    assert config._knob("MOKURO_BRIDGE_FETCH_PORTS", 48) == "20"
    monkeypatch.delenv("MOKURO_BRIDGE_FETCH_PORTS")
    assert config._knob("OCR_UNSET_SETTING", 8) == "8"


def test_display_for_an_empty_csv_is_explicit():
    row = next(r for r in settings.snapshot() if r["env"] == "MOKURO_BRIDGE_INGEST_ROOTS")
    assert row["display"] == "(none)"


def test_a_pinned_key_is_skipped_and_its_stored_value_survives(monkeypatch):
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 12})
    monkeypatch.setenv("MOKURO_BRIDGE_FETCH_PORTS", "0")

    saved, problems = settings.update({"MOKURO_BRIDGE_FETCH_PORTS": "99"})

    assert (saved, problems) == ({}, []), "a pinned key is skipped, not an error"
    assert settings.SETTINGS_FILE.exists()
    assert json.loads(settings.SETTINGS_FILE.read_text())["MOKURO_BRIDGE_FETCH_PORTS"] == 12
    assert settings.value("MOKURO_BRIDGE_FETCH_PORTS") == "0", "the environment still wins"
    assert settings.pinned_keys({"MOKURO_BRIDGE_FETCH_PORTS": "99"}) == [
        "MOKURO_BRIDGE_FETCH_PORTS"
    ]


def test_clearing_a_pinned_key_cannot_delete_the_stored_value(monkeypatch):
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 12})
    monkeypatch.setenv("MOKURO_BRIDGE_FETCH_PORTS", "0")

    settings.update({"MOKURO_BRIDGE_FETCH_PORTS": ""})

    assert settings.stored("MOKURO_BRIDGE_FETCH_PORTS") == "12"


def test_a_mixed_payload_saves_the_keys_the_environment_does_not_own(monkeypatch, tmp_path):
    settings.write({"MOKURO_BRIDGE_FETCH_PORTS": 12})
    monkeypatch.setenv("MOKURO_BRIDGE_FETCH_PORTS", "0")
    target = tmp_path / "library"
    target.mkdir()

    saved, problems = settings.update(
        {
            "MOKURO_BRIDGE_FETCH_PORTS": 0,
            "MOKURO_BRIDGE_OUTPUT_DIR": str(target),
            "MOKURO_BRIDGE_INGEST_ROOTS": "/media/one,/media/two",
        }
    )

    assert problems == []
    assert saved == {
        "MOKURO_BRIDGE_OUTPUT_DIR": str(target),
        "MOKURO_BRIDGE_INGEST_ROOTS": "/media/one,/media/two",
    }
    stored = json.loads(settings.SETTINGS_FILE.read_text())
    assert stored["MOKURO_BRIDGE_FETCH_PORTS"] == 12, "the pinned stored value was not clobbered"
    assert stored["MOKURO_BRIDGE_INGEST_ROOTS"] == "/media/one,/media/two"
    row = next(r for r in settings.snapshot() if r["env"] == "MOKURO_BRIDGE_FETCH_PORTS")
    assert row["file_value"] == "12" and row["pinned"] is True


def test_an_output_dir_that_is_a_file_is_rejected(tmp_path):
    not_a_folder = tmp_path / "not-a-folder"
    not_a_folder.write_text("x")

    saved, problems = settings.update({"MOKURO_BRIDGE_OUTPUT_DIR": str(not_a_folder)})

    assert saved == {}
    assert problems and "file, not a folder" in problems[0].lower()
    assert not settings.SETTINGS_FILE.exists(), "a rejected value writes nothing"


def test_an_existing_output_directory_is_accepted(tmp_path):
    target = tmp_path / "library"
    target.mkdir()

    saved, problems = settings.update({"MOKURO_BRIDGE_OUTPUT_DIR": str(target)})

    assert problems == []
    assert saved == {"MOKURO_BRIDGE_OUTPUT_DIR": str(target)}


def test_an_output_dir_that_does_not_exist_needs_a_creatable_parent(tmp_path):
    missing_parent = tmp_path / "missing" / "deeper"
    saved, problems = settings.update({"MOKURO_BRIDGE_OUTPUT_DIR": str(missing_parent)})
    assert saved == {}
    assert problems and "parent folder does not exist" in problems[0].lower()

    new_folder = tmp_path / "new-library"
    saved, problems = settings.update({"MOKURO_BRIDGE_OUTPUT_DIR": str(new_folder)})
    assert problems == []
    assert saved == {"MOKURO_BRIDGE_OUTPUT_DIR": str(new_folder)}


def test_a_relative_output_dir_is_rejected():
    saved, problems = settings.update({"MOKURO_BRIDGE_OUTPUT_DIR": "relative/library"})
    assert saved == {}
    assert problems and "absolute" in problems[0].lower()


def test_an_empty_output_dir_is_accepted_and_clears():
    settings.write({"MOKURO_BRIDGE_OUTPUT_DIR": "/tmp/library"})

    saved, problems = settings.update({"MOKURO_BRIDGE_OUTPUT_DIR": ""})

    assert problems == []
    assert saved == {"MOKURO_BRIDGE_OUTPUT_DIR": ""}
    assert settings.stored("MOKURO_BRIDGE_OUTPUT_DIR") is None


def test_a_relative_ingest_root_is_rejected():
    saved, problems = settings.update(
        {"MOKURO_BRIDGE_INGEST_ROOTS": "/media/one, relative/books"}
    )

    assert saved == {}
    assert problems and "absolute" in problems[0].lower()
    assert not settings.SETTINGS_FILE.exists()


def test_absolute_ingest_roots_with_spaces_are_still_accepted():
    saved, problems = settings.update(
        {"MOKURO_BRIDGE_INGEST_ROOTS": " /media/one , , ~/library/two "}
    )

    assert problems == []
    assert saved == {"MOKURO_BRIDGE_INGEST_ROOTS": "/media/one,~/library/two"}

