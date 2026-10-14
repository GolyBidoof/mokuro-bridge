"""Which mokuro is installed, and the explicit command that replaces it.

Hermetic: the distribution, the module path and the command runner are all
injectable, so nothing here reads this machine's real install, runs pip, opens
a socket or uninstalls anything. The install tests monkeypatch ``_run_shell``
the same way the update tests do.
"""

from __future__ import annotations

import json

import pytest

from mokuro_bridge import cli
from mokuro_bridge import mokuro_flavor as mf


@pytest.fixture(autouse=True)
def _clean_detect_cache(monkeypatch):
    """The detection cache is module state; no test may inherit another's."""
    monkeypatch.setattr(mf, "_cached", None)


class FakeDist:
    """The two attributes the flavor code reads from a distribution."""

    def __init__(self, version="0.2.2", direct_url=None):
        self.version = version
        self._direct_url = direct_url

    def read_text(self, name):
        return self._direct_url if name == "direct_url.json" else None


def _fork_checkout(tmp_path):
    """A directory that looks like a git clone of the fork."""
    root = tmp_path / "mokuro-checkout"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        '[remote "fork"]\n\turl = https://github.com/GolyBidoof/mokuro.git\n',
        encoding="utf-8",
    )
    return root


def _fork_package(tmp_path):
    """A mokuro package directory carrying the fork's OCR API."""
    package = tmp_path / "mokuro"
    package.mkdir()
    (package / "manga_page_ocr.py").write_text(
        "class MangaPageOcr:\n    def detect_and_extract(self):\n        pass\n",
        encoding="utf-8",
    )
    return package


def test_nothing_installed_is_absent():
    assert mf.classify_flavor(distribution_present=False, module_present=False) == "absent"


def test_a_plain_index_install_is_upstream():
    assert mf.classify_flavor(distribution_present=True, module_present=True) == "upstream"


def test_a_git_url_naming_the_fork_is_the_fork():
    assert mf.classify_flavor(
        distribution_present=True,
        direct_url="git+https://github.com/GolyBidoof/mokuro.git@v0.4.0b",
        module_present=True,
    ) == "fork"


def test_a_local_checkout_of_the_fork_is_the_fork(tmp_path):
    checkout = _fork_checkout(tmp_path)
    assert mf.classify_flavor(
        distribution_present=True,
        direct_url=checkout.as_uri(),
        module_present=True,
    ) == "fork"


def test_a_package_directory_with_the_fork_api_is_the_fork(tmp_path):
    package = _fork_package(tmp_path)
    assert mf.classify_flavor(
        distribution_present=True,
        package_dir=package,
        module_present=True,
    ) == "fork"


def test_an_importable_mokuro_with_no_metadata_is_unknown(tmp_path):
    assert mf.classify_flavor(
        distribution_present=False,
        package_dir=tmp_path / "mokuro",
        module_present=True,
    ) == "unknown"


def test_a_local_install_with_no_module_to_confirm_is_unknown(tmp_path):
    assert mf.classify_flavor(
        distribution_present=True,
        direct_url=tmp_path.as_uri(),
        module_present=False,
    ) == "unknown"


def test_the_version_string_is_never_used_to_pick_the_flavor(tmp_path):
    """The stale-metadata trap: 0.2.2 metadata on a 0.4.0b fork."""
    package = _fork_package(tmp_path)
    assert mf.classify_flavor(
        distribution_present=True,
        direct_url=None,
        package_dir=package,
        module_present=True,
    ) == "fork"


def test_a_checkout_naming_the_fork_is_recognised(tmp_path):
    assert mf._checkout_is_fork(_fork_checkout(tmp_path)) is True


def test_a_checkout_of_upstream_is_not_the_fork(tmp_path):
    root = tmp_path / "upstream"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/kha-white/mokuro.git\n',
        encoding="utf-8",
    )
    # The nearest .git wins, so a nested repo is not mistaken for an outer fork.
    assert mf._checkout_is_fork(root) is False


def test_a_worktree_pointer_is_followed(tmp_path):
    git_dir = tmp_path / "real-git-dir"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/GolyBidoof/mokuro\n',
        encoding="utf-8",
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    assert mf._checkout_is_fork(worktree) is True


