"""The named-account registry: id parsing, storage and legacy defaults.

Everything runs against a temporary ACCOUNTS_DIR, so no test touches the real
`~/.config/mokuro-bridge` (or the developer's keychain).
"""
from __future__ import annotations

import json
import sys

import pytest

from mokuro_bridge import accounts


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway directory."""
    monkeypatch.setattr(accounts, "ACCOUNTS_DIR", tmp_path / "accounts")
    return tmp_path / "accounts"


def test_default_account_uses_the_bare_provider_id():
    assert accounts.method_id("mega") == "mega"
    assert accounts.method_id("mega", accounts.DEFAULT_NAME) == "mega"
    assert accounts.method_id("mega", "work") == "mega:work"

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mega", ("mega", "default")),
        ("MEGA", ("mega", "default")),
        (" mega:work ", ("mega", "work")),
        ("mega:default", ("mega", "default")),  # redundant but normalised
        ("drive:main", ("drive", "main")),
        ("onedrive:uni-2", ("onedrive", "uni-2")),
        ("MEGA:Work", ("mega", "work")),  # case is normalised, not rejected
    ],
)
def test_parse_method_id(raw, expected):
    assert accounts.parse_method_id(raw) == expected

@pytest.mark.parametrize(
    "raw",
    ["local", "dropbox", "mega:", "mega:has space", "mega:_lead", "mega:a" * 20, ""],
)
def test_parse_method_id_rejects_junk(raw):
    with pytest.raises(ValueError):
        accounts.parse_method_id(raw)

def test_local_is_never_an_account():
    with pytest.raises(ValueError, match="local"):
        accounts.parse_method_id("local")

def test_every_provider_has_an_implicit_default(registry):
    listed = accounts.list_instances()
    assert [i.id for i in listed] == ["mega", "drive", "onedrive"]
    assert all(not i.tracked for i in listed)
    # Root falls back to the provider's configured default.
    assert accounts.load_instance("mega").root_path == accounts.default_root("mega")

def test_named_accounts_are_absent_until_saved(registry):
    assert accounts.load_instance("mega", "work") is None
    with pytest.raises(ValueError):
        # An unknown name is not addressable either.
        accounts.save_instance("dropbox", "work")

def test_save_and_list_roundtrip(registry):
    saved = accounts.save_instance(
        "mega", "work", label="Work account", root="/Root/work", extra={"email": "w@example.com"}
    )
    assert saved.id == "mega-work"
    assert saved.email == "w@example.com"
    assert saved.root_path == "/Root/work"
    assert saved.display_name == "MEGA — Work account"
    assert saved.tracked

    assert [i.id for i in accounts.list_instances()] == ["mega", "mega-work", "drive", "onedrive"]

    # Re-saving only the email keeps the label and root (None means "leave it").
    again = accounts.save_instance("mega", "work", extra={"email": "new@example.com"})
    assert again.email == "new@example.com"
    assert again.label == "Work account"
    assert again.root == "/Root/work"

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no POSIX mode bits: st_mode reports 0o666 for any writable "
           "file and chmod only toggles read-only. The boundary there is the "
           "user profile ACL, which already confines the file.",
)
def test_metadata_file_is_private(registry):
    accounts.save_instance("drive", "main")
    path = accounts.meta_path("drive", "main")
    assert path.is_file()
    assert oct(path.stat().st_mode & 0o777) == "0o600"

def test_saved_account_survives_a_fresh_registry_read(registry):
    accounts.save_instance("onedrive", "uni", root="Manga")
    # A new process would see the same thing; emulate by re-reading.
    assert accounts.load_instance("onedrive", "uni").root_path == "Manga"
    assert accounts.instances_for("onedrive")[1].id == "onedrive-uni"

def test_delete_instance_removes_metadata_and_its_own_secrets(registry):
    accounts.save_instance("mega", "work", extra={"email": "w@example.com"})
    accounts.save_instance("mega", "other", extra={"email": "o@example.com"})
    secret = accounts.secret_path("mega", "work", "credentials.env")
    secret.write_text("MEGA_EMAIL=w@example.com\nMEGA_PASSWORD=x\n", encoding="utf-8")
    sibling = accounts.secret_path("mega", "other", "credentials.env")
    sibling.write_text("MEGA_EMAIL=o@example.com\nMEGA_PASSWORD=x\n", encoding="utf-8")

    removed = accounts.delete_instance("mega", "work")

    assert accounts.load_instance("mega", "work") is None
    assert not secret.exists()
    assert sibling.is_file(), "another account's secret must be untouched"
    assert {str(p) for p in removed} == {str(accounts.meta_path("mega", "work")), str(secret)}

def test_legacy_secret_path_only_for_the_default_account(registry):
    assert accounts.legacy_secret_path("mega", "default") is not None
    assert accounts.legacy_secret_path("drive", "default") is not None
    assert accounts.legacy_secret_path("onedrive", "default") is not None
    assert accounts.legacy_secret_path("mega", "work") is None

def test_sibling_emails_protects_shared_credentials(registry):
    """Removing one account must not orphan another's OS-store entry."""
    accounts.save_instance("mega", "default", extra={"email": "shared@example.com"})
    accounts.save_instance("mega", "work", extra={"email": "shared@example.com"})
    accounts.save_instance("mega", "other", extra={"email": "other@example.com"})

    assert accounts.sibling_emails("mega", "work") == {"shared@example.com", "other@example.com"}
    # Removing `work` leaves the shared email in use by `default`.
    accounts.delete_instance("mega", "work")
    assert "shared@example.com" in accounts.sibling_emails("mega", "other")
    # …and the same question asked about a different provider is empty.
    assert accounts.sibling_emails("drive", "work") == set()

