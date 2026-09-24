"""The bridge must run with no OCR engine installed.

mokuro drags in PyTorch, which is several GB, so it is deliberately not a base
dependency. Everything except OCR has to keep working without it, and /health
has to say so plainly rather than failing an import.

Each check runs in a clean interpreter with mokuro blocked on the meta path:
importing the package in-process would depend on test ordering, since Python
caches modules.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BLOCK_MOKURO = """
import importlib.abc, json, sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "mokuro" or name.startswith("mokuro."):
            raise ImportError("simulated: mokuro is not installed")
        return None

sys.meta_path.insert(0, Blocker())
"""


def run_without_mokuro(code: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", BLOCK_MOKURO + code],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"failed without mokuro:\n{result.stderr}"
    return json.loads(result.stdout)


def test_the_api_package_imports_without_mokuro():
    assert run_without_mokuro("""
import mokuro_bridge.api  # must not raise
print(json.dumps({"ok": True}))
""") == {"ok": True}


def test_the_engine_resolves_to_none_rather_than_failing():
    assert run_without_mokuro("""
from mokuro_bridge.ocr import _mokuro_pkg
print(json.dumps({"none": _mokuro_pkg is None}))
""") == {"none": True}


def test_server_side_imports_survive_without_mokuro():
    # server.py reads _mokuro_pkg to print its startup banner.
    assert run_without_mokuro("""
from mokuro_bridge.ocr import _MOKURO_REPO, _fork_supported, _mokuro_pkg
from mokuro_bridge.providers import _UPLOAD_METHODS
print(json.dumps({"none": _mokuro_pkg is None,
                  "methods": len(_UPLOAD_METHODS),
                  "repo": _MOKURO_REPO is None}))
""")["none"] is True


def test_health_reports_the_missing_engine_instead_of_crashing():
    payload = run_without_mokuro("""
from starlette.testclient import TestClient
import mokuro_bridge.api as api
print(json.dumps(TestClient(api.app).get("/health").json()))
""")
    assert payload["mokuro_installed"] is False
    assert "mokuro_version" not in payload


def test_the_fetch_accelerator_does_not_depend_on_the_engine():
    payload = run_without_mokuro("""
from starlette.testclient import TestClient
import mokuro_bridge.api as api
from mokuro_bridge import fetchproxy
health = TestClient(api.app).get("/health").json()
print(json.dumps({
    # Empty here because nothing bound ports in this process; the key existing
    # is what the downloader depends on, since that is how it finds the lanes.
    "ports": health.get("fetchProxyPorts"),
    "proxy": TestClient(fetchproxy.build_app([7])).get("/__bwdd_health").json()["bwddFetchProxy"],
}))
""")
    assert payload["ports"] == [], "the key must be present (empty until server.py binds)"
    assert payload["proxy"] is True
