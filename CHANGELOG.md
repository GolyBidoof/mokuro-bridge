# Changelog

## Unreleased

## v0.7.1

The v0.7.0 artifact-validation crash, on a base install.

### Fixed

- **Reading a generated `.mokuro` no longer needs the OCR engine.**
  `_validate_mokuro_artifact()` loaded the file through
  `_mokuro_submodule("utils").load_json`, so on an install without the optional
  engine it raised `AttributeError: 'NoneType' object has no attribute '__name__'`
  from the missing-package placeholder instead of validating anything. CI caught
  it; a developer's machine did not, because a checkout symlinks the engine in and
  it imports as a namespace package. The validator parses a file the bridge
  generated, so it uses the standard library's `json` and has no business
  depending on PyTorch.
- `_mokuro_submodule()` now raises an `ImportError` naming the missing dependency
  and the command that installs it, rather than an `AttributeError` that pointed
  at neither the cause nor the fix.

## v0.7.0

Installable as a package, plus a version check that says when a newer release exists.

### Added

- **`pipx install mokuro-bridge`** (or `uv tool install`). `pyproject.toml` builds a wheel carrying two console scripts, `mokuro-bridge` and `mokuro-bridge-ocr`. The git-checkout install is unchanged: `server.py` and `ocr_folder.py` stay as wrappers, so existing launchers, the launchd plist and the README keep working.
- `--check-update` prints the newest published release and the upgrade command for the way *this* copy was installed (pipx, pip or a git checkout). Exit codes: 0 up to date, 1 update available, 2 the check could not be completed. `--version` prints the running version.
- `/health` gained `update_check`, `latest_version`, `update_available`, `update_url` and `update_error`, read from a cache so the request never waits on a socket. The check runs in the background at startup and is refreshed at most every six hours (`MOKURO_BRIDGE_UPDATE_TTL_S`). A failure is cached too, so an offline machine does not retry in a loop. `MOKURO_BRIDGE_UPDATE_CHECK=0` turns it off.
- `python -m mokuro_bridge` runs the bridge, for an install whose script directory is not on `PATH`.
- `mokuro_bridge/update.py`, with tests for version comparison, the injected fetcher, TTL caching, and the offline and disabled paths.
- `tests/test_packaging.py` holds the packaging contract: `pyproject.toml` and `requirements*.txt` cannot drift apart, every package directory is listed for the wheel, the installed-vs-checkout output directory rule is pinned, and the console script targets must resolve.
- CI: `.github/workflows/tests.yml` runs the suite on Linux for Python 3.10 to 3.13, plus macOS and Windows on 3.13, and builds the wheel to assert the vendored `./mokuro` fork never ships inside it. `.github/workflows/publish.yml` publishes a `vX.Y.Z` tag to PyPI with Trusted Publishing.

### Fixed

- **`/session/resume` answered 500 on every call.** The module called `_safe_component()` without importing it, so folder ingest failed with a `NameError` and no volume could be handed over from an existing directory. The import is there now, and `tests/test_endpoints.py` locks the endpoint.
- **OCR is about 1.8x faster again.** v0.6.0 raised `num_beams` to 4 as a side effect of an unrelated commit, costing ~43 ms/crop against ~23.5 ms/crop at one beam (measured on MPS). The default is back to one beam.
- **A page resent under a new name no longer duplicates the volume.** A 249-page volume was once finalized as 498 pages, a complete book followed by a jumbled partial copy, and shipped that way. A resend now supersedes the old page instead of sitting beside it.
- **A volume wedged by the bridge's own staging can be finalized again.** Remote uploads stage in `<vol>/_mega_upload` and the ingest walk rejects images in subdirectories, so that debris made every later attempt fail too. It is cleared on the next attempt.
- **A volume could never be reused.** The collision check ran *after* the work
  directory was created, so it was always true and every fresh volume was given a
  random `<title>_<6 hex>` suffix. A later `reuse_existing` start looked the
  session up by the plain title, found nothing, and created yet another directory,
  so the volume and its OCR cache were thrown away on every run. The check now
  happens before the directory is made.
- Imprint and edition tags no longer split one series across folders. `サンプル作品(1) (サンプルコミックス)` now derives a single series rather than one per volume.

### Changed

- `page_num` is optional on `/page` and `/page-local`. Clients that omit it are unaffected; it was already documented as advisory.
- The fetch proxy serves `/_bwdd/<host>/<path>` and advertises it as `pathUpstream` in `/health` (`fetchPathUpstream`), for clients that need a fixed origin.
- `MOKURO_BRIDGE_FETCH_UPSTREAM` and `MOKURO_BRIDGE_FETCH_ALLOWED_HOSTS` are now documented in the README and `.env.example`. The allow-list still defaults to `*`; `_target_is_safe()` is the gate that rejects loopback, private, link-local, multicast and reserved targets.
- The CLI moved from `server.py` into `mokuro_bridge/cli.py`. `server.py` is now a wrapper that also re-exports the ASGI app, so `python server.py` and `uvicorn server:app` behave as before. `ocr_folder.py` moved into the package for the same reason, with a wrapper left behind.
- **The recommended OCR engine is now the mokuro fork** rather than the stock
  package, since it adds batched recognition and is worth roughly 1.8x on a
  measured workload. It installs in one line, `pip install "mokuro @ git+https://github.com/GolyBidoof/mokuro"`,
  and the bridge detects its batch API by introspection, so there is nothing to
  configure. Stock mokuro from PyPI still works and remains the fallback in
  `requirements-ocr.txt`.
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

- **Named upload accounts.** Every provider can now hold more than one account: `python server.py --setup-upload mega --name work` adds a second MEGA account, and it is addressed as `mega:work` (`upload_method=mega:work`, `ocr_folder.py --upload-method mega:work`, `MOKURO_BRIDGE_UPLOAD_DEFAULT=mega:work`). A bare provider id still means the default account, so existing clients and configs are untouched.
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