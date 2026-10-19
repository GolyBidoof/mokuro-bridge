# How mokuro-bridge is put together

This is the explanation a maintainer needs. For what it does and why you would want it, read the [README](../README.md). The route-by-route contract is in [endpoints.md](endpoints.md), the destination and account model is in [destinations.md](destinations.md), and the client hand-off is in [clients.md](clients.md).

## The boundary

The bridge is deliberately ignorant of storefronts. It holds no store account, no cookie, no store protocol, and no store code. A capture client gets page images out of a store however it likes and POSTs them here; the bridge's entire job begins at the image.

That boundary is the load-bearing design decision. It means the bridge does not break when a store changes, does not need legal review per store, and can be reused by any client that can produce a JPEG. The cost is that it cannot help a client get the images, which is why the half that does is a separate project.

## The request lifecycle

The capture loop is three endpoints, and a client that uses them does OCR correctly:

```
POST /session/start          title, reuse_existing        -> a session id
POST /session/{id}/page      one page image, filename      -> stored, queued
POST /session/{id}/finalize  upload_method, ...            -> NDJSON progress, then done
```

Around that loop are nine more routes, twelve in total:

```
POST /session/resume              hand over a folder of pages already on disk
POST /session/{id}/page-local     ingest one page from a path on this machine
POST /session/{id}/cover          attach and push the cover before OCR finishes
GET  /session/{id}/status         poll one session's counters
GET  /sessions                    sessions worth resuming, after a restart
GET  /upload-methods              configured targets and the current default
POST /destination/check           is this volume already at the destination?
GET  /queue                       OCR queue depth, overall and per session
GET  /health                      version, engine, targets, queue, update state
```

Those twelve routes are the capture API. The dashboard's own routes live in `config_api.py` and are documented in [endpoints.md](endpoints.md).

`POST /session/resume` hands over a folder of pages that already exist on disk instead of uploading them. Clients prefer the resume route when the bridge is on the same machine, since a local bridge can read the folder directly and the pages never cross a socket.

`POST /destination/check` is read-only in both directions: for `local` it stats the finished `.cbz`, and for a remote account it walks folders to ask whether the file is there, without creating a folder or uploading a byte. Its failures are redacted the same way finalize's are, because provider errors embed the request URL and that URL carries an access token.

`GET /session/{id}/status` and `GET /sessions` return the same session snapshot. It carries `ingesting` and `finalizing` alongside the page counters, so a client can tell "pages are still arriving" from "the volume is being packaged" instead of inferring it from a counter that has stopped moving.

**Pages are OCR'd as they arrive**, in chunked batches, so capture and recognition overlap instead of running back to back. A 250-page volume is not a 250-page wait followed by an OCR pass; the OCR worker is already busy on page 40 while page 200 is still uploading.

**`finalize` is a stream, not a request.** It answers `application/x-ndjson`: one JSON frame per event, at least every 750 ms while the OCR queue drains and one per file during an upload. The client reads progress off the same connection that is doing the work, which removes the polling loop that would otherwise compete with it for the same bridge. A frame with `stage: "error"` is a terminal failure and must be raised, not ignored: a stream that simply ends without a `done` frame is a truncated upload and both bundled clients treat it as an error rather than success.

## Sessions

Each volume is a session with its own working directory under the work dir, holding the received pages, the OCR state and the assembled artefacts. Session state is persisted, so a bridge restart does not lose a volume in flight, and `GET /sessions` restores those persisted records on demand.

A finished session's working files are removed. Remote uploads stage inside the session's own directory first, and that staging is what wedged a re-finalize in 0.6.0: the ingest walk rejects images in subdirectories, so the leftovers made every later attempt fail too. It is now cleared on the next attempt.

Volume identity is the title, and titles carry edition suffixes (`（２）`, `1巻`, `【電子限定…】`, an imprint). `series_title_from_volume` strips a trailing tag so that one series does not split across four folders by edition. The check that gives a fresh volume its own directory happens before the directory is created: ask afterwards and it is always true, every volume gets a random suffix, and the later `reuse_existing` lookup cannot find it. A volume therefore keeps the clean title unless that name is genuinely taken, in which case the bridge adds a short random suffix such as `_a1b2c3`.

## OCR and scheduling

`ocr.py` is a bounded worker pool with a semaphore, and the model is an engine abstraction with two implementations: the stock `mokuro` package from PyPI, and a fork with a faster batch OCR API detected by introspection. The fork can come from `MOKURO_REPO`, which points at a checkout, or from installing the fork under the `mokuro` name. The engine is an optional install because it drags in PyTorch, which is several gigabytes; the bridge runs without it, `/health` reports `mokuro_installed: false`, and only OCR is unavailable.

