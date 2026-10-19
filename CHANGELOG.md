# Changelog

## Unreleased

### Added

- **mokuro-bunko can own the destination's metadata.** `mokuro-bridge --setup-bunko` builds the newest commit of Bunko's `0.8/remote-storage` development branch (server-only, no OCR engines), writes a cloud-library config for the bridge's upload account, signs in through Bunko's loopback control API, and starts it. Finalize then skips reading, merging and uploading `series.json` for that destination, uploads the `.mokuro` before the `.cbz` so Bunko never queues OCR for a pre-OCR'd volume, and reports a `series_json` frame with `external: true`. Marker detection is fail-open, so a destination without Bunko keeps the old behaviour. `--update-bunko`, `--bunko-status`, `--bunko-logs` and `--stop-bunko` manage the instance; `MOKURO_BUNKO_REF`, `MOKURO_BUNKO_BIN` and `MOKURO_BUNKO_OFFLINE` cover pinned, prebuilt and air-gapped installs. See [docs/bunko.md](docs/bunko.md).
- **A `.mokuro-bunko.json` marker probe and a `Provider.metadata_owner` capability.** The bridge understands the marker the reader also uses (fresh active state, `compiles` listing `series.json`, heartbeat inside `ttl_s + 5 min`), and a WebDAV/Bunko destination can declare metadata ownership explicitly through the account's `series_owner` setting.

## v0.10.0

### Added

- **A configuration dashboard served by the bridge.** `GET /` is one self-contained page: inline CSS and JavaScript, no external assets, no CDN, so it works offline and screenshots cleanly for a README. It shows the live status pill, cards for the engine, the OCR queue, sessions, the proxy ports and the update state, every destination with its credentials source and folder, and every configurable setting with its current source. From it you can browse a provider's real folder tree to choose an account's default upload directory, set the default destination, choose the default local output folder, create that folder, and remove an account. See [docs/dashboard.md](docs/dashboard.md).
- **`mokuro-bridge --status`**: a self-refreshing full-screen terminal display for a service running in the background, showing the engine, destinations (the default starred), the OCR queue, the current upload with a progress bar, the proxy ports and what the bridge is doing right now. Keys are `r` refresh, `c` dashboard URL, `q` quit; piped output is a single frame with no escape sequences, and the terminal is restored on exit and on Ctrl-C.
- **Providers gained a `list_folders` capability**, read-only and capped, so the wizard can browse MEGA (`megatools ls`, parsed against the node-type column), Google Drive (`files.list`) and OneDrive (children) without creating anything. Local browsing lists directories only, and only under the same roots the ingest guard already allows.
- **A settings file the wizard writes**, `~/.config/mokuro-bridge/settings.json` (`MOKURO_BRIDGE_SETTINGS_FILE`). Proxy port count, fetch concurrency, the default output folder, extra read roots, the OCR chunk size and idle flush, and the minimum page count for a remote upload are editable from the page and apply on the next start. Precedence is environment, then file, then built-in default, so an existing launchd plist or shell export keeps winning, and the page names each value's source.
- `/health` now reports each upload method's `current_folder`, so a client can show where a target writes without a second request.

### Changed

- `--remove-upload` and the dashboard's Remove button share one implementation (`providers.forget_account`), so they cannot disagree about what removing an account deletes. The provider error redactor moved to `providers/base.py` for the same reason.

### Security notes

- Configuration writes are refused with `403` unless the request carries a loopback `Origin` or none at all. The bridge is unauthenticated and `CORS_ORIGINS` defaults to `*`, so any page in a browser can already reach it; this keeps a hostile page from repointing an upload destination or removing an account. Reads are not gated, so monitoring still works.
- Local folder browsing is confined to the ingest roots (home, the system temp locations, and any configured extra roots) and refuses anything else with `400`.
- An account whose credentials are not working cannot be made the default: the endpoint answers `409` instead of reporting success for a change the sticky-default writer would silently refuse.

