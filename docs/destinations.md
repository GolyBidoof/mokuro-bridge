# Destinations

Where a finished volume goes, how accounts are named, where their credentials live, and which target is the default.

## The four targets

The bridge ships four destinations: a local folder, MEGA, Google Drive and OneDrive. Upload providers are optional installs, and `local` needs nothing beyond the base install.

| Target | Provider id | Requires | Remote path |
| --- | --- | --- | --- |
| Local folder | `local` | nothing | `<output>/<series>/` |
| MEGA | `mega` | the `megatools` binary plus credentials | `<root>/<series>/` |
| Google Drive | `drive` | `google-api-python-client` and an OAuth or service-account credential | `<root>/<series>/` |
| OneDrive | `onedrive` | `msal` plus an Azure app client ID | `<root>/<series>/` |

WebDAV is not a target. A WebDAV provider existed briefly in the v0.3.0 era and was removed before v0.4.0, so no tagged release supports it. Any documentation elsewhere that lists WebDAV as a current destination of this service is out of date.

Each destination receives the same three files under a per-series folder: `<volume>.webp` (cover), `<volume>.cbz` (pages) and `<volume>.mokuro` (OCR text and blocks). The series name is derived from the volume title by dropping edition and imprint tags, so `サンプル作品（２）【電子限定特典付き】` and `サンプル作品 1巻` share one series folder.

The remote collision policy is chosen per finalize with `overwrite`: `fail` (the default) errors on an existing destination file, `skip` treats an existing file as success, and `overwrite` deletes and re-uploads.

## How accounts are named

Every provider can hold more than one account. An account is addressed by a *method id*:

| Method id | Account |
| --- | --- |
| `local` | The local output folder (the only non-account target) |
| `mega` | The default MEGA account |
| `mega:work` | A second MEGA account named `work` |
| `drive:personal` | A Google Drive account named `personal` |
| `onedrive:uni` | A OneDrive account named `uni` |

A bare provider id always means that provider's default account, so existing single-account configurations keep working. The redundant `<provider>:default` is normalised to the bare id. Account names are 1 to 32 characters from `a-z`, `0-9`, `_` and `-`, starting with a letter or a digit, and are lowercased. `local` has no accounts.

Legacy booleans are still accepted where a method id is expected: `true`, `yes`, `on` and `1` mean `mega`; `false`, `no`, `off` and `0` mean `local`. New clients should send an id.

## Managing accounts

The CLI is the only place accounts are created, listed or removed:

```bash
mokuro-bridge --setup-upload mega                              # default MEGA account
mokuro-bridge --setup-upload mega --name work --root /Root/work --label Work
mokuro-bridge --list-uploads
mokuro-bridge --remove-upload mega:work
```

`--setup-upload` accepts `mega`, `drive`, `onedrive`, or `<provider>:<name>`. Running it again with a new `--name` adds a second account. `--root` sets the account's remote root, and `--label` sets the human label shown in `--list-uploads` and `/health`. `--setup-mega` remains accepted as a hidden compatibility flag but is no longer advertised.

Each wizard offers to install its provider's requirements file when the SDK is missing, with one confirmation, and never installs without consent.

## Credential stores

Nothing secret is ever written to this repository.

**Account metadata.** One JSON file per account under `~/.config/mokuro-bridge/accounts/<provider>__<name>.json` (`MOKURO_BRIDGE_ACCOUNTS_DIR`). It holds only non-secret fields: display label, remote root, and for MEGA the account email. The default account deliberately keeps using the legacy single-account paths, so a single-account install needs no migration.

**MEGA.** For the default account, resolution order is `MEGA_EMAIL` and `MEGA_PASSWORD` in the environment, then `MEGA_CREDS_FILE` (`~/.config/mokuro-bridge/credentials.env`, mode 0600), then the OS credential store: macOS Keychain via the `security` binary, or any `keyring` backend (Windows Credential Manager, Linux Secret Service). For a named account the password is read from the OS store scoped to the email recorded in its account file, with a 0600 per-account `credentials.env` as the fallback. MEGA uploads also need the `megatools` binary; `/health` reports `megatools_installed`, `mega_configured` and `mega_creds_source` (`env`, `file` or `keychain`). The default remote root is `/Root/mokuro-reader` (`MEGA_LIBRARY_ROOT`).

**Google Drive.** OAuth2 desktop-app credentials in `~/.config/mokuro-bridge/drive_credentials.json` (mode 0600, `DRIVE_CREDS_FILE`), or the same path holding a service-account key, which is detected by its `"type": "service_account"` field. A named account keeps its own 0600 file as `~/.config/mokuro-bridge/accounts/drive__<name>.creds.json`. The wizard walks through creating a free Google Cloud OAuth client and pasting its client ID and secret, because a client may only run in the project that registered it; `DRIVE_CLIENT_ID` and `DRIVE_CLIENT_SECRET` (or `DRIVE_CLIENT_SECRET_FILE`) can preset that. The scope is `drive.file` and the library root is the folder `mokuro-reader` at My Drive root (`DRIVE_ROOT_NAME`). `/health` reports `root` and `creds_source` per account.

**OneDrive.** MS Graph device-code sign-in, with the token in `~/.config/mokuro-bridge/onedrive_token.json` (mode 0600, `ONEDRIVE_TOKEN_FILE`), or `~/.config/mokuro-bridge/accounts/onedrive__<name>.token.json` for a named account. The wizard asks for an Azure app client ID, or takes `ONEDRIVE_CLIENT_ID` from the environment. The scope is `Files.ReadWrite` (`offline_access` is added by msal itself and must not be listed) and the library root is the folder `mokuro-reader` (`ONEDRIVE_ROOT_NAME`).

**Local.** The base directory is `MOKURO_BRIDGE_OUTPUT_DIR`, or a checkout's `<repo>/output`, or `~/mokuro-bridge/output` for an installed wheel.

## The sticky default

The default target is deliberately sticky, and it is the one behaviour most likely to surprise a new client.

Once any client names a destination explicitly (an `upload_method` or the legacy `upload_to_mega`), that choice is remembered in the work directory and becomes the default for every later request, including requests from other clients and requests after a restart, until another explicit choice replaces it. It is stored as `{"method": "<id>"}` in `<work dir>/upload_method_default.json`, mode 0600, written atomically.

Details that matter when reasoning about it:

- Only a usable target is remembered: `local`, or an account that currently exists and is configured. An unconfigured provider is never made the default.
- The choice is committed only after a finalize has acquired its exclusive session lease, so a losing concurrent finalize cannot change the process-wide default.
- Before any explicit choice, the environment variable `MOKURO_BRIDGE_UPLOAD_DEFAULT` seeds the default. It accepts the legacy booleans (`true` means `mega`, `false` or empty means `local`) and an account id such as `mega:work`. An unknown or unconfigured value falls back.
- `GET /upload-methods` and `GET /health` both report `upload_method_default`, so a client can display the current default instead of assuming it.
- Finalize resolves the target before it starts: an explicit `upload_method` wins, then the legacy `upload_to_mega`, then the remembered default, then the environment seed.

A local output folder is sticky in the same way and independently: an explicit `local_dir` on a local finalize or a local cover upload is remembered in `<work dir>/local_dir_default.json` and used for later local writes, with `OUTPUT_DIR` as the fallback. A remembered path that has since become disallowed is dropped rather than raising an error.

The practical consequence: one client's one-off `--dest local` changes where the next client's volume lands. Clients that need to be sure should name the destination on every finalize, or read `upload_method_default` first.
