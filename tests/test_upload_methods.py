"""Target resolution: bare provider ids, named accounts, sticky defaults.

Hermetic: the account registry, the sticky-default state file and every
provider's "is it configured" hook are patched, so nothing here reads the real
`~/.config/mokuro-bridge`, the keychain, or the network.
"""
from __future__ import annotations

import json

import pytest

from mokuro_bridge import accounts, providers


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway registry + state file with controllable provider status."""
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(
        providers, "_UPLOAD_METHOD_STATE_FILE", tmp_path / "upload_method_default.json"
    )
    configured = {"mega": set(), "drive": set(), "onedrive": set()}

    def make(provider, source):
        def check(name: str = accounts.DEFAULT_NAME) -> bool:
            return name in configured[provider]

        def creds(name: str = accounts.DEFAULT_NAME):
            return source if name in configured[provider] else None

        return check, creds

    mega_check, mega_creds = make("mega", "keychain")
    drive_check, drive_creds = make("drive", "oauth")
    one_check, _ = make("onedrive", "token")
    monkeypatch.setattr(providers, "_mega_configured", mega_check)
    monkeypatch.setattr(providers, "_drive_configured", drive_check)
    monkeypatch.setattr(providers, "_onedrive_configured", one_check)
    monkeypatch.setattr(providers, "_mega_creds_source", mega_creds)
    monkeypatch.setattr(providers, "_drive_creds_source", drive_creds)
    monkeypatch.delenv("MOKURO_BRIDGE_UPLOAD_DEFAULT", raising=False)
    return configured


# ── resolution ────────────────────────────────────────────────────────────

def test_bare_and_default_ids_are_the_same_target(env):
    assert providers.resolve_upload_method("mega") == "mega"
    assert providers.resolve_upload_method("mega:default") == "mega"
    assert providers.resolve_upload_method(" MEGA ") == "mega"
    assert providers.resolve_upload_method("local") == "local"

def test_named_accounts_resolve_once_configured(env):
    accounts.save_instance("mega", "work", extra={"email": "w@example.com"})
    env["mega"].add("work")
    assert providers.resolve_upload_method("mega:work") == "mega:work"

def test_unknown_account_error_lists_available_targets(env):
    with pytest.raises(ValueError) as exc:
        providers.resolve_upload_method("mega:nope")
    message = str(exc.value)
    assert "mega:nope" in message and "mega" in message and "local" in message

def test_legacy_booleans_still_map_to_mega_and_local(env):
    assert providers.resolve_upload_method("true") == "mega"
    assert providers.resolve_upload_method("off") == "local"
    # …and an empty request means "whatever the default is".
    assert providers.resolve_upload_method("") == providers._default_upload_method()
    assert providers.resolve_upload_method(None) == providers._default_upload_method()

# ── registry listing ──────────────────────────────────────────────────────

def test_every_account_is_listed_with_its_root(env):
    accounts.save_instance("mega", "work", root="/Root/work", extra={"email": "w@example.com"})
    accounts.save_instance("drive", "main", root="mokuro-main")
    env["mega"].add("default")
    env["mega"].add("work")
    env["drive"].add("main")

    methods = providers._build_upload_methods()

    assert list(methods) == ["local", "mega", "mega:work", "drive", "drive:main", "onedrive"]
    default_mega = methods["mega"]
    assert default_mega.name == "MEGA (megatools)"  # unchanged for existing clients
    assert default_mega.extra["library_root"] == accounts.default_root("mega")
    assert default_mega.extra["account"] == "default"

    work = methods["mega:work"]
    assert work.name == "MEGA (megatools) — work"
    assert work.extra["library_root"] == "/Root/work"
    assert work.extra["email"] == "w@example.com"
    assert work.configured is True

    drive_main = methods["drive:main"]
    assert drive_main.extra["root"] == "mokuro-main"
    assert methods["onedrive"].configured is False

def test_unconfigured_account_is_listed_but_not_ready(env):
    accounts.save_instance("mega", "work", extra={"email": "w@example.com"})
    methods = providers._build_upload_methods()
    assert methods["mega:work"].configured is False
    assert methods["mega:work"].extra["creds_source"] is None

def test_current_folder_is_per_account(env):
    accounts.save_instance("mega", "work", root="/Root/work")
    assert providers._method_current_folder("mega:work") == "/Root/work"
    assert providers._method_current_folder("mega") == accounts.default_root("mega")
    assert "My Drive" in providers._method_current_folder("drive")

# ── sticky default ────────────────────────────────────────────────────────

def test_remembered_target_persists_across_reads(env):
    accounts.save_instance("mega", "work", extra={"email": "w@example.com"})
    env["mega"].add("work")

    providers._remember_upload_method("mega:work")

    stored = json.loads(providers._UPLOAD_METHOD_STATE_FILE.read_text(encoding="utf-8"))
    assert stored == {"method": "mega:work"}
    assert providers._load_remembered_upload_method() == "mega:work"
    assert providers._default_upload_method() == "mega:work"
    assert providers._build_upload_methods()["mega:work"].default is True

def test_unconfigured_target_is_never_remembered(env):
    accounts.save_instance("mega", "work", extra={"email": "w@example.com"})
    # Not marked configured — remembering it would strand the default.
    providers._remember_upload_method("mega:work")
    assert not providers._UPLOAD_METHOD_STATE_FILE.exists()
    providers._remember_upload_method("mega:nope")
    assert not providers._UPLOAD_METHOD_STATE_FILE.exists()

def test_remembered_account_that_vanished_falls_back_to_env(env):
    providers._UPLOAD_METHOD_STATE_FILE.write_text(
        json.dumps({"method": "mega:work"}), encoding="utf-8"
    )
    assert providers._load_remembered_upload_method() is None
    assert providers._default_upload_method() == "local"

def test_env_seed_accepts_an_account_id(env, monkeypatch):
    accounts.save_instance("mega", "work", extra={"email": "w@example.com"})
    monkeypatch.setenv("MOKURO_BRIDGE_UPLOAD_DEFAULT", "mega:work")
    assert providers._default_upload_method() == "mega:work"
    monkeypatch.setenv("MOKURO_BRIDGE_UPLOAD_DEFAULT", "true")
    assert providers._default_upload_method() == "mega"
    monkeypatch.setenv("MOKURO_BRIDGE_UPLOAD_DEFAULT", "mega:ghost")
    assert providers._default_upload_method() == "local"

# ── dispatch guard ────────────────────────────────────────────────────────

def test_upload_file_rejects_an_unknown_target(env, tmp_path):
    payload = tmp_path / "x.cbz"
    payload.write_bytes(b"x")
    with pytest.raises(ValueError):
        providers.upload_file("mega:nope", payload, "/Root/x", None)