def test_non_account_json_files_are_ignored(registry):
    accounts.save_instance("drive", "main")
    # A named secret file shares the *.__*.json shape.
    accounts.secret_path("drive", "main", "creds.json").write_text("{}", encoding="utf-8")
    assert [i.id for i in accounts.instances_for("drive")] == ["drive", "drive-main"]

def test_next_instance_name_offers_default_then_account_n(registry):
    assert accounts.next_instance_name("mega") == "default"
    accounts.save_instance("mega", "default", extra={"email": "a@example.com"})
    assert accounts.next_instance_name("mega") == "account2"
    accounts.save_instance("mega", "account2", extra={"email": "b@example.com"})
    assert accounts.next_instance_name("mega") == "account3"

def test_ask_account_name_uses_the_flag_and_never_blocks(registry, monkeypatch):
    assert accounts.ask_account_name("Work", "mega") == "work"
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(EOFError))
    assert accounts.ask_account_name(None, "mega") == "default"


# --- aliases: the user-facing account id -----------------------------------

def test_legacy_accounts_derive_an_alias_without_rewriting_metadata(registry):
    accounts.save_instance("drive", "main")
    instance = accounts.load_instance("drive", "main")
    assert instance.id == "drive-main"
    assert instance.storage_id == "drive:main"
    # The file on disk was written by the old code path: no alias key yet.
    raw = json.loads(accounts.meta_path("drive", "main").read_text(encoding="utf-8"))
    assert raw.get("alias", "") == ""
    # A later unrelated save must not freeze the derived alias into the file.
    accounts.save_instance("drive", "main", root="elsewhere")
    raw = json.loads(accounts.meta_path("drive", "main").read_text(encoding="utf-8"))
    assert raw.get("alias", "") == ""

def test_parse_accepts_alias_alias_provider_and_legacy_spelling(registry):
    accounts.save_instance("mega", "work", alias="mega-beta")
    assert accounts.parse_method_id("mega-beta") == ("mega", "work")
    assert accounts.parse_method_id("mega:work") == ("mega", "work")
    assert accounts.parse_method_id("mega:default") == ("mega", "default")
    assert accounts.parse_method_id("MEGA-BETA") == ("mega", "work")
    assert accounts.parse_method_id("mega") == ("mega", "default")

def test_unknown_target_error_lists_known_aliases(registry):
    accounts.save_instance("mega", "work", alias="mega-beta")
    with pytest.raises(ValueError) as info:
        accounts.parse_method_id("mega-gamma")
    assert "mega-beta" in str(info.value)

def test_new_accounts_persist_an_explicit_alias(registry):
    saved = accounts.save_instance("drive", "drive-next", alias="drive-next")
    assert saved.id == "drive-next"
    raw = json.loads(accounts.meta_path("drive", "drive-next").read_text(encoding="utf-8"))
    assert raw["alias"] == "drive-next"
    assert accounts.find_alias("drive-next").name == "drive-next"

