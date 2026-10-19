"""Headless tests for the status display: no socket, no TTY, no terminal state.

``run_status`` gets an injected fetch and key source, plus a fake TTY stream.
"""

from __future__ import annotations

import io
import os
import re

import pytest

from mokuro_bridge import status_tui

SGR = re.compile(r"\x1b\[[0-9;]*m")
ESC = "\x1b"


def visible(line):
    return SGR.sub("", line)


def health(**overrides):
    payload = {
        "app": "mokuro-bridge",
        "version": "0.9.0",
        "status": "ok",
        "busy": False,
        "update_check": "ok",
        "latest_version": "0.9.0",
        "update_available": False,
        "mokuro_flavor": "fork",
        "mokuro_installed": True,
        "mokuro_version": "0.2.2",
        "megatools_installed": True,
        "upload_method_default": "mega:work",
        "upload_methods": [
            {
                "id": "mega:work",
                "name": "MEGA (megatools) (work)",
                "configured": True,
                "default": True,
                "library_root": "/Root/mokuro-reader",
                "email": "a@b.c",
                "creds_source": "keychain",
            },
            {"id": "local", "name": "Local output directory", "configured": False, "default": False,
             "root": "/Users/x/output"},
        ],
        "fetchProxyPorts": [8123, 8124],
        "fetchUpstreams": ["*.bookwalker.jp"],
        "active_sessions": 0,
        "ocr_chunk_size": 8,
        "ocr_idle_flush_s": 30,
        "ocr_queue_depth": 0,
        "ocr_processing_depth": 0,
        "ocr_worker": "idle",
    }
    payload.update(overrides)
    return payload


def upload(**overrides):
    data = {
        "file": "vol01.cbz",
        "percent": 42.0,
        "current_bytes": 21_000_000,
        "total_bytes": 50_000_000,
        "speed_human": "3.4 MB/s",
    }
    data.update(overrides)
    return data


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def _fixed_terminal(monkeypatch):
    monkeypatch.setattr(status_tui.shutil, "get_terminal_size",
                        lambda *a, **k: os.terminal_size((100, 30)))


# --- render_status ---------------------------------------------------------


@pytest.mark.parametrize("width", [80, 100, 140])
@pytest.mark.parametrize("height", [12, 30])
def test_frame_is_exactly_width_by_height(width, height):
    frame = status_tui.render_status(health(), width=width, height=height)
    lines = frame.split("\n")
    assert len(lines) == height
    assert all(len(line) == width for line in lines)


@pytest.mark.parametrize("width", [80, 100, 140])
def test_frame_with_upload_and_busy_is_exact(width):
    frame = status_tui.render_status(health(busy=True, busy_stage="ocr", busy_detail="3 pages"),
                                     width=width, height=24, upload=upload())
    assert all(len(line) == width for line in frame.split("\n"))


def test_plain_frame_has_no_ansi_and_colour_keeps_width():
    plain = status_tui.render_status(health(), width=100, height=30)
    assert ESC not in plain
    coloured = status_tui.render_status(health(), width=100, height=30, color=True)
    assert ESC + "[" in coloured
    lines = coloured.split("\n")
    assert len(lines) == 30
    assert all(len(visible(line)) == 100 for line in lines)


def test_star_follows_upload_method_default():
    first = status_tui.render_status(health(), width=100, height=30)
    mega = next(line for line in first.split("\n") if "MEGA (megatools) (work)" in line)
    local = next(line for line in first.split("\n") if "Local output directory" in line)
    assert "★" in mega
    assert "★" not in local

    second = status_tui.render_status(health(upload_method_default="local"), width=100, height=30)
    moved = next(line for line in second.split("\n") if "Local output directory" in line)
    assert "★" in moved


def test_configured_and_unconfigured_destinations_differ():
    frame = status_tui.render_status(health(), width=100, height=30)
    mega = next(line for line in frame.split("\n") if "MEGA (megatools) (work)" in line)
    local = next(line for line in frame.split("\n") if "Local output directory" in line)
    assert "●" in mega
    assert "○" in local
    assert "/Root/mokuro-reader" in mega
    assert "/Users/x/output" in local


def test_busy_payload_shows_stage_and_detail():
    busy = status_tui.render_status(
        health(busy=True, busy_stage="uploading", busy_detail="vol01.cbz"),
        width=100, height=30,
    )
    assert "uploading" in busy
    assert "vol01.cbz" in busy

    idle = status_tui.render_status(health(), width=100, height=30)
    now = next(line for line in idle.split("\n") if "NOW" in line)
    assert "idle" in now


