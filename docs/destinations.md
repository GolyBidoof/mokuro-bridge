# Destinations

Where a finished volume goes, how accounts are named, where their credentials live, which target is the default, and what a destination backend has to provide. This page combines the user-facing destination model with the provider contract, because the two describe the same thing from opposite sides.

## The six targets

The bridge ships six destinations: a local folder, MEGA, Google Drive, OneDrive, WebDAV and rclone. The local folder needs nothing beyond the base install. The native cloud providers need their own optional Python library or binary, and rclone needs only the `rclone` binary with a configured remote.

| Target | Provider id | Requires | Remote path |
| --- | --- | --- | --- |
| Local folder | `local` | nothing | `<output>/<series>/` |
| MEGA | `mega` | the `megatools` binary plus credentials | `<root>/<series>/` |
| Google Drive | `drive` | `google-api-python-client` and an OAuth credential | `<root>/<series>/` |
| OneDrive | `onedrive` | `msal` plus a free Azure app ID, entered once in the page | `<root>/<series>/` |
| WebDAV | `webdav` | `requests` plus a server URL and Basic-auth credentials | `<base>/<root>/<series>/` |
| rclone | `rclone` | the `rclone` binary plus a remote from `rclone config` | `<remote>:<root>/<series>/` |

A provider id is also the alias of that provider's first account. The local folder is a destination but not an account, and its id is always `local`.

