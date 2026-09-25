"""Regression tests for the macOS keychain MEGA credential lookup.

They never touch the real keychain: `security` is stubbed with a fake that
models **two** mega.nz items (a stale address first, the current one second).
That is the state that made every MEGA upload fail with
`API call 'us' failed: Server returned error ENOENT`, because
`find-internet-password` without `-a` keeps returning the older item.
"""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from mokuro_bridge import creds

STALE = ("amelachs0+bridge@gmail.com", "old-pw")
CURRENT = ("amelachs0@gmail.com", "new-pw")
ITEMS = [STALE, CURRENT]


class FakeSecurity:
    """Minimal stand-in for the `security` CLI over a list of items."""

    def __init__(self, items=None, delete_rc: int = 0):
        self.items = list(ITEMS if items is None else items)
        self.delete_rc = delete_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        assert cmd[0] == "security", cmd
        verb = cmd[1]
        account = cmd[cmd.index("-a") + 1] if "-a" in cmd else None

        if verb == "find-internet-password":
            match = next(
                (it for it in self.items if account is None or it[0] == account), None
            )
            if match is None:
                return subprocess.CompletedProcess(cmd, 44, "", "not found")
            if "-w" in cmd:
                return subprocess.CompletedProcess(cmd, 0, match[1] + "\n", "")
            return subprocess.CompletedProcess(
                cmd,
                0,
                f'    "acct"<blob>="{match[0]}"\n    "srvr"<blob>="mega.nz"\n',
                "",
            )

        if verb == "add-internet-password":
            assert "-w" in cmd and "-U" in cmd, cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        if verb == "delete-internet-password":
            if self.delete_rc == 0:
                self.items = [it for it in self.items if it[0] != account]
            return subprocess.CompletedProcess(cmd, self.delete_rc, "", "")

        raise AssertionError(f"unexpected security verb: {verb}")


@pytest.fixture
def fake_security(monkeypatch):
    def install(items=None, delete_rc: int = 0) -> FakeSecurity:
        fake = FakeSecurity(items, delete_rc)
        monkeypatch.setattr(
            creds,
            "subprocess",
            types.SimpleNamespace(
                run=fake,
                TimeoutExpired=subprocess.TimeoutExpired,
                CompletedProcess=subprocess.CompletedProcess,
            ),
        )
        return fake

    return install


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS keychain lookup"
)


def test_email_and_password_come_from_the_same_item(fake_security, monkeypatch):
    """Without MEGA_EMAIL, the first item wins — but only for both halves.

    Previously the email came from one `security` call and the password from
    another, so duplicates could be mixed into a combination that never
    authenticates.
    """
    monkeypatch.delenv("MEGA_EMAIL", raising=False)
    fake = fake_security()

    email, password = creds._keychain_mega_creds()

    assert (email, password) == STALE
    password_calls = [c for c in fake.calls if c[1] == "find-internet-password" and "-w" in c]
    assert password_calls == [
        ["security", "find-internet-password", "-s", "mega.nz", "-r", "htps", "-w", "-a", STALE[0]]
    ]


def test_mega_email_selects_that_account(fake_security, monkeypatch):
    """MEGA_EMAIL disambiguates when a stale item is still present."""
    monkeypatch.setenv("MEGA_EMAIL", CURRENT[0])
    fake_security()

    assert creds._keychain_mega_creds() == CURRENT


def test_store_drops_the_accounts_the_caller_marks_stale(fake_security):
    """A renamed/replaced account can be cleaned up — when the caller says so."""
    fake = fake_security()

    creds._store_mega_creds_keychain(CURRENT[0], CURRENT[1], stale_accounts=[STALE[0]])

    assert [it[0] for it in fake.items] == [CURRENT[0]]
    assert [
        "security", "delete-internet-password", "-s", "mega.nz", "-r", "htps", "-a", STALE[0]
    ] in fake.calls


def test_store_never_deletes_a_sibling_account(fake_security):
    """Adding a second account must not remove the first one's item.

    This is the multi-account contract: the wizard passes an explicit list of
    orphaned accounts, and an empty list (the default) deletes nothing.
    """
    fake = fake_security()

    creds._store_mega_creds_keychain(CURRENT[0], CURRENT[1])

    assert [it[0] for it in fake.items] == [STALE[0], CURRENT[0]]
    assert not [c for c in fake.calls if c[1] == "delete-internet-password"]


def test_store_warns_when_a_stale_item_survives(fake_security, capsys):
    """A failed cleanup is reported instead of silently leaving the trap."""
    fake = fake_security(delete_rc=1)

    creds._store_mega_creds_keychain(CURRENT[0], CURRENT[1], stale_accounts=[STALE[0]])

    assert [it[0] for it in fake.items] == [STALE[0], CURRENT[0]]
    err = capsys.readouterr().err
    assert STALE[0] in err and "Keychain Access" in err