def test_upload_progress_renders_bar_and_percent():
    frame = status_tui.render_status(health(), width=100, height=30, upload=upload())
    line = next(line for line in frame.split("\n") if "█" in line)
    assert "░" in line
    assert "42%" in line
    assert "vol01.cbz" in line
    assert "3.4 MB/s" in line
    assert "20.0 MB" in line


def test_upload_percent_is_derived_from_bytes_when_absent():
    frame = status_tui.render_status(health(), width=100, height=30,
                                     upload=upload(percent=None, current_bytes=25_000_000))
    line = next(line for line in frame.split("\n") if "█" in line)
    assert "50%" in line


def test_missing_values_render_placeholders():
    frame = status_tui.render_status({}, width=80, height=24)
    lines = frame.split("\n")
    assert len(lines) == 24
    assert all(len(line) == 80 for line in lines)
    assert "-" in frame

    sparse = status_tui.render_status(
        {"mokuro_flavor": None, "ocr_queue_depth": None, "upload_methods": None, "fetchProxyPorts": None},
        width=80, height=24,
    )
    assert ESC not in sparse
    assert all(len(line) == 80 for line in sparse.split("\n"))


def test_non_dict_payload_does_not_crash():
    frame = status_tui.render_status(None, width=80, height=10)
    assert len(frame.split("\n")) == 10


def test_unknown_keys_are_ignored():
    frame = status_tui.render_status(health(zzz="???", nested={"x": 1}), width=100, height=20)
    assert "???" not in frame


def test_height_below_content_is_still_exact():
    frame = status_tui.render_status(health(), width=100, height=8)
    lines = frame.split("\n")
    assert len(lines) == 8
    assert all(len(line) == 100 for line in lines)


@pytest.mark.parametrize("width", [10, 30, 50, 59])
def test_narrow_terminal_never_exceeds_width(width):
    frame = status_tui.render_status(health(), width=width, height=20, upload=upload())
    lines = frame.split("\n")
    assert len(lines) == 20
    assert all(len(line) == width for line in lines)


def test_host_header_is_shown_when_given():
    frame = status_tui.render_status(health(), width=100, height=20, host_header="127.0.0.1:62642")
    assert "http://127.0.0.1:62642" in frame
    assert "http://" not in status_tui.render_status(health(), width=100, height=20)


# --- run_status ------------------------------------------------------------


def test_run_status_piped_prints_one_frame_and_returns_zero():
    calls = []

    def fetch(url):
        calls.append(url)
        return health()

    stream = io.StringIO()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=stream, fetch=fetch, keys=iter(["q"]))
    assert code == 0
    out = stream.getvalue()
    assert ESC not in out
    # A piped snapshot uses the full terminal height (the alt screen reserves a
    # row, a snapshot does not), so `head -30` with LINES=30 shows all 30 lines.
    expected = status_tui.render_status(health(), width=100, height=30, host_header="http://127.0.0.1:62642")
    assert out == expected + "\n"
    assert calls == ["http://127.0.0.1:62642/health"]


def test_run_status_reports_fetch_failure_in_the_frame():
    def fetch(url):
        raise OSError("connection refused")

    stream = io.StringIO()
    code = status_tui.run_status("http://127.0.0.1:1", stream=stream, fetch=fetch, keys=iter(["q"]))
    assert code == 0
    out = stream.getvalue()
    assert ESC not in out
    assert "unreachable" in out
    assert "connection refused" in out


def test_run_status_restores_terminal_on_quit():
    stream = FakeTTY()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=stream, fetch=lambda url: health(),
                                 keys=iter(["q"]))
    assert code == 0
    out = stream.getvalue()
    assert status_tui.ALT_ON in out
    assert status_tui.CURSOR_HIDE in out
    assert out.endswith(status_tui.CURSOR_SHOW + status_tui.ALT_OFF)


def test_run_status_restores_terminal_on_keyboard_interrupt():
    def fetch(url):
        raise KeyboardInterrupt

    stream = FakeTTY()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=stream, fetch=fetch, keys=iter([]))
    assert code == 130
    assert stream.getvalue().endswith(status_tui.CURSOR_SHOW + status_tui.ALT_OFF)


def test_run_status_c_key_prints_dashboard_url_and_keeps_running():
    stream = FakeTTY()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=stream, fetch=lambda url: health(),
                                 keys=iter(["c", "x", "q"]))
    assert code == 0
    out = stream.getvalue()
    assert "dashboard: http://127.0.0.1:62642" in out
    assert out.endswith(status_tui.CURSOR_SHOW + status_tui.ALT_OFF)


# --- main ------------------------------------------------------------------