def test_a_missing_checkout_is_not_the_fork(tmp_path):
    assert mf._checkout_is_fork(tmp_path / "nope") is False


def test_a_file_url_becomes_a_path(tmp_path):
    assert mf._file_url_path(tmp_path.as_uri()) == tmp_path


def test_a_non_file_url_has_no_path():
    assert mf._file_url_path("https://github.com/GolyBidoof/mokuro.git") is None


def test_detect_reports_the_distribution_and_the_flavor(monkeypatch, tmp_path):
    package = _fork_package(tmp_path)
    monkeypatch.setattr(mf, "_distribution", lambda: FakeDist(version="0.2.2"))
    monkeypatch.setattr(mf, "_module_path", lambda: package / "__init__.py")

    found = mf.detect()
    assert found["flavor"] == "fork"
    assert found["installed"] is True
    assert found["distribution"] is True
    assert found["version"] == "0.2.2", "the metadata is reported, not used"
    assert found["module_path"].endswith("__init__.py")


def test_detect_reports_absent_when_there_is_no_mokuro(monkeypatch):
    monkeypatch.setattr(mf, "_distribution", lambda: None)
    monkeypatch.setattr(mf, "_module_path", lambda: None)
    found = mf.detect()
    assert found["flavor"] == "absent"
    assert found["installed"] is False
    assert found["version"] is None


def test_detect_caches_and_refresh_bypasses_it(monkeypatch, tmp_path):
    package = _fork_package(tmp_path)
    calls = []
    dist = FakeDist()
    monkeypatch.setattr(mf, "_distribution", lambda: calls.append(1) or dist)
    monkeypatch.setattr(mf, "_module_path", lambda: package / "__init__.py")

    mf.detect()
    mf.detect()
    assert len(calls) == 1, "a health poll may not re-read the filesystem"
    mf.detect(refresh=True)
    assert len(calls) == 2


def test_a_malformed_direct_url_is_ignored(monkeypatch, tmp_path):
    package = _fork_package(tmp_path)
    monkeypatch.setattr(mf, "_distribution", lambda: FakeDist(direct_url="{not json"))
    monkeypatch.setattr(mf, "_module_path", lambda: package / "__init__.py")
    assert mf.detect()["flavor"] == "fork"


def test_health_fields_always_carry_the_flavor(monkeypatch):
    monkeypatch.setattr(mf, "_distribution", lambda: None)
    monkeypatch.setattr(mf, "_module_path", lambda: None)
    assert mf.health_fields() == {"mokuro_flavor": "absent"}


def test_the_notice_names_the_single_switch_command():
    line = mf.notice_line({"flavor": "upstream"})
    assert line is not None
    assert mf.SWITCH_COMMAND in line
    assert "GolyBidoof/mokuro" in line
    assert mf.FORK_TAG in line


@pytest.mark.parametrize("flavor", ["unknown", "upstream"])
def test_the_notice_fires_for_an_engine_that_is_not_the_fork(flavor):
    assert mf.notice_line({"flavor": flavor}) is not None


@pytest.mark.parametrize("flavor", ["fork", "absent"])
def test_the_notice_is_silent_when_there_is_nothing_to_switch_to(flavor):
    assert mf.notice_line({"flavor": flavor}) is None


def test_the_env_switch_silences_the_notice(monkeypatch):
    monkeypatch.setenv(mf.NOTICE_SWITCH, "0")
    assert mf.notice_enabled() is False
    assert mf.notice_line({"flavor": "upstream"}) is None


def test_the_notice_defaults_to_on(monkeypatch):
    monkeypatch.delenv(mf.NOTICE_SWITCH, raising=False)
    assert mf.notice_enabled() is True


def test_announce_logs_through_the_shared_logger(monkeypatch):
    seen = {}
    monkeypatch.setattr(mf.log, "info", lambda tag, msg: seen.update(tag=tag, msg=msg))
    line = mf.announce({"flavor": "upstream"})
    assert seen["tag"] == "mokuro"
    assert seen["msg"] == line


