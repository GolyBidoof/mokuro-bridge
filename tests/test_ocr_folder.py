from __future__ import annotations

import io
import json
import sys

import pytest

# The implementation moved into the package so a wheel can ship the
# mokuro-bridge-ocr console script; the root ocr_folder.py is now a wrapper.
from mokuro_bridge import ocr_folder


def test_help_names_the_command_that_was_invoked(monkeypatch, capsys):
    """The usage line must not claim to be a file that is not on your PATH.

    Now that `mokuro-bridge-ocr` is a real entry point, `prog` follows argv[0]
    rather than being pinned to the checkout script's filename.
    """
    monkeypatch.setattr(sys, "argv", ["mokuro-bridge-ocr", "--help"])
    with pytest.raises(SystemExit):
        ocr_folder.main()
    assert "usage: mokuro-bridge-ocr" in capsys.readouterr().out


class _Response:
    def __init__(self, frames):
        self._body = b"".join(
            (json.dumps(frame) + "\n").encode("utf-8") for frame in frames
        )
        self._stream = io.BytesIO(self._body)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self._stream)

    def close(self):
        self._stream.close()


def test_finalize_accepts_only_success_status(monkeypatch):
    monkeypatch.setattr(
        ocr_folder.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response([
            {"stage": "done", "status": "success", "message": "ok", "pages": 12},
        ]),
    )
    result = ocr_folder.finalize(
        "http://127.0.0.1:62642", "session", "local", True
    )
    assert result["status"] == "success"


def test_finalize_rejects_empty_terminal_stream(monkeypatch):
    monkeypatch.setattr(
        ocr_folder.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response([]),
    )
    with pytest.raises(SystemExit, match="without a terminal success"):
        ocr_folder.finalize(
            "http://127.0.0.1:62642", "session", "local", True
        )


def test_finalize_rejects_partial_upload_success_stage(monkeypatch):
    monkeypatch.setattr(
        ocr_folder.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response([
            {
                "stage": "done",
                "status": "partial_upload",
                "message": "one file failed",
            },
        ]),
    )
    with pytest.raises(SystemExit, match="partial_upload"):
        ocr_folder.finalize(
            "http://127.0.0.1:62642", "session", "mega", True
        )
