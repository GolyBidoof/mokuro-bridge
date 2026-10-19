"""The OneDrive one-time Azure app ID: where it comes from and how it is stored.

The dashboard can set it without an environment variable or a restart, so the
resolution order (environment, then the file the page writes, then a project
default) is worth its own tests, as is the GUID check that catches the portal's
Object ID and Directory ID lookalikes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mokuro_bridge.providers import onedrive

REPO_ROOT = Path(__file__).resolve().parent.parent
GUID = "11111111-2222-3333-4444-555555555555"
OTHER_GUID = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """An empty app-ID file, no environment value and no shipped default."""
    monkeypatch.setattr(onedrive, "ONEDRIVE_APP_FILE", tmp_path / "onedrive_app.json")
    monkeypatch.setattr(onedrive, "ONEDRIVE_CLIENT_ID", "")
    monkeypatch.setattr(onedrive, "_ONEDRIVE_BUILTIN_CLIENT_ID", "")
    monkeypatch.delenv("ONEDRIVE_CLIENT_ID", raising=False)
    return tmp_path


def test_the_file_written_by_the_page_wins_over_a_shipped_default(app_env, monkeypatch):
    assert onedrive.client_id() == ""
    assert onedrive.client_id_source() is None

    onedrive.store_client_id(GUID)
    assert onedrive.client_id() == GUID
    assert onedrive.client_id_source() == "file"

    monkeypatch.setattr(onedrive, "_ONEDRIVE_BUILTIN_CLIENT_ID", OTHER_GUID)
    assert onedrive.client_id() == GUID, "a user's own app beats a project default"

    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", OTHER_GUID)
    assert onedrive.client_id() == OTHER_GUID
    assert onedrive.client_id_source() == "env"


def test_a_shipped_default_needs_no_setup_at_all(app_env, monkeypatch):
    monkeypatch.setattr(onedrive, "_ONEDRIVE_BUILTIN_CLIENT_ID", OTHER_GUID)
    assert onedrive.client_id() == OTHER_GUID
    assert onedrive.client_id_source() == "builtin"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-a-guid",
        "11111111222233334444555555555555",
        # The Object ID and Directory ID sit next to the right value in the
        # portal; one digit short must be caught rather than sent to Microsoft.
        "11111111-2222-3333-4444-55555555555",
        "tenant-name.onmicrosoft.com",
    ],
)
def test_values_that_are_not_application_client_ids_are_refused(value):
    with pytest.raises(ValueError, match="\\(client\\) ID"):
        onedrive.validate_client_id(value)


def test_whitespace_around_a_guid_is_tolerated():
    assert onedrive.validate_client_id(f"  {GUID}\n") == GUID


def test_the_stored_file_is_private_json(app_env):
    onedrive.store_client_id(GUID)
    path = app_env / "onedrive_app.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"client_id": GUID}
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_known_microsoft_errors_become_the_step_they_need():
    consent = onedrive.explain_aadsts("AADSTS65001: The user has not consented")
    assert "Files.ReadWrite" in consent and "AADSTS65001" in consent

    wrong_app = onedrive.explain_aadsts("AADSTS700016: Application not found in the directory")
    assert "personal Microsoft accounts" in wrong_app
    assert "AADSTS700016" in wrong_app

    no_tenant = onedrive.explain_aadsts(
        "could not start the flow: AADSTS50059: No tenant-identifying information"
    )
    assert "Application (client) ID" in no_tenant
    assert "AADSTS50059" in no_tenant

    assert onedrive.explain_aadsts("some other error") == ""


def test_the_page_collects_the_app_id_with_the_azure_steps():
    page = (REPO_ROOT / "mokuro_bridge" / "web" / "dashboard.html").read_text(encoding="utf-8")
    for marker in (
        'id="add-onedrive-client-id"',
        "Where do I get this?",
        "Application (client) ID",
        "Allow public client flows",
        "Files.ReadWrite",
        "portal.azure.com",
    ):
        assert marker in page, f"the OneDrive form lost {marker!r}"
