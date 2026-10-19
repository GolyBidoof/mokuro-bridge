# MEGA authentication audit

## Is browser email/password legitimate?

MEGA does not offer OAuth to third-party tools. Its API login is email plus password, optionally a TOTP code, with email or device confirmation on new logins. megatools, rclone and mokuro-bunko all authenticate the same way. A local email/password form is therefore the real mechanism, not a stand-in for a missing OAuth flow.

What would be unreliable and unsafe: scraping a browser mega.nz session or replaying an undocumented session token. Those expire, break on MEGA changes, and cannot be refreshed predictably.

## Current bridge path

1. The dashboard posts email and password to a loopback-only endpoint with an Origin check.
2. `mokuro_bridge/providers/mega.py::_mega_verify_creds` writes a temporary `.megarc` and runs `megatools ls /`.
3. On success the password goes to the OS keychain (fallback: a 0600 file) and is never rendered again.
4. Every later upload and listing goes through megatools with the same credentials.

## Failure modes found

- Two-factor accounts cannot connect. megatools has no TOTP argument and its config has no session field, so the non-interactive verification fails and no code is stored. This is the main gap.
- New-login email confirmation and login challenges surface as raw megatools stderr.
- Password changes and revoked sessions break the account until the user reconnects.
- megatools error strings are not stable across versions, so raw errors are poor UX.

## What changed now

- `_mega_login_error` maps 2FA, confirmation, blocked, rate-limit and wrong-password cases to actionable messages.
- The first connected remote destination is promoted to default, so "Filing to local" stops showing after connecting cloud storage.

## Options for real 2FA

| Option | 2FA | Persistent session | Maintained | Verdict |
| --- | --- | --- | --- | --- |
| megatools | no | n/a | yes | what ships today |
| mega.py (PyPI 1.0.8) | unclear | in-process only | no (2020, pins pathlib 1.0.1) | unusable on Python 3.13 |
| rclone 1.75 MEGA backend | yes, `--mega-2fa` | no, re-authenticates per invocation | yes | fine for setup checks, not for unattended uploads because a TOTP code is single use |
| MEGAcmd (official CLI) | yes | yes, persistent local session | yes | the only reliable unattended path |

## Recommendation

Install MEGAcmd and add it as an optional MEGA backend selected when an account uses 2FA, keeping megatools for non-2FA accounts. The bridge would call `mega-login` once with the code, store no password beyond setup, then use `mega-ls`, `mega-get`, `mega-put`, `mega-mkdir` and `mega-rm` against the persistent session. That is a provider rewrite and a new system dependency (`brew install megacmd`), so it needs an explicit decision.
