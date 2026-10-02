"""Packaging has to stay in step with the two install paths.

Since the bridge ships both as a wheel (pipx/pip) and as a git checkout, two
things can silently rot:

  * the dependency list is written twice, in requirements.txt and in
    pyproject.toml, and a package added to one but not the other works for
    whoever tested it and breaks for everyone else;
  * a new subpackage under mokuro_bridge/ is invisible unless it is listed, so
    it would be missing from the wheel with no error anywhere.

Both are cheap to assert, and this project already prefers a test over a
comment (see tests/test_imports.py).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomli = pytest.importorskip("tomli", reason="needs tomllib (3.11+) or tomli")
    tomllib = tomli

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The fork checkout at ./mokuro is a sibling directory, not part of this
# project's distribution. Nothing under it may ever be packaged.
VENDOR_DIR = "mokuro"


def _requirements(filename: str) -> list[str]:
    """Requirement lines from *filename*, comments and -r includes dropped."""
    lines = []
    for raw in (REPO_ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        lines.append(line)
    return lines


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_base_dependencies_match_requirements_txt(pyproject):
    assert set(pyproject["project"]["dependencies"]) == set(_requirements("requirements.txt"))


@pytest.mark.parametrize(
    "extra,requirements_file",
    [
        ("ocr", "requirements-ocr.txt"),
        ("drive", "requirements-drive.txt"),
        ("onedrive", "requirements-onedrive.txt"),
        ("dev", "requirements-dev.txt"),
    ],
)
def test_each_extra_matches_its_requirements_file(pyproject, extra, requirements_file):
    assert set(pyproject["project"]["optional-dependencies"][extra]) == set(
        _requirements(requirements_file)
    )


def _names(requirements: list[str]) -> set[str]:
    """Just the package names, so two floors for one package can be compared."""
    return {re.split(r"[<>=!~;\[]", dep, maxsplit=1)[0].strip().lower() for dep in requirements}


def test_the_cloud_extra_covers_both_upload_sdks_without_torch(pyproject):
    extras = pyproject["project"]["optional-dependencies"]
    assert _names(extras["cloud"]) == _names(extras["drive"]) | _names(extras["onedrive"])
    # requests is listed in both provider files at different floors (>=2.20.0 for
    # Drive, >=2.31 for Graph); the combined extra must carry the stricter one,
    # not quietly the older.
    assert "requests>=2.31" in extras["cloud"]
    # The whole point of the base install is that it does not drag in PyTorch.
    assert not any(dep.startswith("mokuro") for dep in extras["cloud"])
    assert not any(dep.startswith("torch") for dep in extras["cloud"])


def test_the_ocr_engine_is_not_a_base_dependency(pyproject):
    """v0.5.1 made mokuro optional; packaging must not undo that."""
    assert not any(
        dep.split(">")[0].split("=")[0].split("[")[0] == "mokuro"
        for dep in pyproject["project"]["dependencies"]
    )


def test_the_version_has_a_single_source(pyproject):
    """The version lives in mokuro_bridge/__init__.py, not in pyproject."""
    assert "version" in pyproject["project"]["dynamic"]
    assert "version" not in pyproject["project"]
    assert (
        pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        == "mokuro_bridge.__version__"
    )


def test_the_documented_python_floor_is_the_packaged_one(pyproject):
    assert pyproject["project"]["requires-python"] == ">=3.10"


def test_every_package_directory_is_listed_for_the_wheel(pyproject):
    """A new subpackage must be added to [tool.setuptools] packages.

    Setting `packages` explicitly is what keeps the ./mokuro fork out of the
    wheel, and the cost of that is this list going stale.
    """
    declared = set(pyproject["tool"]["setuptools"]["packages"])
    actual = {
        ".".join(init.parent.relative_to(REPO_ROOT).parts)
        for init in (REPO_ROOT / "mokuro_bridge").rglob("__init__.py")
    }
    assert actual == declared, (
        f"pyproject lists {sorted(declared)} but the tree has {sorted(actual)}"
    )


def test_the_vendored_fork_is_not_packaged(pyproject):
    declared = pyproject["tool"]["setuptools"]["packages"]
    assert not any(pkg == VENDOR_DIR or pkg.startswith(f"{VENDOR_DIR}.") for pkg in declared)
    # And the fork really is there, so the exclusion above means something.
    # ./mokuro is a gitignored sibling checkout, not part of the repository, so
    # it is absent in a clean clone and in CI; only check it where it exists.
    if (REPO_ROOT / VENDOR_DIR).is_dir():
        assert (REPO_ROOT / VENDOR_DIR / "mokuro").is_dir()


def test_the_console_scripts_point_at_real_callables(pyproject):
    scripts = pyproject["project"]["scripts"]
    assert set(scripts) == {"mokuro-bridge", "mokuro-bridge-ocr"}
    for name, target in scripts.items():
        module_name, _, attribute = target.partition(":")
        assert module_name and attribute, f"{name} is not module:callable"
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{name} -> {target} is not callable"


def test_python_dash_m_entry_point_is_shipped():
    """`python -m mokuro_bridge` works wherever the console script is missing."""
    assert (REPO_ROOT / "mokuro_bridge" / "__main__.py").is_file()


# ── the two checkout entry points must keep working ─────────────────────


def test_the_checkout_wrappers_still_resolve():
    """run.sh, the launchd plist and the README all invoke these scripts.

    server.py and ocr_folder.py are compatibility shims now; this fails if a
    refactor quietly drops one, which would break every existing install that
    was set up before the wheel existed.
    """
    assert (REPO_ROOT / "server.py").is_file()
    assert (REPO_ROOT / "ocr_folder.py").is_file()

    server = importlib.import_module("server")
    assert callable(server.main)
    # `uvicorn server:app` is a documented dev invocation.
    assert server.app is not None

    ocr_folder = importlib.import_module("ocr_folder")
    assert callable(ocr_folder.main)


def test_the_launcher_scripts_still_target_server_py():
    for name in ("run.sh", "install-launchd.sh"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "server.py" in text, f"{name} no longer launches server.py"
    plist = (REPO_ROOT / "com.mokuro-bridge.plist").read_text(encoding="utf-8")
    assert "__SERVER__" in plist, "the plist template lost its server placeholder"


# ── the installed-vs-checkout output directory ──────────────────────────


@pytest.mark.parametrize(
    "package_file",
    [
        "/usr/lib/python3.13/site-packages/mokuro_bridge/config.py",
        "/home/u/.local/pipx/venvs/mokuro-bridge/lib/python3.13/site-packages/mokuro_bridge/config.py",
        "/usr/lib/python3/dist-packages/mokuro_bridge/config.py",
    ],
)
def test_an_installed_wheel_writes_beside_the_home_directory(package_file):
    """Never inside the venv: a pipx upgrade rebuild would strand the output."""
    from mokuro_bridge.config import _default_output_dir

    resolved = _default_output_dir(package_file)
    assert resolved == Path.home() / "mokuro-bridge" / "output"
    assert "site-packages" not in str(resolved)
    assert "dist-packages" not in str(resolved)


@pytest.mark.parametrize(
    "package_file",
    [
        str(REPO_ROOT / "mokuro_bridge" / "config.py"),
        "/Users/someone/Projects/mokuro-bridge/mokuro_bridge/config.py",
    ],
)
def test_a_checkout_keeps_using_its_own_output_directory(package_file):
    """The documented default for an existing git-checkout install is <repo>/output."""
    from mokuro_bridge.config import _default_output_dir

    expected = Path(package_file).resolve().parent.parent / "output"
    assert _default_output_dir(package_file) == expected