def test_a_failing_logger_does_not_raise(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("console gone")

    monkeypatch.setattr(mf.log, "info", boom)
    assert mf.announce({"flavor": "upstream"}) is None


def test_announce_is_silent_for_the_fork(monkeypatch):
    monkeypatch.setattr(
        mf.log, "info", lambda *a, **k: pytest.fail("the fork must not be announced")
    )
    assert mf.announce({"flavor": "fork"}) is None


def test_the_probe_command_is_a_python_c_import(monkeypatch):
    import shlex

    command = mf.probe_command("/opt/py/bin/python3")
    parts = shlex.split(command)
    assert parts[0] == "/opt/py/bin/python3"
    assert parts[1] == "-c"
    assert "import mokuro" in parts[2]
    assert "direct_url.json" in parts[2]


def test_parse_probe_needs_evidence_to_call_a_local_install_the_fork(tmp_path):
    """A local path alone is not proof; only the markers or the remote are."""
    package = _fork_package(tmp_path)
    unknown = {"version": "0.4.0b0", "url": (tmp_path / "elsewhere").as_uri(),
               "path": str(package / "__init__.py")}
    # The package directory has the fork marker, so this one is the fork.
    assert mf.parse_probe(json.dumps(unknown))["flavor"] == "fork"

    plain = tmp_path / "plain" / "mokuro"
    plain.mkdir(parents=True)
    (plain / "manga_page_ocr.py").write_text("class MangaPageOcr:\n    pass\n")
    upstream = {"version": "0.2.2", "url": "", "path": str(plain / "__init__.py")}
    assert mf.parse_probe(json.dumps(upstream))["flavor"] == "upstream"


def test_parse_probe_reads_the_fork_from_its_url():
    payload = {
        "version": "0.4.0b0",
        "url": "https://github.com/GolyBidoof/mokuro.git",
        "path": None,
    }
    assert mf.parse_probe(json.dumps(payload))["flavor"] == "fork"


def test_parse_probe_reads_the_fork_from_its_module_path(tmp_path):
    package = _fork_package(tmp_path)
    payload = {"version": "0.4.0b", "url": "", "path": str(package / "__init__.py")}
    assert mf.parse_probe(json.dumps(payload))["flavor"] == "fork"


def test_parse_probe_refuses_to_guess_from_garbage():
    assert mf.parse_probe("Traceback (most recent call last):")["flavor"] == "unknown"
    assert mf.parse_probe("")["flavor"] == "unknown"


# Derived, not hardcoded: a literal here drifted the moment the fork moved to a
# stable 0.4.0 tag, and the test failed on a change that was correct. Asserting
# against the module's own constant keeps the commands and the target in step.
TARGET = mf.INSTALL_TARGET


@pytest.mark.parametrize(
    "method,expected",
    [
        (
            "pip",
            [
                "/opt/py/bin/python3 -m pip uninstall -y mokuro",
                f"/opt/py/bin/python3 -m pip install --upgrade '{TARGET}'",
            ],
        ),
        (
            "git",
            [
                "/opt/py/bin/python3 -m pip uninstall -y mokuro",
                f"/opt/py/bin/python3 -m pip install --upgrade '{TARGET}'",
            ],
        ),
        (
            "pipx",
            [
                "pipx runpip mokuro-bridge uninstall -y mokuro",
                f"pipx runpip mokuro-bridge install --upgrade '{TARGET}'",
            ],
        ),
        (
            "uv",
            [f"uv tool install --force --with '{TARGET}' mokuro-bridge"],
        ),
        ("unknown", []),
    ],
)
def test_each_install_method_maps_to_its_exact_commands(method, expected):
    assert cli._mokuro_install_commands(method, executable="/opt/py/bin/python3") == expected


def test_every_command_starts_by_removing_the_old_distribution():
    """The fork shares the name, so the uninstall has to come first."""
    for method in ("pip", "git", "pipx"):
        first = cli._mokuro_install_commands(method)[0]
        assert "uninstall" in first


PROBE_FORK = json.dumps(
    {
        "version": "0.4.0b0",
        "url": "https://github.com/GolyBidoof/mokuro.git",
        "path": "/tmp/fake/mokuro/__init__.py",
    }
)


def _state(flavor, **overrides):
    payload = {
        "flavor": flavor,
        "installed": flavor != "absent",
        "distribution": flavor != "absent",
        "version": "0.2.2",
        "direct_url": None,
        "module_path": None,
    }
    payload.update(overrides)
    return payload


def _install_env(monkeypatch, *, flavor="upstream", method="pip", verify=(0, PROBE_FORK)):
    ran: list[str] = []
    monkeypatch.setattr(cli.mokuro_flavor, "detect", lambda **kw: _state(flavor))
    monkeypatch.setattr(cli, "_is_externally_managed", lambda prefix=None: False)
    monkeypatch.setattr(cli, "_install_method", lambda *a, **k: method)

    def runner(command, *, capture=False):
        ran.append(command)
        if capture:
            return verify
        return 0

    monkeypatch.setattr(cli, "_run_shell", runner)
    return ran


def test_the_fork_is_left_alone_and_nothing_runs(monkeypatch, capsys):
    ran = _install_env(monkeypatch, flavor="fork")
    assert cli._run_install_mokuro() == 0
    assert ran == []
    out = capsys.readouterr().out
    assert "already installed" in out
    assert "before:" in out


def test_an_upstream_install_is_replaced_and_verified(monkeypatch, capsys):
    ran = _install_env(monkeypatch, flavor="upstream", method="pip")
    assert cli._run_install_mokuro() == 0
    out = capsys.readouterr().out
    expected = cli._mokuro_install_commands("pip")
    # Every mutating command is printed before it is run.
    for command in expected:
        assert f"about to run: {command}" in out
    assert ran[: len(expected)] == expected, "only the detected commands run"
    assert "verifying:" in out
    assert "after: mokuro distribution=yes flavor=fork version=0.4.0b0" in out
    assert "done:" in out


def test_the_uv_method_uses_the_tool_installer(monkeypatch):
    ran = _install_env(monkeypatch, method="uv")
    assert cli._run_install_mokuro() == 0
    assert ran[0] == cli._mokuro_install_commands("uv")[0]


def test_a_failed_install_is_reported_with_its_status(monkeypatch, capsys):
    monkeypatch.setattr(cli.mokuro_flavor, "detect", lambda **kw: _state("upstream"))
    monkeypatch.setattr(cli, "_is_externally_managed", lambda prefix=None: False)
    monkeypatch.setattr(cli, "_install_method", lambda *a, **k: "pip")
    monkeypatch.setattr(cli, "_run_shell", lambda command, **kw: 4)

    assert cli._run_install_mokuro() == 4
    assert "install failed (4)" in capsys.readouterr().out


def test_a_verification_that_cannot_import_is_a_failure(monkeypatch, capsys):
    _install_env(monkeypatch, verify=(1, "ModuleNotFoundError: No module named 'mokuro'"))
    assert cli._run_install_mokuro() == 1
    out = capsys.readouterr().out
    assert "verification failed (1)" in out
    assert "No module named 'mokuro'" in out


def test_an_install_that_is_not_the_fork_is_a_failure(monkeypatch, capsys):
    upstream_report = json.dumps({"version": "0.2.2", "url": "", "path": None})
    _install_env(monkeypatch, verify=(0, upstream_report))
    assert cli._run_install_mokuro() == 1
    assert "does not report the fork" in capsys.readouterr().out


def test_an_externally_managed_interpreter_is_refused(monkeypatch, capsys):
    ran = _install_env(monkeypatch)
    monkeypatch.setattr(cli, "_is_externally_managed", lambda prefix=None: True)
    assert cli._run_install_mokuro() == 2
    out = capsys.readouterr().out
    assert "refusing" in out
    assert "PEP 668" in out
    assert ran == [], "a system Python that pip refuses must not be touched"


def test_an_install_that_cannot_be_told_apart_is_refused(monkeypatch, capsys):
    ran = _install_env(monkeypatch, method="unknown")
    assert cli._run_install_mokuro() == 2
    out = capsys.readouterr().out
    assert "refusing" in out
    assert mf.INSTALL_TARGET in out
    assert ran == []
