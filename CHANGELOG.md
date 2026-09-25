# Changelog

## v0.6.0

Several accounts per upload provider, and a fix for the keychain lookup that
made a re-run of the MEGA wizard appear to do nothing.

### Added

- **Named upload accounts.** Every provider can now hold more than one account:
  `python server.py --setup-upload mega --name work` adds a second MEGA
  account, and it is addressed as `mega:work` (`upload_method=mega:work`,
  `ocr_folder.py --upload-method mega:work`,
  `MOKURO_BRIDGE_UPLOAD_DEFAULT=mega:work`). A bare provider id still means the
  default account, so existing clients and configs are untouched.
- Each account has its own remote root (`--root`), display label (`--label`)
  and credential store: one keychain item per MEGA email, a 0600 credential or
  token file per Drive/OneDrive account. Non-secret metadata lives in
  `~/.config/mokuro-bridge/accounts/<provider>__<name>.json`
  (`MOKURO_BRIDGE_ACCOUNTS_DIR`).
- `python server.py --list-uploads` prints every account with its readiness,
  credential source and remote root; `--remove-upload mega:work` forgets one.
- `/upload-methods` and `/health` list one entry per account, each with
  `provider`, `account`, its own root and `current_folder`.
- `mokuro_bridge/accounts.py`: the instance registry (id parsing, metadata,
  legacy default resolution), plus tests for id parsing, account isolation and
  the keychain cleanup rules.

### Fixed

- The macOS keychain lookup paired the email from one `mega.nz` item with the
  password from another, and an account-less `find-internet-password` kept
  returning the *older* item. A stale entry for a previous address therefore
  shadowed the working one and every upload failed with
  `API call 'us' failed: Server returned error ENOENT` even after a successful
  wizard run. The lookup now reads both halves from a single item, prefers the
  email recorded for the account, and the wizard removes only the entries it
  knows are orphaned — never a sibling account's.

### Notes

- `accounts_dir()` no longer creates its directory on read, so importing the
  bridge cannot fail on an unwritable `$HOME`.

## v0.5.2

Two install failures reported from the field. Both are diagnosed in the README
now, and one is handled by the server itself.

### Added

- `server.py` prints an actionable message when a base dependency is missing,
  instead of a bare traceback. It names the interpreter that failed, gives the
  matching `-m pip install` line, and points at the usual cause: installing into
  one Python and running another.

### Documented

- `No space left on device` while pip builds a wheel is normally `$TMPDIR`, not
  the disk. Many distros mount `/tmp` as a small tmpfs, so `df -h /` reports a
  different filesystem and the machine genuinely has free space. Building
  `unidic-lite`, which ships an sdist and no wheel, unpacks about 45 MB of
  dictionary into it. The fix is `TMPDIR=~/.cache/pip-tmp pip install ...`.
- `ModuleNotFoundError: No module named 'fastapi'` means pip targeted a
  different Python. Use `python3 -m pip` rather than a bare `pip`, and activate
  the virtualenv in every new terminal.

### Notes

- Both failures come from the OCR engine's dependency tree, which the base
  install no longer contains as of v0.5.1. Installing the bridge on its own now
  builds nothing from source.

## v0.5.1

The OCR engine is no longer a base dependency.

`mokuro` depends on PyTorch, so `pip install -r requirements.txt` pulled several
GB onto machines that only ever wanted page capture, the fetch accelerator or
uploads. The bridge already ran without an engine, with `/health` reporting
`mokuro_installed: false`, so this is a packaging fix rather than a behavioural
one.

### Changed

- `requirements.txt` is the server only now: fastapi, uvicorn, python-multipart,
  httpx and keyring.
- `mokuro` moved to the new `requirements-ocr.txt`, to install when you want OCR.
  A `MOKURO_REPO` checkout still works and needs neither file.
- README quickstart and troubleshooting updated: `mokuro_installed: false` is the
  expected state on a base install, not a fault to chase.

### Added

- `tests/test_optional_ocr.py` imports the API, the OCR module, the providers and
  `/health` with `mokuro` blocked on the meta path, and checks the fetch
  accelerator still answers.
- `tests/test_imports.py` walks the AST of every module and fails if anything
  outside the base requirements is imported at module level, so the OCR engine
  and the cloud SDKs cannot quietly creep back into the startup path.

## v0.5.0

Turns the bridge into a page-fetch accelerator for the downloader userscript.
Chrome allows 6 concurrent HTTP/1.1 connections per *origin*, and the page CDN
refuses HTTP/2, so a viewer download was pinned to 6 sockets. Since an origin
includes the port, the bridge now opens a range of extra localhost ports, each
worth another 6 sockets to the browser.

### Added

- **Multi-port fetch proxy** (`mokuro_bridge/fetchproxy.py`). Serves the CDN
  proxy on `MOKURO_BRIDGE_FETCH_PORTS` extra ports, 48 by default. The ports it
  manages to bind are advertised on `/health` as `fetchProxyPorts`, so the
  userscript discovers them with no configuration on either side.
- **File-descriptor headroom.** Every proxied page holds a descriptor on each
  side of the bridge, so 48 ports at 6 sockets is roughly 600 at once. The
  bridge raises `RLIMIT_NOFILE` at startup, which covers launchd, `run.sh` and
  manual runs alike. macOS hands a launchd agent a soft limit of 256, which
  previously surfaced as `OSError: Too many open files` on accept() and
  ECONNRESET in the browser, with nothing pointing at the ulimit.
- **Test suite** under `tests/`, run with `python3 -m pytest`. Covers port
  binding, the proxy's forwarding and streaming behaviour, the host allow-list
  guard, upstream failure handling, and the environment defaults. No mokuro,
  torch or network access required.

### Changed

- The startup banner reports the fetch-proxy port range and the socket count it
  buys, or says why the proxy is off.
- `httpx` is a direct dependency now rather than an incidental one.

### Notes

- `UVICORN_RELOAD=1` disables the fetch proxy: the reloader spawns a second
  process that would contend for the same ports.
- The proxy refuses any host outside its allow-list, so it cannot be used as an
  open proxy. Only the CDN host is reachable through it.
- Downloading never depends on the bridge. With it stopped, the userscript falls
  back to its own page and background-context lanes.