The fork exists because a single setting was worth about 1.8x: `num_beams` had been raised to 4 by a commit about something else entirely, costing roughly 43 ms/crop against 23.5 ms/crop at one beam. `/health` reports `mokuro_fork_api` when the batched path is active.

Pages are grouped into chunks (default 8, `OCR_CHUNK_SIZE`) and flushed early after an idle interval (default 1.5 s, `OCR_IDLE_FLUSH_S`), and the worker takes up to `OCR_BATCH_SIZE` pages per pass (default `max(OCR_CHUNK_SIZE, 32)`) so the engine stays busy while another volume is being read. With several volumes queued, the default fair selector (`MOKURO_BRIDGE_OCR_FAIR_SCHEDULING`, on by default) gives each active session a turn and caps finalize work at a share of a mixed batch (`MOKURO_BRIDGE_OCR_FINALIZE_PRIORITY`, default 0.5), so one volume finishing does not starve the others. Setting the fair selector to `0` restores the historical FIFO selector. `GET /queue` exposes the resulting depths.

## The page-fetch accelerator

This is the other half of the product, and the half that is not obvious. It is started by `cli.py` when `MOKURO_BRIDGE_FETCH_PORTS` is non-zero, with no dependency on the OCR engine being installed, so a client that only wants faster capture can run the bridge and ignore everything else.

A browser allows six concurrent HTTP/1.1 connections per **origin**, and an origin is scheme + host + **port**. A store's page CDN is one host and refuses HTTP/2, so a download is pinned to six sockets no matter how fast the link is.

Since the port is part of the origin, the bridge opens a range of extra localhost ports, each serving the same small proxy. The browser sees each as a fresh origin and gets six more sockets, while the bridge fetches under no browser limit at all. It binds what it can and advertises the range on `/health` as `fetchProxyPorts`; a client discovers them on its own and there is nothing to configure. The default is 48 ports (`MOKURO_BRIDGE_FETCH_PORTS`, 0 disables), so the proxy adds 288 browser sockets. `MOKURO_BRIDGE_FETCH_CONCURRENCY` caps simultaneous upstream fetches (0 means no cap), and `MOKURO_BRIDGE_FETCH_UPSTREAM` overrides the CDN being accelerated.

Two consequences worth knowing:

- **It costs file descriptors.** Each port holds a descriptor on each side of the bridge, so 48 ports at 6 sockets is about 600 at peak. A launchd agent starts with a soft limit of 256, and exhausting it shows up as `OSError: Too many open files` on `accept()` and ECONNRESET in the browser, with nothing pointing at a ulimit. The bridge raises `RLIMIT_NOFILE` at startup to cover it. To opt out, lower `MOKURO_BRIDGE_FETCH_PORTS` or set it to `0`.
- **Downloading never depends on the bridge.** With the accelerator stopped, a client falls back to its own lanes. The bridge is an accelerator, not a dependency of capture.

## Upload providers and accounts

`providers/` holds MEGA, Google Drive, OneDrive and WebDAV, each with its dependencies kept out of the base install so a user who only writes to disk does not install an OAuth library or an HTTP client. The terminal setup wizard detects a missing dependency and offers to install it, once, with consent; the dashboard's Add destination disables that provider and names what is missing. The full model is in [destinations.md](destinations.md).

**Accounts have aliases.** `mega`, `mega-2`, `drive-next`, `webdav`: each account has its own remote root, display label, credential store and alias, which is the id `/health`, `/upload-methods` and `/config` report and the value a client passes as `upload_method`. Accounts are added from the dashboard for all four providers (MEGA form, Google OAuth, Microsoft device code, WebDAV URL plus Basic credentials) or from the terminal with `--setup-upload`. Historical spellings (`mega`, `mega:work`, `drive:main`) still resolve to the same account, so existing configuration keeps working. Renaming changes only the alias; the storage name, credentials and default are preserved.

**The default target is sticky.** Once any client asks for a target explicitly (`upload_method=mega`, `upload_method=local`, the legacy `upload_to_mega`), that choice is remembered in the work dir and becomes the default for every later request, including for other clients, until another explicit choice replaces it. It is deliberate, a user who sets a destination once should not have to set it every run, but it means one client's one-off `--dest local` changes where the next client's volume lands. Only a usable target is remembered, and the choice is committed only after a finalize owns its session lease. `MOKURO_BRIDGE_UPLOAD_DEFAULT` seeds the value before any explicit choice. `GET /upload-methods` always reports which target is currently the default, so a client can show it rather than assume.

Credentials live in the OS keychain where one exists, otherwise in 0600 files under `~/.config/mokuro-bridge/`. Nothing is ever written to this repository. A metadata file describes each account without its secret (alias, label, root, and for MEGA the email or for WebDAV the server URL and username), so `--list-uploads` and `/config` can show what is configured and ready without touching a password. A WebDAV account is a user-supplied base URL plus Basic-auth credentials; the URL is checked once before it is stored and the password goes to the same credential store as the other providers.