@pytest.mark.parametrize(
    "alias",
    ["", "Mega Beta", "mega_beta", "-mega", "a" * 33, "local", "drive"],
)
def test_invalid_aliases_are_rejected(registry, alias):
    with pytest.raises(ValueError):
        accounts.validate_alias("mega", alias)
    with pytest.raises(ValueError):
        accounts.save_instance("mega", "x", alias=alias)

def test_alias_collisions_are_a_distinct_error(registry):
    accounts.save_instance("mega", "mega-1", alias="mega-1")
    with pytest.raises(accounts.AliasCollision):
        accounts.save_instance("mega", "other", alias="mega-1")
    with pytest.raises(accounts.AliasCollision):
        accounts.validate_alias("drive", "mega-1")
    # The account being edited does not collide with itself.
    accounts.save_instance("mega", "mega-1", alias="mega-1", label="same alias")

def test_first_account_claiming_the_bare_provider_id_must_be_the_default(registry):
    accounts.save_instance("mega", "mega-1", alias="mega-1")
    # A named account may not take the bare provider alias...
    with pytest.raises(ValueError):
        accounts.save_instance("mega", "mega-1", alias="mega")
    # ...but the provider's first (storage "default") account may.
    accounts.save_instance("mega", accounts.DEFAULT_NAME, alias="mega")
    assert accounts.load_instance("mega").id == "mega"
    assert accounts.alias_to_name("mega", "mega") == accounts.DEFAULT_NAME
    assert accounts.alias_to_name("mega", "mega-2") == "mega-2"

def test_alias_suggestions_start_at_the_provider_then_count_up(registry):
    assert accounts.suggest_alias("mega") == "mega"  # only the implicit default exists
    accounts.save_instance("mega", accounts.DEFAULT_NAME, alias="mega")
    assert accounts.suggest_alias("mega") == "mega-2"
    accounts.save_instance("mega", "mega-2", alias="mega-2")
    assert accounts.suggest_alias("mega") == "mega-3"
    # A taken "mega-4" is skipped, not proposed.
    accounts.save_instance("mega", "mega-4", alias="mega-4")
    assert accounts.suggest_alias("mega") == "mega-3"
    assert accounts.suggest_alias("drive") == "drive"

def test_renaming_an_alias_keeps_the_storage_name_and_legacy_id(registry):
    accounts.save_instance("mega", "work", label="Work")
    assert accounts.load_instance("mega", "work").id == "mega-work"
    renamed = accounts.save_instance("mega", "work", alias="mega-beta")
    assert renamed.id == "mega-beta"
    assert renamed.storage_id == "mega:work"
    assert renamed.label == "Work"
    # Both the new alias and the historical id still address the account.
    assert accounts.parse_method_id("mega-beta") == ("mega", "work")
    assert accounts.parse_method_id("mega:work") == ("mega", "work")
    # The freed alias can be claimed by another account.
    accounts.save_instance("mega", "other", alias="mega-work")
    assert accounts.find_alias("mega-work").name == "other"

def test_renaming_the_first_account_keeps_the_bare_provider_id_working(registry):
    accounts.save_instance("mega", accounts.DEFAULT_NAME, alias="mega")
    accounts.save_instance("mega", accounts.DEFAULT_NAME, alias="mega-personal")
    assert accounts.load_instance("mega").id == "mega-personal"
    assert accounts.parse_method_id("mega") == ("mega", "default")
    assert accounts.parse_method_id("mega-personal") == ("mega", "default")

def test_hand_edited_derived_alias_collisions_are_deduped(registry):
    accounts.save_instance("mega", "work_2")
    accounts.save_instance("mega", "work-2")
    ids = [i.id for i in accounts.instances_for("mega")]
    assert len(ids) == len(set(ids)), ids
    assert "mega-work-2" in ids
    assert all(accounts.find_alias(alias) is not None for alias in ids)

def test_alias_for_storage_keeps_renames_and_avoids_collisions(registry):
    accounts.save_instance("mega", "work", alias="mega-beta")
    assert accounts.alias_for_storage("mega", "work") == "mega-beta"
    accounts.save_instance("mega", "solo")
    assert accounts.alias_for_storage("mega", "solo") == "mega-solo"
    # Two hand-edited names that derive the same alias: file order picks one to
    # freeze, the other keeps a read-derived id (deduped by list_instances).
    accounts.save_instance("mega", "work_2")
    accounts.save_instance("mega", "work-2")
    assert accounts.alias_for_storage("mega", "work_2") is None
    assert accounts.alias_for_storage("mega", "work-2") == "mega-work-2"
