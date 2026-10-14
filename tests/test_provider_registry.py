"""The provider registry is the dispatch seam.

Adding a backend means adding data to the registry, not editing a dispatch
chain in ``providers/__init__.py``. This suite registers a fake provider and
drives it through the public paths (the upload-methods listing,
``remote_file_exists``, ``upload_file`` and the setup runner) to prove that no
dispatch code has to change for a new provider to be reachable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mokuro_bridge import accounts, providers
from mokuro_bridge.providers.base import Provider, ProviderAccount, walk_folders

FAKE_ROOT = "FakeRoot"


def _fake_provider(calls: dict) -> Provider:
    """A provider that records every call and answers from a fixed reply."""

    def configured(name: str = accounts.DEFAULT_NAME) -> bool:
        return name == accounts.DEFAULT_NAME

    def creds_source(name: str = accounts.DEFAULT_NAME) -> str:
        return "fake-store"

    def upload(name, root, remote_dir, local_path, on_progress, overwrite):
        calls["upload"] = (name, root, remote_dir, Path(local_path).name, overwrite)
        return True, None, "https://example.invalid/fake"

    def remote_exists(name, remote_dir, filename) -> bool:
        calls["remote_exists"] = (name, remote_dir, filename)
        return filename == "present.cbz"

    def run_setup(name=None, root="", label=""):
        calls["setup"] = (name, root, label)

    def account(instance) -> ProviderAccount:
        return ProviderAccount(
            label=instance.display_name_for("Fake Cloud"),
            extra={"root": instance.root_path},
            current_folder=f"{instance.root_path} (fake)",
        )

    return Provider(
        id="fake",
        label="Fake Cloud",
        configured=configured,
        creds_source=creds_source,
        upload=upload,
        remote_exists=remote_exists,
        run_setup=run_setup,
        account=account,
    )


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """The real registry plus a fake provider, on a clean account store."""
    real_default_root = accounts.default_root

    def default_root(provider: str) -> str:
        if provider == "fake":
            return FAKE_ROOT
        return real_default_root(provider)

    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(accounts, "default_root", default_root)
    monkeypatch.setattr(accounts, "PROVIDERS", accounts.PROVIDERS + ("fake",))
    # Replace the dict object so register_provider mutates a copy that
    # monkeypatch restores when the test ends.
    monkeypatch.setattr(providers, "_PROVIDERS", dict(providers._PROVIDERS))
    monkeypatch.setattr(providers, "_UPLOAD_METHOD_STATE_FILE", tmp_path / "default.json")
    monkeypatch.delenv("MOKURO_BRIDGE_UPLOAD_DEFAULT", raising=False)
    calls: dict = {}
    providers.register_provider(_fake_provider(calls))
    return calls


def test_the_three_shipped_providers_are_registered():
    assert set(providers._PROVIDERS) == {"mega", "drive", "onedrive"}


def test_a_registered_provider_appears_in_the_upload_methods_listing(fake):
    methods = providers._build_upload_methods()

    assert "fake" in methods
    fake_method = methods["fake"]
    assert fake_method.name == "Fake Cloud"
    assert fake_method.configured is True
    assert fake_method.extra["provider"] == "fake"
    assert fake_method.extra["creds_source"] == "fake-store"
    assert fake_method.extra["root"] == FAKE_ROOT
    assert providers._method_current_folder("fake") == f"{FAKE_ROOT} (fake)"


def test_remote_file_exists_is_dispatched_to_the_registered_provider(fake):
    assert providers.remote_file_exists("fake", FAKE_ROOT, "present.cbz") is True
    assert providers.remote_file_exists("fake", FAKE_ROOT, "absent.cbz") is False
    assert fake["remote_exists"] == ("default", FAKE_ROOT, "absent.cbz")


def test_upload_is_dispatched_to_the_registered_provider(fake, tmp_path):
    payload = tmp_path / "vol.cbz"
    payload.write_bytes(b"x")

    assert providers.upload_file("fake", payload, f"{FAKE_ROOT}/Series", None) == (
        True,
        None,
        "https://example.invalid/fake",
    )
    assert fake["upload"] == (
        "default",
        FAKE_ROOT,
        f"{FAKE_ROOT}/Series",
        "vol.cbz",
        "fail",
    )


def test_the_setup_runner_comes_from_the_registry(fake):
    runner = providers.setup_runner("fake")
    assert runner is not None
    runner(None, root="R", label="L")
    assert fake["setup"] == (None, "R", "L")


def test_an_unknown_provider_has_no_setup_runner():
    assert providers.setup_runner("not-a-provider") is None


def test_walk_folders_read_only_stops_at_the_first_missing_segment():
    tree = {("root", "a"): "id-a"}

    def find(parent, name):
        return tree.get((parent, name))

    assert walk_folders("a/b", find=find) is None
    assert walk_folders("a", find=find) == "id-a"


def test_walk_folders_create_reaches_every_segment():
    made = []

    def find(parent, name):
        return None

    def make(parent, name):
        made.append((parent, name))
        return f"{parent}/{name}"

    assert walk_folders("a/b", find=find, make=make) == "root/a/b"
    assert made == [("root", "a"), ("root/a", "b")]


def test_walk_folders_empty_path_semantics():
    assert walk_folders("", find=lambda parent, name: None) is None
    with pytest.raises(ValueError):
        walk_folders("", find=lambda parent, name: None, require_nonempty=True)