def test_main_prefers_status_url_then_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(status_tui, "run_status", lambda url, **kw: seen.append((url, kw)) or 0)

    monkeypatch.delenv("MOKURO_BRIDGE_HOST", raising=False)
    monkeypatch.delenv("MOKURO_BRIDGE_PORT", raising=False)
    assert status_tui.main(["--status"]) == 0
    assert seen[-1][0] == status_tui.DEFAULT_URL

    monkeypatch.setenv("MOKURO_BRIDGE_HOST", "10.0.0.5")
    monkeypatch.setenv("MOKURO_BRIDGE_PORT", "9000")
    assert status_tui.main(["--status"]) == 0
    assert seen[-1][0] == "http://10.0.0.5:9000"

    assert status_tui.main(["--status", "--status-url", "http://elsewhere:1", "--status-interval", "0.5"]) == 0
    assert seen[-1] == ("http://elsewhere:1", {"interval_s": 0.5})


def test_worker_metrics_render_as_a_state_word():
    frame = status_tui.render_status(
        health(ocr_worker={"started": True, "alive": True, "state": "processing"}),
        width=110,
        height=30,
    )
    assert "processing" in frame
    assert "{" not in frame and "}" not in frame


def test_worker_dict_without_a_state_falls_back_to_started_or_idle():
    started = status_tui.render_status(health(ocr_worker={"started": True}), width=110, height=30)
    idle = status_tui.render_status(health(ocr_worker={"started": False}), width=110, height=30)
    assert "started" in started
    assert "idle" in idle


def test_idle_flush_keeps_its_fraction():
    frame = status_tui.render_status(health(ocr_idle_flush_s=1.5), width=110, height=30)
    assert "idle-flush 1.5s" in frame


@pytest.mark.parametrize("count,expected", [(1, "1 upstream "), (2, "2 upstreams")])
def test_upstream_count_is_pluralised(count, expected):
    frame = status_tui.render_status(
        health(fetchUpstreams=["https://a.example"] * count), width=110, height=30
    )
    assert expected in frame


# --- display width: wide and combining characters ---------------------------


def assert_frame_geometry(frame, width, height):
    lines = frame.split("\n")
    assert len(lines) == height
    assert all(status_tui.display_width(line) == width for line in lines), [
        status_tui.display_width(line) for line in lines
    ]


def test_display_width_counts_wide_and_combining_characters():
    assert status_tui.display_width("abc") == 3
    assert status_tui.display_width("日本語") == 6
    assert status_tui.display_width("e\u0301") == 1
    assert status_tui.display_width("★") == 1


def test_clip_never_splits_wide_or_combining_characters():
    clipped = status_tui._clip("日本語", 4)
    assert status_tui.display_width(clipped) <= 4
    assert clipped.endswith("…")
    assert status_tui._clip("日本語", 1) == "…"
    combining = status_tui._clip("e\u0301e\u0301e\u0301", 2)
    assert status_tui.display_width(combining) <= 2
    assert combining == "e\u0301…"  # the combining mark stays with its base character


def test_cjk_names_keep_the_frame_geometry():
    frame = status_tui.render_status(
        health(
            upload_methods=[{
                "id": "local",
                "name": "ローカル出力",
                "configured": True,
                "root": "/Users/x/漫画/日本語の本",
            }],
            upload_method_default="local",
        ),
        width=80,
        height=24,
        upload=upload(file="日本語の漫画_第01巻.cbz"),
    )
    assert_frame_geometry(frame, 80, 24)
    assert "日本語" in frame


# --- borders and footer ------------------------------------------------------


@pytest.mark.parametrize("width", [20, 30, 35, 40, 50, 60, 100])
@pytest.mark.parametrize("overrides", [
    {},
    {"status": "unreachable",
     "error": "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"},
    {"busy": True, "busy_stage": "uploading", "busy_detail": "vol01.cbz"},
])
def test_both_corners_survive_every_width(width, overrides):
    frame = status_tui.render_status(
        health(**overrides), width=width, height=15, host_header="http://127.0.0.1:62642"
    )
    lines = frame.split("\n")
    assert lines[0].endswith("┐"), lines[0]
    assert lines[-1].endswith("┘"), lines[-1]
    assert_frame_geometry(frame, width, 15)


@pytest.mark.parametrize("width", [30, 40, 50])
def test_footer_keeps_the_quit_hint(width):
    frame = status_tui.render_status(health(), width=width, height=12)
    assert "q quit" in frame
    assert all(status_tui.display_width(line) == width for line in frame.split("\n"))


# --- narrow and mid-width layouts -------------------------------------------


@pytest.mark.parametrize("width", [50, 59, 60, 61, 70])
def test_mid_widths_keep_destinations_and_worker_state(width):
    frame = status_tui.render_status(
        health(busy=True, busy_stage="ocr", busy_detail="3 pages"), width=width, height=24
    )
    assert "DESTINATIONS" in frame
    assert "MEGA" in frame
    assert "worker" in frame
    assert_frame_geometry(frame, width, 24)


