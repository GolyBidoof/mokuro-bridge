"""The upstream pin. No network: `check` and `refresh` take a fetcher."""
from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest

from mokuro_bridge import series_reference


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _body(repo: str, commit: str, path: str) -> bytes:
    return f"{repo}@{commit}:{path}\n".encode()


def _pin(*, repo="Gnathonic/mokuro-reader", commit="a" * 40, branch="main") -> dict:
    files = [
        {"path": "src/lib/metadata/series-file.ts", "role": "generator"},
        *({"path": f"src/{role}.ts", "role": role} for role in series_reference.REQUIRED_ROLES
          if role not in ("generator", "spec")),
        {"path": "docs/spec.md", "role": "spec"},
    ]
    return {
        "schema": 1,
        "upstream": [
            {
                "repo": repo,
                "license": "GPL-3.0",
                "branch": branch,
                "commit": commit,
                "files": [
                    {
                        "path": entry["path"],
                        "role": entry["role"],
                        "sha256": _sha(_body(repo, commit, entry["path"])),
                        "bytes": len(_body(repo, commit, entry["path"])),
                    }
                    for entry in files
                ],
            }
        ],
    }


class FakeGitHub:

    def __init__(self, pin: dict, *, head: str = None, head_body=None, fail=False):
        self.pin = pin
        self.head = head or pin["upstream"][0]["commit"]
        self.head_body = head_body
        self.fail = fail
        self.requests: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        if self.fail:
            raise urllib.error.URLError("offline")
        upstream = self.pin["upstream"][0]
        if "/commits/" in url:
            return json.dumps({"sha": self.head}).encode()
        prefix = "https://raw.githubusercontent.com/"
        _, _, rest = url.partition(prefix)
        _owner, _repo, remainder = rest.split("/", 2)
        commit, _, path = remainder.partition("/")
        for entry in upstream["files"]:
            if entry["path"] == path:
                if self.head_body is not None and commit != upstream["commit"]:
                    return self.head_body(path)
                return _body(upstream["repo"], upstream["commit"], path)
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)


def test_shipped_pin_verifies():
    assert series_reference.verify() == []


def test_pinned_files_flatten_repo_and_commit():
    flattened = series_reference.pinned_files()
    assert flattened
    assert all(item["commit"] and item["sha256"] for item in flattened)
    roles = {item["role"] for item in flattened}
    assert set(series_reference.REQUIRED_ROLES) <= roles


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pin: pin.update(upstream=[]),
        lambda pin: pin["upstream"][0].update(commit="short"),
        lambda pin: pin["upstream"][0].update(license=""),
        lambda pin: pin["upstream"][0]["files"][0].update(sha256="nothex"),
        lambda pin: pin["upstream"][0]["files"][0].update(role=""),
        lambda pin: pin["upstream"][0].update(files=[]),
    ],
)
def test_verify_reports_broken_pins(mutate):
    pin = _pin()
    mutate(pin)
    assert series_reference.verify(pin)


def test_verify_reports_a_missing_required_role():
    pin = _pin()
    for upstream in pin["upstream"]:
        upstream["files"] = [f for f in upstream["files"] if f["role"] != "writer"]
    problems = series_reference.verify(pin)
    assert any("writer" in problem for problem in problems)


def test_check_accepts_a_matching_pin():
    pin = _pin()
    report = series_reference.check(pin, fetcher=FakeGitHub(pin))
    assert report["ok"] is True
    assert report["checked"] == len(series_reference.pinned_files(pin))
    assert report["mismatched"] == []
    assert report["moved"] == []
    assert report["drift"] == []
    assert report["errors"] == []


def test_check_reports_a_mismatched_pin():
    pin = _pin()
    upstream = pin["upstream"][0]
    real_body = _body(upstream["repo"], upstream["commit"], upstream["files"][0]["path"])
    upstream["files"][0]["sha256"] = _sha(b"tampered")
    report = series_reference.check(pin, fetcher=FakeGitHub(pin))
    assert report["ok"] is False
    assert len(report["mismatched"]) == 1
    assert report["mismatched"][0]["actual"] == _sha(real_body)


def test_check_reports_a_branch_that_moved_and_changed_a_file():
    pin = _pin()
    head = "b" * 40
    report = series_reference.check(
        pin, fetcher=FakeGitHub(pin, head=head, head_body=lambda path: b"changed\n")
    )
    assert report["ok"] is True
    assert report["moved"][0]["head"] == head
    assert len(report["drift"]) == len(series_reference.pinned_files(pin))
    assert report["drift"][0]["head_sha256"] == _sha(b"changed\n")


def test_check_reports_a_branch_that_moved_without_changing_pinned_files():
    pin = _pin()
    report = series_reference.check(pin, fetcher=FakeGitHub(pin, head="c" * 40))
    assert report["ok"] is True
    assert report["moved"]
    assert report["drift"] == []


def test_check_never_raises_on_a_dead_network():
    pin = _pin()
    report = series_reference.check(pin, fetcher=FakeGitHub(pin, fail=True))
    assert report["ok"] is False
    assert report["errors"]
    assert report["checked"] == 0


def test_refresh_rewrites_the_pin_atomically(monkeypatch, tmp_path):
    from mokuro_bridge import series_reference as module

    target = tmp_path / "series_json_pin.json"
    pin = _pin(commit="a" * 40)
    target.write_text(json.dumps(pin), encoding="utf-8")
    monkeypatch.setattr(module, "PIN_PATH", target)
    head = "d" * 40

    refreshed = module.refresh(fetcher=FakeGitHub(pin, head=head), now="2026-10-15T22:00:00Z")
    assert refreshed["upstream"][0]["commit"] == head
    assert module.verify(refreshed) == []
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == refreshed
    assert refreshed["upstream"][0]["license"] == "GPL-3.0"
    assert not list(target.parent.glob(".*tmp*"))


def test_load_pin_reports_an_unusable_file(monkeypatch, tmp_path):
    from mokuro_bridge import series_reference as module

    target = tmp_path / "pin.json"
    monkeypatch.setattr(module, "PIN_PATH", target)
    with pytest.raises(module.ReferenceError):
        module.load_pin()
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(module.ReferenceError):
        module.load_pin()
    assert module.verify() and "not valid JSON" in module.verify()[0]


def test_real_pin_records_the_format_provenance():
    pin = series_reference.load_pin()
    assert "v1.9.0" in pin["introduced_in"]
    repos = {entry["repo"]: entry for entry in pin["upstream"]}
    assert repos["Gnathonic/mokuro-reader"]["license"] == "GPL-3.0"
    assert repos["Gnathonic/mokuro-bunko"]["license"] == "MPL-2.0"
    package = Path(series_reference.__file__).parent
    assert not (package / "upstream").exists()