## v0.9.0

### Added

- **Every finalize now publishes the reader's `series.json`.** reader.mokuro.app keeps its per-series index in `<Series Title>/series.json` inside the series folder, and it only creates that file itself for a series the device already knows locally, so a library filled by a script (or by this bridge) had no index and no path to one. A local finalize writes the manifest in the same transaction as the volume files, and a remote finalize creates or updates it at the destination once the volume files have landed. A second volume is merged in without disturbing the entries the first published, the destination's existing manifest is read back first, and the file is left byte-identical when the volume is already listed.
- `--check-series-reference` re-fetches the upstream files that define the format and reports whether the pin still matches and whether upstream has moved (exit 0 matching and current, 1 upstream moved, 2 the check could not run). `--refresh-series-reference` re-pins them. Nothing is fetched at upload time or at startup.
- Providers gained a `download` capability, used only to read an existing `series.json` before merging into it, and `overwrite=replace` for a file the caller regenerates from local state: MEGA deletes and re-uploads under the same name instead of leaving a `series.json.replace-xxxx` sibling that no reader reads.
- [docs/series-json.md](docs/series-json.md) documents the format, the merge rules and the pin.

### Design notes

- The format belongs to Gnathonic's mokuro-reader (GPL-3.0), so nothing is vendored into this MIT project. `mokuro_bridge/data/series_json_pin.json` pins the repository, commit and per-file SHA-256 of every upstream file that creates the file, `mokuro_bridge/series_json.py` ports that contract, and the port is checked against a parity fixture probed from the pinned upstream implementation.
- The bridge publishes no series facts, so it never writes the current time into `updated_at`: its files carry the epoch sentinel, which loses every facts comparison and cannot outrank or strand a real link on another device.
- A `series.json` the bridge could not read is never overwritten, and the finalize reports `partial_upload` in that case rather than success, because a folder without an index is a series the reader cannot build.

### Fixed

- **A local finalize that saved the volume files but not the index reported success.** A destination without `series.json` is invisible in the reader, so the manifest step now counts toward the finalize's success and the working files are kept when it fails.

## v0.8.1

### Fixed

- **Windows reported an installed fork as upstream.** A `file:` URL spells a Windows drive as `/C:/...`, and the path was built without unwrapping that leading slash, so it named nothing, the checkout walk found no `.git`, and `mokuro-flavor` answered `upstream` for a fork install. Found by the Windows CI job, which had been red on every platform run since the check was added.

## v0.8.0

### Added

