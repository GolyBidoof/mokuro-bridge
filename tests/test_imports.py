"""The base install must not need anything beyond its own dependencies.

mokuro-bridge deliberately ships without the OCR engine and the three cloud
SDKs. Those are optional extras, so nothing at *module* level may import them:
an eager import would make `pip install -r requirements.txt` insufficient to
even start the server. This walks the AST and checks that, rather than trusting
a grep.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "mokuro_bridge"

# What requirements.txt installs, plus what fastapi/uvicorn pull in and the code
# imports directly.
BASE = {
    "fastapi", "starlette", "httpx", "uvicorn", "multipart",
    "pydantic", "anyio", "click", "typing_extensions",
}

MODULES = sorted(PACKAGE.rglob("*.py"))


def module_level_imports(path: Path) -> set[str]:
    """Root module names imported at the top level of *path* (not in a function)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_module_level_imports_are_stdlib_or_base_dependencies(path):
    third_party = module_level_imports(path) - set(sys.stdlib_module_names)
    unexpected = third_party - BASE
    assert not unexpected, (
        f"{path.relative_to(REPO_ROOT)} imports {sorted(unexpected)} at module level. "
        "Import it inside the function that needs it, or add it to requirements.txt."
    )


@pytest.mark.parametrize("name", ["mokuro", "googleapiclient", "msgraph", "mega", "webdav3", "keyring"])
def test_optional_integrations_are_never_eager(name):
    offenders = [str(p.relative_to(REPO_ROOT)) for p in MODULES if name in module_level_imports(p)]
    assert not offenders, f"{name} is imported at module level in {offenders}"
