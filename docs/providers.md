# Upload providers

An upload provider is one remote destination backend. MEGA, Google Drive, OneDrive, WebDAV and rclone ship today, and a new backend should be a copy of one of their modules rather than an edit to the dispatch code in `providers/__init__.py`. The contract itself lives in [`mokuro_bridge/providers/base.py`](../mokuro_bridge/providers/base.py); this note is the map for the next provider.

## The contract

A provider module ends with a module level `PROVIDER` value, an instance of the frozen `Provider` dataclass from `providers/base.py`. The dataclass has these capabilities.

| Capability | Signature | What it must do |
| --- | --- | --- |
| `id` | `str` | The provider id that appears in a method id, so `mega` or `drive:work`. It must match the id in `accounts.PROVIDERS`. |
| `label` | `str` | The base display name, such as `MEGA (megatools)`. The account view may refine it per account. |
| `configured` | `(name) -> bool` | Whether that one account is usable right now: its credentials exist and any optional SDK or binary is importable or on PATH. |
| `creds_source` | `(name) -> str or None` | A short description of where that account's secret comes from, for `/health`, such as `oauth`, `keychain` or `token`. |
| `upload` | `(name, root, remote_dir, local_path, on_progress, overwrite) -> (ok, error, url)` | Upload one file and return the three tuple. The provider owns its account lock, the folders it needs to create and its own error policy. |
| `remote_exists` | `(name, remote_dir, filename) -> bool` | Whether the file is already there, without creating a folder or uploading a byte. |
| `download` | `(name, remote_dir, filename) -> bytes or None` | Read one small sidecar back: `None` when the file is absent, a raise when the read fails, and a refusal above `DOWNLOAD_LIMIT_BYTES` (4 MiB) rather than pulling an arbitrary remote file into memory. Used only to merge into an existing `series.json`, and it must never create a folder. It is fail-closed on purpose: the caller's alternative to reading the file is to leave it alone, because the entries every other volume published live in it. |
| `run_setup` | `(name, root, label) -> None` | The interactive wizard that `--setup-upload` calls. |
| `account` | `(instance) -> ProviderAccount` | The presentation view described below. |
| `metadata_owner` | `(name) -> bool` or `None` | Optional. Whether another program (mokuro-bunko) compiles this destination's `series.json`, so finalize skips its own read, merge and upload. All five shipped providers leave it `None` and are covered by the registry's generic `.mokuro-bunko.json` marker probe, which works through `download` and `root`; the hook exists for a backend that needs a signal the marker cannot carry. `series_owner` in the account metadata overrides either one, see [bunko.md](bunko.md). |

The `root(name)` method on `Provider` is not a field: it resolves the account through `accounts.py` and falls back to the provider default, so a provider never duplicates the root mapping.

`upload` receives the account name rather than the account instance, so a provider stays decoupled from the account registry. The provider also receives `overwrite`, whose meaning is uniform across backends: `fail` reports an existing file as an error, `skip` treats it as success, `overwrite` replaces it, and `replace` replaces it under the same name. `overwrite` is what a volume artifact wants, because the versioned sibling MEGA produces keeps the prior valid copy. `replace` exists for a file the caller regenerates from local state, which today is only the series manifest: a manifest renamed to `series.json.replace-abc123` is a manifest no reader reads. MEGA implements `replace` by deleting and re-uploading; Drive and OneDrive treat it exactly like `overwrite`, which already replaces in place.

## The account view

`Provider.account(instance)` returns a `ProviderAccount`, a small frozen value with three fields:

* `label` is the upload-methods display name. The usual spelling is `instance.display_name_for(label)`, which puts the account name after the provider name and keeps one provider note, such as `(megatools)`, attached to the provider.
* `extra` is the provider specific part of `UploadMethod.extra`. The caller adds the generic `provider`, `account`, `creds_source` and `tracked` keys, so a provider only adds what is genuinely its own, such as `library_root` and `email` for MEGA or `root` for Drive and OneDrive.
* `current_folder` is the human readable "current folder" a client shows.

## One folder walk

`walk_folders` in `providers/base.py` is the single implementation of "follow a `/` separated remote path". It takes a `find(parent_id, segment)` callback and, optionally, a `make(parent_id, segment)` callback:

* With `make` absent the walk only reads and returns `None` at the first missing segment. This is the walk behind `remote_exists`, so checking a path never creates anything.
* With `make` present the walk creates a missing segment and continues. Drive and OneDrive both use the creating form, while their read only form passes `find` alone.

The callbacks let each provider keep its own request semantics. Drive resolves a folder with one `files.list` query and creates with one `files.create` call. OneDrive uses a read only probe for the read walk and a separate probe plus `POST children` for the creating walk, with the 409 race handled inside the create callback. WebDAV does not use the walk at all: a remote path there is a URL suffix, so it creates each segment with `MKCOL` and probes it with a `Depth: 0` `PROPFIND`. The shared helper only sequences the segments, starting at `root`, and raises `ValueError` on an empty path when `require_nonempty` is set.

## The registry

`providers/__init__.py` holds `_PROVIDERS`, a mapping from provider id to `Provider`, built from the provider modules at import. `_method_configured`, `upload_file`, `remote_file_exists` and `_method_current_folder` are lookups in that mapping, not chains of `instance.provider` tests. `register_provider` adds or replaces one entry, and `setup_runner(provider_id)` returns the wizard that `cli.py` calls.

## Adding a provider

1. Copy `providers/mega.py` or `providers/drive.py` to `providers/<id>.py` and adapt it. Keep the module level `PROVIDER` value at the end so it can reference the functions above it.
2. Add the id to `accounts.PROVIDERS`, to `accounts.default_root` and to `accounts._PROVIDER_LABELS`. Accounts, ids and the `<provider>:<name>` spelling stay in `accounts.py` by design.
3. Add `<module>.PROVIDER` to the tuple that builds `_PROVIDERS` in `providers/__init__.py`.
4. Optionally name the provider's own optional dependency in its `_ensure_python_deps` call, so its wizard can install what is missing. The base and `cloud` extras deliberately ship no cloud SDK, because the supported setup path is rclone; the native Drive, OneDrive and WebDAV wizards install their SDK on request.

Nothing in `cli.py`, `api.py` or the lookup functions changes, and the fake provider test below is the check that this stays true.

## Accounts stay in accounts.py

`accounts.py` remains the source of truth for which accounts exist, how a method id is spelled, and the default root per provider. A provider never enumerates accounts and never parses a method id; it receives one account name and answers questions about it. The sticky default target also stays untouched in `providers/__init__.py`.

## Proving the seam

`tests/test_provider_registry.py` builds a fake provider and registers it through `register_provider`, then asserts that it is reachable from the upload-methods listing, `remote_file_exists`, `upload_file` and `setup_runner`. That test is what keeps "add a provider" a one module change plus registration rather than a claim.