- `POST /destination/check` answers, without creating a folder or uploading a byte, whether a finished volume is already at a destination: a `.cbz` stat for `local`, a folder walk for MEGA, Drive and OneDrive. It is what lets a client decide whether to re-download and re-OCR a volume at all.
- `GET /sessions` restores persisted sessions after the service restarts, so a client that reconnects can still see work it had started. The session snapshot carries `ingesting` and `finalizing` so "pages are still arriving" is distinguishable from "the volume is being packaged".
- `mokuro-bridge --update` upgrades this install to the newest release in one command. It reads the install method from the paths the tool left behind (`pipx`, `uv tool`, a plain pip install or a git checkout), prints every command before running it, and prints how to restart the service afterwards. It never restarts the service itself, and it runs nothing for an install it cannot tell apart: that case prints the candidate commands instead.
- The service logs a one-line notice at startup when a newer release exists, naming the exact command that gets it. The notice rides the background check the CLI already starts, so it never blocks startup, it is silent when offline, and `MOKURO_BRIDGE_UPDATE_CHECK=0` turns it off.
- `mokuro-bridge --install-mokuro` replaces the installed `mokuro` with the faster [GolyBidoof fork](https://github.com/GolyBidoof/mokuro) in one command, installing the wheel attached to its `v0.4.0` release (the git tag is the fallback if that asset is ever removed). It reports the current distribution, version metadata and flavor first, uninstalls the old distribution before installing the fork (the two share the distribution name, which is what makes the swap clean), prints every command before running it, and verifies the result by importing `mokuro` in a fresh interpreter. It refuses with exit 2 on an externally managed interpreter (PEP 668) or an install it cannot safely change, and exits 1 when the install or the verification fails. The per-environment commands and the way back to upstream are in [docs/mokuro.md](docs/mokuro.md).
- `/health` reports which `mokuro` is installed as `mokuro_flavor`: `upstream`, `fork`, `absent` or `unknown`. It is read from the install provenance in `direct_url.json` and from the module path, never from the version string, because a fork editable install reported stale metadata on a real machine. `mokuro_installed` keeps working.
- When the installed engine is upstream or unrecognised, the service logs one line at startup naming `mokuro-bridge --install-mokuro`, the single command that switches to the faster fork. It reads local metadata only, never blocks startup, never raises, is silent when the fork is already installed, and `MOKURO_BRIDGE_MOKURO_NOTICE=0` turns it off.

### Fixed

- `--check-update` called the newest *published* tag "latest" even when the running build was newer than it, which reads as a downgrade while preparing a release. It now says the build is ahead of that tag.
- **A provider error no longer hands the caller a live credential.** `/destination/check` interpolated the raw provider exception, and Graph and Drive errors embed the request URL, which for those providers carries an access token in the query string. It now goes through the same redaction as finalize.
- **Re-sending a page the bridge already has no longer fails.** The digest compare runs before the "already has completed OCR" branch, so a capture client that retries its last page after a restart is acknowledged as an idempotent retry instead of refused. Bytes that *differ* are still a 409: the stored OCR result no longer matches the image.
- `/sessions` re-read and re-logged every session JSON on every poll; a session that is already live is now served from memory.
- `/destination/check` falls back to a generated title for a blank one, matching `/session/start`, instead of answering 400.

### Changed

- `--setup-mega` stays accepted but is no longer advertised; the message in `providers/mega.py` and `setup-keychain.sh` now points at `mokuro-bridge --setup-upload mega`.
- `--check-update` now names the single `mokuro-bridge --update` command instead of printing the raw per-method upgrade lines; `--update` still prints each line, right before it runs it.
- Documented the sticky upload default, which remembers the last explicit `upload_method` for every later request, in `docs/architecture.md`. The endpoint list in that document also caught up with the twelve routes that now exist.

## v0.7.4

### Fixed

- The local-ingest root list was defined twice in `config.py`, so every `MOKURO_BRIDGE_INGEST_ROOTS` entry was appended twice. Shipped in 0.7.3.

## v0.7.3

### Fixed

- `test_metadata_file_is_private` is skipped on Windows, which has no POSIX mode bits: `st_mode` reports `0o666` for any writable file and `chmod` only toggles read-only. The boundary there is the user profile ACL.

### Changed

## v0.7.2

### Added

- `MOKURO_BRIDGE_INGEST_ROOTS`: comma-separated absolute paths added to the local-ingest allowlist, which is otherwise the home directory plus the POSIX temp paths. Those resolve to `C:\tmp` on Windows and never match, so a library on a second drive had no way in. Relative entries are ignored.

### Changed

- The `publish` workflow's build job runs on pushes to `main` as well as tags, so the sdist and wheel are built and `twine check`ed on every commit. Uploading still requires a `v*` tag or a manual dispatch.

## v0.7.1

### Fixed

- `_validate_mokuro_artifact()` reads with the standard library instead of `_mokuro_submodule("utils").load_json`. Without the optional OCR engine the latter raised `AttributeError` from a `None` placeholder. The validator parses a file the bridge generated and does not need PyTorch.
- `_mokuro_submodule()` raises an `ImportError` naming the missing dependency rather than an `AttributeError`.

## v0.7.0

Installable as a package, plus a version check that says when a newer release exists.

### Added

- **`pipx install mokuro-bridge`** (or `uv tool install`). Two console scripts: `mokuro-bridge`, `mokuro-bridge-ocr`. `server.py` and `ocr_folder.py` stay as wrappers, so the checkout install is unchanged.
- `--check-update` prints the newest published release and the upgrade command for the way *this* copy was installed (pipx, pip or a git checkout). Exit codes: 0 up to date, 1 update available, 2 the check could not be completed. `--version` prints the running version.
- `/health` gained `update_check`, `latest_version`, `update_available`, `update_url` and `update_error`, read from a cache so the request never waits on a socket. The check runs in the background at startup and is refreshed at most every six hours (`MOKURO_BRIDGE_UPDATE_TTL_S`). A failure is cached too, so an offline machine does not retry in a loop. `MOKURO_BRIDGE_UPDATE_CHECK=0` turns it off.
- `python -m mokuro_bridge` runs the bridge, for an install whose script directory is not on `PATH`.
- `mokuro_bridge/update.py`, with tests for version comparison, the injected fetcher, TTL caching, and the offline and disabled paths.
- `tests/test_packaging.py` holds the packaging contract: `pyproject.toml` and `requirements*.txt` cannot drift apart, every package directory is listed for the wheel, the installed-vs-checkout output directory rule is pinned, and the console script targets must resolve.
- CI: `.github/workflows/tests.yml` runs the suite on Linux for Python 3.10 to 3.13, plus macOS and Windows on 3.13, and builds the wheel to assert the vendored `./mokuro` fork never ships inside it. `.github/workflows/publish.yml` publishes a `vX.Y.Z` tag to PyPI with Trusted Publishing.

### Fixed

- **`/session/resume` answered 500 on every call.** `_safe_component()` was called without being imported. Fixed, and locked by `tests/test_endpoints.py`.
- **OCR is about 1.8x faster again.** v0.6.0 raised `num_beams` to 4, costing ~43 ms/crop against ~23.5 at one beam (measured on MPS). Back to one beam.
- **A page resent under a new name supersedes the old one** instead of sitting beside it. A 249-page volume was once finalized as 498 and shipped that way.
- **A volume wedged by the bridge's own staging can be finalized again.** Remote uploads stage in `<vol>/_mega_upload` and the ingest walk rejects images in subdirectories, so that debris made every later attempt fail too. It is cleared on the next attempt.
- **A volume could never be reused.** The collision check ran *after* the work directory was created, so it was always true and every fresh volume was given a random `<title>_<6 hex>` suffix. A later `reuse_existing` start looked the session up by the plain title, found nothing, and created yet another directory, so the volume and its OCR cache were thrown away on every run. The check now happens before the directory is made.
- Imprint and edition tags no longer split one series across folders. `サンプル作品(1) (サンプルコミックス)` now derives a single series rather than one per volume.

### Changed

- `page_num` is optional on `/page` and `/page-local`. Clients that omit it are unaffected; it was already documented as advisory.
- The fetch proxy serves `/_bwdd/<host>/<path>` and advertises it as `pathUpstream` in `/health` (`fetchPathUpstream`), for clients that need a fixed origin.
- `MOKURO_BRIDGE_FETCH_UPSTREAM` and `MOKURO_BRIDGE_FETCH_ALLOWED_HOSTS` are now documented in the README and `.env.example`. The allow-list still defaults to `*`; `_target_is_safe()` is the gate that rejects loopback, private, link-local, multicast and reserved targets.
- The CLI moved from `server.py` into `mokuro_bridge/cli.py`. `server.py` is now a wrapper that also re-exports the ASGI app, so `python server.py` and `uvicorn server:app` behave as before. `ocr_folder.py` moved into the package for the same reason, with a wrapper left behind.
- **The recommended OCR engine is now the mokuro fork** rather than the stock package, since it adds batched recognition and is worth roughly 1.8x on a measured workload. It installs in one line, `pip install "mokuro @ git+https://github.com/GolyBidoof/mokuro"`, and the bridge detects its batch API by introspection, so there is nothing to configure. Stock mokuro from PyPI still works and remains the fallback in `requirements-ocr.txt`.
- The version check uses httpx rather than urllib. httpx is already a base dependency and verifies TLS against certifi, where a python.org macOS build ships no CA store of its own and fails the request with `CERTIFICATE_VERIFY_FAILED`.
- The default output directory now depends on how the bridge was installed. A git checkout keeps using `<repo>/output`; an installed wheel uses `~/mokuro-bridge/output`, because the old default resolved to the parent of `site-packages` and would have put the user's volumes inside the virtualenv, where a `pipx upgrade` rebuild can strand them. `MOKURO_BRIDGE_OUTPUT_DIR` still overrides both.
- `./mokuro` is excluded from the distribution explicitly. A wheel shipping a top-level `mokuro` package would shadow the real OCR engine that users install from PyPI.
- README quickstart now leads with pipx and keeps the checkout path beside it, and a new "Keeping it up to date" section documents the check, the `/health` fields and the restart after an upgrade.

### Notes

- Nothing is updated automatically. `--check-update` reports, and prints the command; applying it, and restarting a service, stays your call, because a restart mid-OCR would lose work.
- The first PyPI release is v0.7.0. v0.6.0 was git-only, so there is no version collision, and the update check compares against the v0.7.0 tag. `publish.yml` fails the build if the tag and the packaged version disagree.
- Publishing needs a one-time pending publisher on PyPI: owner `GolyBidoof`, repository `mokuro-bridge`, workflow `publish.yml`, environment `pypi`. Until that exists the publish job fails at its OIDC step; the build job still runs and its wheel is kept as an artifact.

## v0.6.0

Several accounts per upload provider, and a fix for the keychain lookup that made a re-run of the MEGA wizard appear to do nothing.

### Added

- **Named upload accounts.** Every provider can now hold more than one account: `mokuro-bridge --setup-upload mega --name work` adds a second MEGA account, and it is addressed as `mega:work` (`upload_method=mega:work`, `ocr_folder.py --upload-method mega:work`, `MOKURO_BRIDGE_UPLOAD_DEFAULT=mega:work`). A bare provider id still means the default account, so existing clients and configs are untouched.
- Each account has its own remote root (`--root`), display label (`--label`) and credential store: one keychain item per MEGA email, a 0600 credential or token file per Drive/OneDrive account. Non-secret metadata lives in `~/.config/mokuro-bridge/accounts/<provider>__<name>.json` (`MOKURO_BRIDGE_ACCOUNTS_DIR`).
- `python server.py --list-uploads` prints every account with its readiness, credential source and remote root; `--remove-upload mega:work` forgets one.
- `/upload-methods` and `/health` list one entry per account, each with `provider`, `account`, its own root and `current_folder`.
- `mokuro_bridge/accounts.py`: the instance registry (id parsing, metadata, legacy default resolution), plus tests for id parsing, account isolation and the keychain cleanup rules.

### Fixed

- The macOS keychain lookup paired the email from one `mega.nz` item with the password from another, and an account-less `find-internet-password` kept returning the *older* item. A stale entry for a previous address therefore shadowed the working one and every upload failed with `API call 'us' failed: Server returned error ENOENT` even after a successful wizard run. The lookup now reads both halves from a single item, prefers the email recorded for the account, and the wizard removes only the entries it knows are orphaned, never a sibling account's.

### Notes

- `accounts_dir()` no longer creates its directory on read, so importing the bridge cannot fail on an unwritable `$HOME`.

## v0.5.2

Two install failures reported from the field. Both are diagnosed in the README now, and one is handled by the server itself.

### Added

- `server.py` prints an actionable message when a base dependency is missing, instead of a bare traceback. It names the interpreter that failed, gives the matching `-m pip install` line, and points at the usual cause: installing into one Python and running another.

### Documented

- `No space left on device` while pip builds a wheel is normally `$TMPDIR`, not the disk. Many distros mount `/tmp` as a small tmpfs, so `df -h /` reports a different filesystem and the machine genuinely has free space. Building `unidic-lite`, which ships an sdist and no wheel, unpacks about 45 MB of dictionary into it. The fix is `TMPDIR=~/.cache/pip-tmp pip install ...`.
- `ModuleNotFoundError: No module named 'fastapi'` means pip targeted a different Python. Use `python3 -m pip` rather than a bare `pip`, and activate the virtualenv in every new terminal.

### Notes

- Both failures come from the OCR engine's dependency tree, which the base install no longer contains as of v0.5.1. Installing the bridge on its own now builds nothing from source.

## v0.5.1

The OCR engine is no longer a base dependency.

`mokuro` depends on PyTorch, so `pip install -r requirements.txt` pulled several GB onto machines that only ever wanted page capture, the fetch accelerator or uploads. The bridge already ran without an engine, with `/health` reporting `mokuro_installed: false`, so this is a packaging fix rather than a behavioural one.

### Changed

- `requirements.txt` is the server only now: fastapi, uvicorn, python-multipart, httpx and keyring.
- `mokuro` moved to the new `requirements-ocr.txt`, to install when you want OCR. A `MOKURO_REPO` checkout still works and needs neither file.
- README quickstart and troubleshooting updated: `mokuro_installed: false` is the expected state on a base install, not a fault to chase.

### Added

- `tests/test_optional_ocr.py` imports the API, the OCR module, the providers and `/health` with `mokuro` blocked on the meta path, and checks the fetch accelerator still answers.
- `tests/test_imports.py` walks the AST of every module and fails if anything outside the base requirements is imported at module level, so the OCR engine and the cloud SDKs cannot quietly creep back into the startup path.

## v0.5.0

Turns the bridge into a page-fetch accelerator for the downloader userscript. Chrome allows 6 concurrent HTTP/1.1 connections per *origin*, and the page CDN refuses HTTP/2, so a viewer download was pinned to 6 sockets. Since an origin includes the port, the bridge now opens a range of extra localhost ports, each worth another 6 sockets to the browser.

### Added

- **Multi-port fetch proxy** (`mokuro_bridge/fetchproxy.py`). Serves the CDN proxy on `MOKURO_BRIDGE_FETCH_PORTS` extra ports, 48 by default. The ports it manages to bind are advertised on `/health` as `fetchProxyPorts`, so the userscript discovers them with no configuration on either side.
- **File-descriptor headroom.** Every proxied page holds a descriptor on each side of the bridge, so 48 ports at 6 sockets is roughly 600 at once. The bridge raises `RLIMIT_NOFILE` at startup, which covers launchd, `run.sh` and manual runs alike. macOS hands a launchd agent a soft limit of 256, which previously surfaced as `OSError: Too many open files` on accept() and ECONNRESET in the browser, with nothing pointing at the ulimit.
- **Test suite** under `tests/`, run with `python3 -m pytest`. Covers port binding, the proxy's forwarding and streaming behaviour, the host allow-list guard, upstream failure handling, and the environment defaults. No mokuro, torch or network access required.

### Changed

- The startup banner reports the fetch-proxy port range and the socket count it buys, or says why the proxy is off.
- `httpx` is a direct dependency now rather than an incidental one.

### Notes

- `UVICORN_RELOAD=1` disables the fetch proxy: the reloader spawns a second process that would contend for the same ports.
- The proxy resolves every host it is given and refuses loopback, private, link-local, multicast and reserved addresses, so it cannot reach the local network or the bridge itself. Its host allow-list defaults to `*`; `MOKURO_BRIDGE_FETCH_ALLOWED_HOSTS` narrows it. See the README for what that does and does not cover.
- Downloading never depends on the bridge. With it stopped, the userscript falls back to its own page and background-context lanes.