def test_narrow_body_keeps_status_destination_worker_and_activity():
    frame = status_tui.render_status(health(), width=40, height=15)
    for needle in ("status", "queue", "worker", "output", "MEGA", "now"):
        assert needle in frame, needle
    assert_frame_geometry(frame, 40, 15)


# --- upload plumbing, help, colour, flags -----------------------------------


def test_run_status_passes_the_health_upload_object_to_both_renderers():
    payload = health(upload=upload())
    tty = FakeTTY()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=tty,
                                 fetch=lambda url: payload, keys=iter(["q"]))
    assert code == 0
    assert "42%" in tty.getvalue() and "█" in tty.getvalue()

    plain = io.StringIO()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=plain,
                                 fetch=lambda url: payload, keys=iter(["q"]))
    assert code == 0
    assert "42%" in plain.getvalue() and "█" in plain.getvalue()
    assert ESC not in plain.getvalue()


def test_question_mark_opens_help_and_any_key_returns():
    stream = FakeTTY()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=stream,
                                 fetch=lambda url: health(), keys=iter(["?", "x", "q"]))
    assert code == 0
    out = stream.getvalue()
    assert "default destination" in out
    assert "destination configured" in out
    assert "q quit" in out
    assert out.endswith(status_tui.CURSOR_SHOW + status_tui.ALT_OFF)


def test_no_color_keeps_the_display_but_drops_colour(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = FakeTTY()
    code = status_tui.run_status("http://127.0.0.1:62642", stream=stream,
                                 fetch=lambda url: health(), keys=iter(["q"]))
    assert code == 0
    out = stream.getvalue()
    assert status_tui.ALT_ON in out
    assert "\x1b[32m" not in out and "\x1b[36m" not in out and "\x1b[1m" not in out
    assert out.endswith(status_tui.CURSOR_SHOW + status_tui.ALT_OFF)


def test_main_honours_host_and_port_from_the_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(status_tui, "run_status", lambda url, **kw: seen.append(url) or 0)
    assert status_tui.main(["--status"], host="10.0.0.5", port=9000) == 0
    assert seen[-1] == "http://10.0.0.5:9000"
    assert status_tui.main(["--status"], host="10.0.0.5", port=9000,
                           status_url="http://elsewhere:1") == 0
    assert seen[-1] == "http://elsewhere:1"


def test_url_normalisation_from_flags_and_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(status_tui, "run_status", lambda url, **kw: seen.append(url) or 0)
    for name in ("MOKURO_BRIDGE_HOST", "MOKURO_BRIDGE_PORT"):
        monkeypatch.delenv(name, raising=False)

    assert status_tui.main(["--status", "--status-url", "127.0.0.1:62642"]) == 0
    assert seen[-1] == "http://127.0.0.1:62642"

    monkeypatch.setenv("MOKURO_BRIDGE_HOST", "http://10.0.0.5")
    monkeypatch.setenv("MOKURO_BRIDGE_PORT", "9000")
    assert status_tui.main(["--status"]) == 0
    assert seen[-1] == "http://10.0.0.5:9000"  # scheme added once, not doubled

    monkeypatch.setenv("MOKURO_BRIDGE_HOST", "::1")
    monkeypatch.setenv("MOKURO_BRIDGE_PORT", "62642")
    assert status_tui.main(["--status"]) == 0
    assert seen[-1] == "http://[::1]:62642"

    monkeypatch.setenv("MOKURO_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("MOKURO_BRIDGE_PORT", "")  # empty must not crash the CLI
    assert status_tui.main(["--status"]) == 0
    assert seen[-1] == "http://127.0.0.1:62642"


def test_run_status_re_reads_the_terminal_size_each_frame(monkeypatch):
    stream = FakeTTY()
    sizes = iter([(100, 30), (80, 24)])

    def fake_size(*args, **kwargs):
        return next(sizes)

    monkeypatch.setattr(status_tui, "_terminal_size", fake_size)
    code = status_tui.run_status("http://127.0.0.1:62642", stream=stream,
                                 fetch=lambda url: health(), keys=iter(["r", "q"]))
    assert code == 0
    plain = SGR.sub("", stream.getvalue())
    borders = [line for line in plain.split("\n") if "┌" in line and "┐" in line]
    widths = {status_tui.display_width(line[line.index("┌"):line.rindex("┐") + 1]) for line in borders}
    assert widths == {100, 80}, widths
    assert stream.getvalue().endswith(status_tui.CURSOR_SHOW + status_tui.ALT_OFF)