## Security posture

Stated plainly, because it is a deliberate design and not an oversight:

- **The bridge is unauthenticated.** No token, no login. It binds to `127.0.0.1` by default (`MOKURO_BRIDGE_HOST`) and that is the entire access control. The intended deployment is loopback-only; binding it to a LAN address means anyone who can reach the port can start a volume, feed it pages, and have the result written or uploaded.
- **Configuration writes are gated to a loopback `Origin`.** The dashboard can repoint an upload destination, add or remove an account and change settings, so every configuration `POST` answers `403` when the request carries a non-loopback `Origin`, while curl and the CLI (no `Origin`) are accepted. The Google Drive OAuth callback is a `GET` opened by Google's redirect and is not gated; it accepts only the one-time `state` this bridge created. Account setup responses never contain a credential, token or client secret, and a Drive client secret pasted into the page is used for that sign-in and not persisted.
- **CORS is a wildcard by default**, deliberately: the client runs on whichever storefront you are reading from, and pinning a list would mean a new store silently stops working. `CORS_ORIGINS` is the narrowing knob.
- **The fetch proxy is not restricted to a CDN by default.** `MOKURO_BRIDGE_FETCH_ALLOWED_HOSTS` defaults to `*`, so it does nothing until set. The real gate is `_target_is_safe()`, which refuses anything resolving to a loopback, private, link-local, multicast or reserved address, so LAN hosts and `169.254.169.254` are refused. Any public host is fetchable, so this is a way to reach the public internet, not a closed pipe to one CDN.
- **There is no path traversal.** Page names go through a validator, the ingest resolver confines `source_dir` and `page-local` to your home and the system temp locations, and uploads are extension-checked before anything is read. A caller can drive the bridge; it cannot read arbitrary files off the disk.

That home-and-temp confinement, applied to `source_dir` and `page-local`, was the whole allowlist until 0.7.2. **`MOKURO_BRIDGE_INGEST_ROOTS` widens it** with comma-separated absolute paths, for the common case of a library that is not where the bridge assumes: a second drive on Windows, say, where `C:\tmp` and friends resolve to paths that never match. A relative entry is ignored rather than resolved, because that would anchor the gate to whatever directory the server was started in.

## The update check

`update.py` compares the running version against the latest release, from a cache that is refreshed at most every six hours in the background. A failure is cached too, so an offline machine does not retry in a loop, and `MOKURO_BRIDGE_UPDATE_CHECK=0` turns it off. `/health` reports `update_check`, `latest_version`, `update_available`, `update_url` and `update_error`, and never performs network I/O itself.

Nothing is updated automatically. `--check-update` reports and prints the right command for how this copy was installed (git, pipx or pip), and applying it stays the user's call, because a restart mid-OCR loses work.

## Module map

| Module | Lines | Owns |
| --- | --- | --- |
| `api.py` | ~3290 | the FastAPI app: routes, sessions, ingest, upload, health, the progress stream and the live upload snapshot |
| `web/dashboard.html` | ~1950 | the dashboard page: self-contained inline CSS and JavaScript, theme switch, destination cards, account dialogs and preferences |
| `ocr.py` | ~1290 | the OCR worker pool, queue, fair scheduling and the engine abstraction |
| `config_api.py` | ~1120 | the dashboard's JSON API and browser account setup: `/config`, folder browsing, settings, the MEGA, Drive, OneDrive and WebDAV sign-in routes, rename and removal |
| `cli.py` | ~815 | argument parsing, the two servers, `--status` dispatch, signal handling and startup |
| `providers/drive.py` | ~800 | Google Drive, including OAuth setup and upload sessions |
| `providers/mega.py` | ~775 | MEGA, via megatools |
| `providers/webdav.py` | ~760 | WebDAV: URL and XML handling, Basic auth, and the upload, listing and download calls over plain HTTP |
| `providers/onedrive.py` | ~750 | OneDrive, including the device-code setup flow |
| `providers/rclone.py` | ~480 | rclone: shells out to a configured remote, so OneDrive works without an Azure account |
| `status_tui.py` | ~720 | the live terminal status display: upload bar, width-safe frame, help and dashboard overlays |
| `accounts.py` | ~640 | the account registry and alias model: validation, collisions, suggestions, rename and legacy derivation |
| `series_json.py` | ~560 | the published shape of the reader's `series.json`: its validators, merge rules and wire order |
| `providers/__init__.py` | ~520 | the upload-target registry and dispatch, including the sticky default |
| `sessions.py` | ~420 | session state, persistence, snapshots and page identity |
| `update.py` | ~415 | the version check |
| `fetchproxy.py` | ~380 | the page-fetch accelerator |
| `settings.py` | ~320 | the settings file the dashboard writes, with environment variables winning and environment-pinned keys skipped on save |
| `creds.py` | ~295 | keychain and file credential storage |
| `config.py` | ~275 | paths, environment knobs and the series title helper |
| `series_reference.py` | ~260 | the pin on the upstream files that define the format |
| `ocr_folder.py` | ~230 | the standalone OCR-a-folder command |
| `util.py`, `log.py` | small | shared helpers and logging |