Each destination receives the same files under a per-series folder: `<volume>.webp` (cover), `<volume>.cbz` (pages), `<volume>.mokuro` (OCR text and blocks) and one shared `series.json` (the reader's per-series index). The series name is derived from the volume title by dropping edition and imprint tags, so `サンプル作品（２）【電子限定特典付き】` and `サンプル作品 1巻` share one series folder.

The remote collision policy is chosen per finalize with `overwrite`: `fail` (the default) reports an existing destination file as an error, `skip` treats an existing file as success, and `overwrite` deletes and re-uploads. `series.json` is not governed by that flag: the bridge updates it in place, because a manifest under a new name is a manifest no reader reads. When the destination already holds a correct `series.json` the file is left byte-identical.

The remote root of each account is editable from the configuration dashboard, which browses the provider's own folder tree and can set any folder as that account's default upload directory. See [dashboard.md](dashboard.md).

### rclone, and the loopback gateway

rclone is the way to reach OneDrive without an Azure account. Creating an Azure app registration needs an Entra tenant that a personal Microsoft account cannot make, while rclone already has its own Microsoft app registration; `rclone config` signs in with a personal account and keeps the token in rclone's own configuration. An rclone account is one remote (`onedrive:`, `gdrive:`, an S3 remote) plus a folder inside it, and the same provider reaches everything else rclone supports without another dependency in the bridge.

rclone is also the transport mokuro-bunko shares. The bridge runs `rclone serve webdav` on loopback for the account and sends its own transfers through it, so one authenticated rclone session serves both processes, and Bunko gets that URL instead of a second cloud sign-in. The gateway's records (`gateways.json`, `rclone.conf`, one log per account) live under `~/Library/Application Support/mokuro-bridge/rclone` on macOS, or `~/.local/share/mokuro-bridge/rclone` elsewhere (`MOKURO_BRIDGE_RCLONE_DIR`). The 0600 state file holds only loopback Basic-auth values, never a cloud token, and the recorded port survives a restart so Bunko's saved URL keeps working. `MOKURO_BRIDGE_RCLONE_GATEWAY=0` turns the gateway off and every transfer then talks to the cloud remote directly, which is the fallback when `rclone serve` cannot start. See [bunko.md](bunko.md).

### The series index

`series.json` is what makes a library delivered by a script, rather than by the reader, visible in reader.mokuro.app: the reader only builds that index itself for series it already knows locally, so the uploader has to publish it. The bridge creates it for the first volume in a series and merges each later volume into it, reading the destination's existing copy first and never overwriting one it could not read. See [series-json.md](series-json.md) for the format and for the pin on upstream.

The exception is a destination mokuro-bunko manages. Bunko compiles `series.json` and `catalog.json` itself, so for that account the bridge uploads the three volume files only, reports the index step as a `series_json` frame with `external: true`, and never reads, merges or uploads the manifest. Ownership is recognised from the destination's `.mokuro-bunko.json` marker, or declared explicitly with `series_owner` in the account metadata; either way the fallback is always to write the file. See [bunko.md](bunko.md).

## Accounts and aliases

Every provider can hold any number of accounts. An account is addressed by an alias, which is the user-facing method id:

| Alias | Account |
| --- | --- |
| `local` | The local output folder (not an account) |
| `mega` | The first MEGA account |
| `mega-2`, `mega-beta` | A second MEGA account, suggested as `mega-2` and renameable to any free alias |
| `drive` | The first Google Drive account |
| `drive-next` | A second Google Drive account |
| `onedrive-uni` | A OneDrive account named by the user |
| `webdav` | The first WebDAV account |
| `rclone` | The first rclone account |

Aliases are 1 to 32 characters from lowercase `a-z`, digits `0-9` and `-`, starting with a letter or digit. They are globally unique across providers, `local` is reserved, and so is another provider's bare name (`mega`, `drive`, `onedrive`, `webdav`, `rclone`). The first account of a provider is suggested as the bare provider name; later accounts are suggested as `<provider>-2`, `<provider>-3`, and so on, skipping anything taken. Any free alias can be typed instead.

The alias is the id reported by `GET /health`, `GET /upload-methods` and `GET /config`, and the value a client passes as `upload_method`. To select an account from a script, read the ids from `/upload-methods` and send one on the finalize:

```bash
curl -X POST "http://127.0.0.1:62642/session/<id>/finalize" \
  -d "upload_method=mega-work" -d "delete_after_upload=true"
```

Historical spellings keep working as input forever: a bare provider (`mega`), `mega:work`, `drive:main` and `provider:default` all resolve to the same account, so an old script or a stored default needs no migration. Displays show the alias, so an old `mega:work` appears as `mega-work`; the account's files and legacy credential paths are not rewritten for that. The redundant `<provider>:default` is normalised to the bare id.

Legacy booleans are still accepted where a method id is expected: `true`, `yes`, `on` and `1` mean `mega`; `false`, `no`, `off` and `0` mean `local`. New clients should send an alias.

## Adding, renaming and removing accounts

The configuration dashboard is the main path, and it covers every provider. Add destination opens a provider picker:

- MEGA asks for the account email and password and verifies them with `megatools` before storing anything.
- Google Drive opens Google's sign-in in a new tab and returns to the bridge's OAuth callback.
- OneDrive runs Microsoft's device-code flow.
- WebDAV asks for the server URL, username and password and contacts the server once before saving, so a typo is an error instead of an account that fails every later upload.
- rclone lists the remotes `rclone config` already has and walks through creating one, with the browser sign-in for the services that need it.

Reconnect appears on an account whose saved sign-in has expired and runs the same flow again under the same alias, keeping the folder, note and default. Rename changes the alias and the note while preserving the folder, credentials and the default flag; an alias already used by another account is refused. Remove deletes the account metadata and its saved credentials, leaves uploaded files alone, and clears the sticky default if it pointed there. See [dashboard.md](dashboard.md) for the page itself.

The terminal path still works for every provider, including WebDAV and rclone:

```bash
mokuro-bridge --setup-upload mega
mokuro-bridge --setup-upload drive --name next --label Next
mokuro-bridge --setup-upload webdav
mokuro-bridge --list-uploads
mokuro-bridge --remove-upload drive-next
```

`--setup-upload` takes a provider and optionally `--name`, `--root` and `--label`. A `--name` becomes the storage name and the derived alias (`--name next` becomes `drive-next`; underscores become hyphens). Running the wizard again for an existing account re-authenticates it, which is the terminal equivalent of Reconnect. `--setup-mega` remains accepted as a hidden compatibility flag but is no longer advertised. Renaming from the terminal is not offered; use the dashboard.

Each wizard checks for its provider's SDK and, when it is missing, prints the `pip install` command for it and offers to run it, with one confirmation; it never installs without consent.

## Credential stores

Nothing secret is ever written to this repository.

**Account metadata.** One JSON file per account under `~/.config/mokuro-bridge/accounts/<provider>__<storage>.json` (`MOKURO_BRIDGE_ACCOUNTS_DIR`). It holds only non-secret fields: the alias, the display label, the remote root, and for MEGA the account email, or for WebDAV the server URL and username. The first account of a provider keeps the historical `default` storage name and its legacy single-account credential paths, so a single-account install needs no migration; later accounts use a file named after their alias (or, for accounts created before aliases, the old `name`).

**MEGA.** For the first account, resolution order is `MEGA_EMAIL` and `MEGA_PASSWORD` in the environment, then `MEGA_CREDS_FILE` (`~/.config/mokuro-bridge/credentials.env`, mode 0600), then the OS credential store: macOS Keychain via the `security` binary, or any `keyring` backend (Windows Credential Manager, Linux Secret Service). For any account, the password is also read from the OS store scoped to the email recorded in its account file, with a 0600 per-account `credentials.env` as the fallback. MEGA uploads also need the `megatools` binary; `/health` reports `megatools_installed`, `mega_configured` and `mega_creds_source` (`env`, `file` or `keychain`). The default remote root is `/Root/mokuro-reader` (`MEGA_LIBRARY_ROOT`).

**Google Drive.** OAuth2 desktop-app credentials in `~/.config/mokuro-bridge/drive_credentials.json` (mode 0600, `DRIVE_CREDS_FILE`), or the same path holding a service-account key, which is detected by its `"type": "service_account"` field. A second account keeps its own 0600 file, `~/.config/mokuro-bridge/accounts/drive__<storage>.creds.json`. The dashboard's Add destination walks through Google's sign-in; a free Google Cloud OAuth client is needed once, and `DRIVE_CLIENT_ID` and `DRIVE_CLIENT_SECRET` (or `DRIVE_CLIENT_SECRET_FILE`) can preset it. A client pasted into the page is used for that sign-in only and never persisted. The scope is `drive.file` and the library root is the folder `mokuro-reader` at My Drive root (`DRIVE_ROOT_NAME`). `/health` reports `root` and `creds_source` per account.

**OneDrive.** MS Graph device-code sign-in, with the token in `~/.config/mokuro-bridge/onedrive_token.json` (mode 0600, `ONEDRIVE_TOKEN_FILE`), or `~/.config/mokuro-bridge/accounts/onedrive__<storage>.token.json` for a later account. The sign-in needs one free Azure application (client) ID per installation, the same one-app-per-deployment model upstream mokuro-reader uses with `VITE_ONEDRIVE_CLIENT_ID`; the difference is where it lives. The dashboard's OneDrive form collects it, validates that it is an Application (client) ID and not one of the Object ID and Directory ID lookalikes, and writes it to `~/.config/mokuro-bridge/onedrive_app.json` (`ONEDRIVE_APP_FILE`, mode 0600) without a restart. Resolution is `ONEDRIVE_CLIENT_ID` in the environment, then that file, then a project default a build may ship. No redirect URI and no client secret are involved: it is a public client using the device-code flow. The scope is `Files.ReadWrite` and the library root is the folder `mokuro-reader` (`ONEDRIVE_ROOT_NAME`). A known Microsoft error code is translated into the step it needs (`AADSTS65001` means the permission is missing, `AADSTS700016` means the app ID or its account types are wrong).

**WebDAV.** The server URL and username are non-secret and live in the account metadata. The password is written to that account's 0600 credentials file, `~/.config/mokuro-bridge/webdav_credentials.json` (`WEBDAV_CREDS_FILE`) for the first account and `~/.config/mokuro-bridge/accounts/webdav__<storage>.creds.json` for later ones, and the first account mirrors it into the OS keychain so a keychain-only setup written by an older provider keeps working. Resolution is `WEBDAV_PASSWORD` in the environment, then the file, then the keychain, so a password changed in the dashboard always wins over a stale keychain item. `WEBDAV_BASE_URL` and `WEBDAV_USERNAME` seed the first account for an existing setup, and the folder created under the base URL defaults to `mokuro-reader` (`WEBDAV_ROOT_NAME`). WebDAV needs only `requests`; there is no per-provider SDK. The dashboard's WebDAV form accepts the server's web address and discovers the usual WebDAV paths under it, so a Nextcloud or ownCloud user can paste the address they browse and let the bridge find `remote.php/dav/files/<user>/`. A NAS or home server with a self-signed certificate can be accepted per account from the form's advanced settings; that choice is non-secret metadata (`insecure`), only disables certificate verification for that account, and is shown in the account's Details dialog and in `/config`.

**rclone.** The bridge stores no credential at all: the token belongs to rclone's own configuration (`rclone config file` prints its path), and the account metadata records only the remote name and the folder. `RCLONE_ROOT_NAME` sets the default folder (`mokuro-reader`) and `RCLONE_BINARY` points at a non-default binary. `/health` reports the remote and `creds_source` as `rclone config`. The shared loopback gateway's own state (per-account port and Basic-auth pair) is separate: 0600 `gateways.json` under the rclone data directory, described above.

**Local.** The base directory is `MOKURO_BRIDGE_OUTPUT_DIR`, or a checkout's `<repo>/output`, or `~/mokuro-bridge/output` for an installed wheel.

## The sticky default

The default target is deliberately sticky, and it is the one behaviour most likely to surprise a new client.

Once any client names a destination explicitly (an `upload_method` or the legacy `upload_to_mega`), that choice is remembered in the work directory as the account's alias and becomes the default for every later request, including requests from other clients and requests after a restart, until another explicit choice replaces it. It is stored as `{"method": "<alias>"}` in `<work dir>/upload_method_default.json`, mode 0600, written atomically. A file written before aliases existed holds the old spelling; it resolves to the same account, follows a rename, and is deleted when that account is removed.

Details that matter when reasoning about it:

- Only a usable target is remembered: `local`, or an account that currently exists and is configured. An unconfigured provider is never made the default.
- The choice is committed only after a finalize has acquired its exclusive session lease, so a losing concurrent finalize cannot change the process-wide default.
- Before any explicit choice, the environment variable `MOKURO_BRIDGE_UPLOAD_DEFAULT` seeds the default. It accepts the legacy booleans (`true` means `mega`, `false` or empty means `local`), an alias such as `mega-work`, and the historical `mega:work` spelling. An unknown or unconfigured value falls back.
- `GET /upload-methods` and `GET /health` both report `upload_method_default` as an alias, so a client can display the current default instead of assuming it.
- Finalize resolves the target before it starts: an explicit `upload_method` wins, then the legacy `upload_to_mega`, then the remembered default, then the environment seed.

A local output folder is sticky in the same way and independently: an explicit `local_dir` on a local finalize or a local cover upload is remembered in `<work dir>/local_dir_default.json` and used for later local writes, with `OUTPUT_DIR` as the fallback. A remembered path that has since become disallowed is dropped rather than raising an error.

The practical consequence: one client's one-off `local` choice changes where the next client's volume lands. Clients that need to be sure should name the destination on every finalize, or read `upload_method_default` first.

## The provider contract

An upload provider is one destination backend. The existing five are copies of one shape rather than special cases in the dispatch code, and a new backend should be a copy of one module. The contract lives in [`mokuro_bridge/providers/base.py`](../mokuro_bridge/providers/base.py).

A provider module ends with a module-level `PROVIDER` value, an instance of the frozen `Provider` dataclass:

| Capability | Signature | What it must do |
| --- | --- | --- |
| `id` | `str` | The provider id that appears in a method id, such as `mega` or `drive`. It must match the id in `accounts.PROVIDERS`. |
| `label` | `str` | The base display name. The account view may refine it per account. |
| `configured` | `(name) -> bool` | Whether that one account is usable right now: credentials exist and any optional SDK or binary is importable or on `PATH`. |
| `creds_source` | `(name) -> str or None` | A short description of where the secret comes from, for `/health`, such as `oauth`, `keychain` or `token`. |
| `upload` | `(name, root, remote_dir, local_path, on_progress, overwrite) -> (ok, error, url)` | Upload one file and return the three tuple. The provider owns its account lock, the folders it creates and its own error policy. |
| `remote_exists` | `(name, remote_dir, filename) -> bool` | Whether a file is already there, without creating a folder or uploading a byte. |
| `download` | `(name, remote_dir, filename) -> bytes or None` | Read one small sidecar back: `None` when absent, a raise when the read fails, and a refusal above `DOWNLOAD_LIMIT_BYTES` (4 MiB). Used to merge into an existing `series.json`, and it must never create a folder. It is fail-closed on purpose: the caller's alternative to reading the file is to leave it alone. |
| `list_folders` | `(name, remote_dir) -> list[dict]` | One read-only directory listing, each entry `{"name", "path", "type", "size"}`, capped at `LIST_LIMIT_ENTRIES`. Used by the dashboard's folder picker, and it never creates a folder. |
| `run_setup` | `(name, root, label) -> None` | The interactive wizard that `--setup-upload` calls. |
| `account` | `(instance) -> ProviderAccount` | The presentation view described below. |
| `metadata_owner` | `(name) -> bool` or `None` | Optional. Whether another program, mokuro-bunko, compiles this destination's `series.json`. Every shipped provider leaves it `None` and relies on the registry's generic marker probe, which works through `download` and `root`; the hook exists for a backend that needs a signal the marker cannot carry. `series_owner` in the account metadata overrides either one. |

The `root(name)` method on `Provider` is not a field: it resolves the account through `accounts.py` and falls back to the provider default, so a provider never duplicates the root mapping. `upload` receives the account name rather than the instance, so a provider stays decoupled from the account registry.

The overwrite meaning is uniform across backends: `fail` reports an existing file as an error, `skip` treats it as success, `overwrite` replaces it, and `replace` replaces it under the same name. A volume artifact wants `overwrite`, because the versioned sibling MEGA produces preserves the prior valid copy. `replace` exists for a file the caller regenerates from local state, which today is only the series manifest: a manifest renamed to `series.json.replace-abc123` is a manifest no reader reads.

### The account view

`Provider.account(instance)` returns a `ProviderAccount`, a small frozen value with three fields:

- `label` is the upload-methods display name, usually `instance.display_name_for(label)`, which puts the account name after the provider name and keeps one provider note such as `(megatools)` attached to the provider.
- `extra` is the provider-specific part of the method row. The caller adds the generic `provider`, `account`, `creds_source` and `tracked` keys, so a provider adds only what is genuinely its own, such as `library_root` and `email` for MEGA or `root` for Drive and OneDrive.
- `current_folder` is the human-readable folder a client shows.

### One folder walk

`walk_folders` in `providers/base.py` is the single implementation of "follow a `/` separated remote path". It takes a `find(parent_id, segment)` callback and, optionally, a `make(parent_id, segment)` callback:

- With `make` absent the walk only reads and returns `None` at the first missing segment. This is the walk behind `remote_exists`, so checking a path never creates anything.
- With `make` present the walk creates a missing segment and continues. Drive and OneDrive use the creating form, while their read-only form passes `find` alone.

The callbacks let each provider keep its own request semantics. Drive resolves a folder with one `files.list` query and creates with one `files.create` call. OneDrive uses a read-only probe for the read walk and a separate probe plus a children POST for the creating walk, with the race handled inside the create callback. WebDAV does not use the walk: a remote path there is a URL suffix, so it creates each segment with `MKCOL` and probes it with a `Depth: 0` `PROPFIND`. The shared helper only sequences the segments, starting at `root`, and raises `ValueError` on an empty path when `require_nonempty` is set.

### The registry

`providers/__init__.py` holds `_PROVIDERS`, a mapping from provider id to `Provider`, built from the provider modules at import. `_method_configured`, `upload_file`, `remote_file_exists` and the current-folder lookup are lookups in that mapping, not chains of provider tests. `register_provider` adds or replaces one entry, and `setup_runner(provider_id)` returns the wizard that `cli.py` calls.

`accounts.py` stays the source of truth for which accounts exist, how a method id is spelled and the default root per provider. A provider never enumerates accounts and never parses a method id; it receives one account name and answers questions about it.

### Adding a provider

1. Copy `providers/mega.py` or `providers/drive.py` to `providers/<id>.py` and adapt it. Keep the module-level `PROVIDER` value at the end so it can reference the functions above it.
2. Add the id to `accounts.PROVIDERS`, to `accounts.default_root` and to `accounts._PROVIDER_LABELS`.
3. Add `<module>.PROVIDER` to the tuple that builds `_PROVIDERS` in `providers/__init__.py`.
4. Optionally name the provider's own optional dependency in its `_ensure_python_deps` call, so its wizard can install what is missing.

Nothing in `cli.py`, `api.py` or the lookup functions changes. `tests/test_provider_registry.py` builds a fake provider, registers it and asserts it is reachable from the upload-methods listing, `remote_file_exists`, `upload_file` and `setup_runner`, which is what keeps "add a provider" a one-module change plus registration rather than a claim.