`series_json.py` is a port, not an invention: the format belongs to Gnathonic's mokuro-reader, which is GPL-3.0, so nothing upstream is vendored. `series_reference.py` holds the commit and per-file hashes that make the port auditable and the drift detectable. See [series-json.md](series-json.md).

`api.py` being one large module is the main thing I would change given more time. Its seams are already visible (ingest, upload, health, the progress stream) and each is independently testable, so splitting it is mechanical rather than risky.

## Testing

`python3 -m pytest` runs the full suite in about 11 seconds. It is hermetic: no network, no real credentials, no OCR model.

- `test_imports.py` walks the AST of every module and fails on any import outside the base dependencies, which is how "the engine and the cloud SDKs must not be base dependencies" is kept true.
- `test_optional_ocr.py` blocks `mokuro` on `sys.meta_path` in a subprocess and re-imports there, so a base install is genuinely exercised, and it checks `/health` and the fetch accelerator still answer. That is why there is no skip marker for the optional engine.
- `test_fetchproxy.py` runs a real `http.server` stub CDN through the app and asserts the exact upstream path and query, header handling, and the allow-list guard.
- `test_endpoints.py` covers `/session/start`, `/session/resume`, the per-page routes, `/session/{id}/status` and `/destination/check`, including that a provider failure does not echo its signed URL. The `/session/resume` case exists because 0.6.0 shipped a version that answered 500 on every call, from a call to a helper that was never imported.
- `test_finalize_series_json.py` drives the finalize stream end to end with the OCR engine stubbed: a local finalize leaves a `series.json` beside the volume, a second volume is appended without disturbing the first, an unchanged manifest is not rewritten, and a manifest the bridge could not read is never overwritten. It is the regression lock for the release that shipped without the index.
- `test_series_json.py` and `test_series_reference.py` cover the format port and the upstream pin. The parity fixture they read was generated by probing the pinned upstream implementation in Node, and the format's own golden example is asserted byte for byte.
- `test_config_api.py` covers the dashboard and its API: the page renders with the version substituted and is read from disk per request, listings stay inside the allowed local roots, a provider failure is redacted, settings round-trip and reject bad values without writing, environment-pinned keys are skipped, an unconfigured destination cannot become the default, and every write is refused from a foreign origin.
- `test_settings.py` covers the settings file: precedence (environment, then file, then default), typing and range validation, pinned keys not being overwritten, path and absolute-root validation, clearing a key, private permissions, and a corrupt file being ignored rather than fatal.
- `test_status_tui.py` renders frames headlessly at several widths and heights, checks the narrow layout, the upload bar, the help overlay and that there is no escape byte when colour is off, and exercises the runner with a stubbed fetch and key source instead of a real TTY. `test_status_upload_health.py` covers the `/health` upload snapshot itself.
- `test_ocr_scheduling.py` covers the fair selector and `GET /queue`; `test_accounts.py`, `test_upload_methods.py` and `test_account_setup_api.py` cover the alias model, the registry and default selection, and the browser setup routes with mocked provider seams.
- `test_webdav_provider.py` drives the WebDAV provider against `tools/dav_server.py`: MKCOL folder creation, PUT with progress, the shared fail/skip/overwrite policy, PROPFIND listings, download, the credential store and the `/config/accounts/webdav` route.
- `test_packaging.py` holds the packaging contract: `pyproject.toml` and `requirements*.txt` cannot drift apart, the wheel's package list matches the directories that exist, the pinned provenance is shipped as package data, and the console scripts resolve.

`ruff check .` is clean and configured in-tree, so the lint result is reproducible rather than depending on whatever config happens to be discovered.

## Known limits

- **Unauthenticated and loopback-only.** See above. This is the one to read before running it anywhere but your own machine.
- **Zero-page sessions** and OCR that outlives its client are not bounded by a deadline in every path, so a wedged finalize can hold a worker slot longer than it should.
- **No cloud provider is exercised against a real service.** The account setup routes are tested with mocked seams (`test_account_setup_api.py`) and the finalize provider seam is faked in `test_finalize_series_json.py`, so MEGA, Drive and OneDrive network behaviour, and the megatools invocation in particular, stay unverified by the suite. WebDAV is the exception: `test_webdav_provider.py` runs the provider against `tools/dav_server.py`, a stdlib-only WebDAV server in this repository, over a real socket.
- **The OCR engine lags Python.** PyTorch wheels often trail new releases, so the newest Python may not work yet.
