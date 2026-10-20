# How mokuro-bridge is put together

This is the maintainer's view: what the process is, how a volume moves through it, and where the seams are. For what the service does and how to run it, read the [README](../README.md). The HTTP contract is [endpoints.md](endpoints.md), the client-facing protocol is [clients.md](clients.md) and [integrating.md](integrating.md), and the destination model is [destinations.md](destinations.md).

## The service

mokuro-bridge is one Python process running a FastAPI application on uvicorn, bound to loopback by default. It owns no storefront account, cookie or store protocol: the work begins with a page image and ends with a delivered volume. That boundary is deliberate, because it means a store redesign cannot break OCR or delivery, and any program that can produce an image can use the service.

The base install is small (`fastapi`, `uvicorn`, `python-multipart`, `httpx`, `keyring`). The OCR engine is optional because it brings PyTorch, and each native cloud provider has its own optional dependency. All of them are detected at runtime, so a base install still starts, answers `/health` and accepts capture; only OCR or the affected destination is unavailable.

The application has twelve capture routes in `api.py` and the dashboard's configuration API in `config_api.py`. Both are mounted on the same app, so a client sees one origin and one port.

## The request path and its four lanes

Every blocking call the routes need runs on one of four bounded thread pools in `lanes.py`. They exist because the original design used the event loop's single shared executor, and a batch of finalizing volumes filled it with long-lived work: threads polling OCR, threads waiting on a global assembly lock, threads uploading for minutes. `/health` and `/session/start` then queued behind them and a client timed out.

| Lane | Work it carries | Default workers |
| --- | --- | --- |
| `control` | Short bookkeeping on the request path: session lookup, destination resolution, the upload-method registry | 4 |
| `data` | Page bytes: writes, hashing, copies and source-directory sync | 8 |
| `finalize` | Per-volume assembly and packing, which are CPU-bound and can take seconds | 4 |
| `upload` | Provider uploads, the early cover push and the series manifest, the longest-running work | 8 |

Each lane is a plain `ThreadPoolExecutor`, overridable with `MOKURO_BRIDGE_CONTROL_THREADS`, `MOKURO_BRIDGE_DATA_THREADS`, `MOKURO_BRIDGE_FINALIZE_THREADS` and `MOKURO_BRIDGE_UPLOAD_THREADS`. `/health` reports `lanes` with each pool's worker count and backlog, so a stall names the lane that is behind instead of appearing as one slow route. The single OCR worker thread is a separate dedicated lane, not an executor.

## Sessions and their lifecycle

A session is one volume in progress. It has a session id, the title the client supplied, a sanitized title that becomes the working directory name, and a working directory under the work dir (`~/mokuro-input` by default, `MOKURO_BRIDGE_WORK_DIR`). The last segment of a volume title is not always unique, so the bridge asks whether the target directory is taken before it creates it; when it is, the volume keeps the clean title with a short random suffix such as `_a1b2c3`.

A session carries the received pages, the pages OCR has completed, the pages that failed, a per-page reservation set for in-flight ingests, and the mapping from page number to filename. The page number is the identity: a client that changes its naming scheme mid-volume cannot silently double the page count, because re-sending page 12 under a new name replaces the file the old name held.

Session state is persisted as one JSON file per session under `<work dir>/.bridge_sessions`, written atomically with private permissions. `GET /sessions` restores persisted records on demand, so a bridge restart does not lose a volume in flight, and a client can reconnect to what it had started. A finished session is removed from memory and from disk after its state is final.

The lifecycle is:

1. `POST /session/start` creates a session, or `POST /session/resume` reopens one for a folder of pages already on disk and queues the pages whose OCR cache is missing. Both accept a title; the resume route also accepts a `source_dir` for the folder hand-off.
2. `POST /session/{id}/page` stores one uploaded image and queues it for OCR, and `POST /session/{id}/page-local` copies one image from a path on this machine. Both are idempotent for identical bytes and refuse conflicting ones.
3. `POST /session/{id}/cover` attaches a cover early, so a destination that is slow to accept the cover has it before the volume is ready.
4. `POST /session/{id}/finalize` owns the rest: it waits for OCR, assembles, packs, publishes the index, delivers, and reports every step on an NDJSON stream. The route takes a non-blocking cross-process lease per session, so two finalizes for one volume cannot both run; the loser answers `409`.
5. After a successful finalize the working files are removed when `delete_after_upload` is true, and the session is finalized and cleaned up. A failed post-commit step marks the session retryable rather than leaving a finalized ghost.

`GET /session/{id}/status` returns the same snapshot as `GET /sessions`. The snapshot carries `ingesting` and `finalizing` beside the counters, so a client can distinguish pages still arriving from the volume being packaged without inferring it from a counter that stopped moving.

## The OCR worker and its cache

`ocr.py` holds one OCR worker thread, a queue and an engine abstraction. Pages are grouped into chunks (`OCR_CHUNK_SIZE`, default 8) and flushed early after an idle interval (`OCR_IDLE_FLUSH_S`, default 1.5 s). The worker takes up to `OCR_BATCH_SIZE` pages per pass, defaulting to `max(OCR_CHUNK_SIZE, 32)`, so the engine stays busy across volumes. With several volumes queued, the fair selector gives each active session a turn and caps finalize work at a share of a mixed batch (`MOKURO_BRIDGE_OCR_FINALIZE_PRIORITY`, default 0.5), so one volume finishing does not starve the others. Setting `MOKURO_BRIDGE_OCR_FAIR_SCHEDULING=0` restores the historical first-in-first-out selector. `GET /queue` exposes the resulting depths, worker state and per-session counts.

OCR results are cached per page, as JSON under `<work dir>/_ocr/<volume>/<page>.json`. The cache is keyed to the page's bytes: a page whose bytes changed loses its cached result, and the cache is validated before assembly instead of being trusted. That cache is why re-sending a page, resuming a volume or finalizing after a crash does not redo recognition. Deleting the work volume's `_ocr` directory is the supported way to force recognition again.

The engine is detected at runtime, not from the version string. The bridge supports upstream `mokuro` from PyPI and the GolyBidoof fork, which exposes a batched recognition path; `/health` reports `mokuro_flavor` (`upstream`, `fork`, `absent` or `unknown`) and `mokuro_fork_api`. The fork's extra idle pipeline processes are disabled by default (`MOKURO_BRIDGE_OCR_WORKERS=0`), leaving the one worker this service manages.

## Assembly and packing

Finalize waits for the OCR queue to drain, reports `wait_ocr` frames while it does, and then assembles the volume from the cached per-page results. Assembly produces the `.mokuro` file through the loaded engine's generator. Packing produces the `.cbz` from the page images and the `.webp` cover: the cover is the first `.webp` page when the client sent WebP, otherwise the first image, normalized and re-encoded. A local cover that was already pushed through the cover route is reused when its digest still matches, and a remote cover upload is skipped when the early cover push already delivered it.

Packing writes into a private temporary directory inside the destination staging area and then moves the finished files into place. A remote destination stages under `<volume>/_mega_upload`; a local destination stages directly in `<output>/<series>/`. Publishing is all or nothing per file: an existing file is backed up, replaced atomically where the filesystem allows it, and the previous copy is restored if a later step fails. A packaging run that produced an incomplete artifact fails the finalize instead of delivering a partial volume.

The series folder name is derived from the volume title by stripping edition and imprint tags, so `サンプル作品（２）【電子限定特典付き】` and `サンプル作品 1巻` share one folder. The volume files keep the full volume title. `series.json` is written with the volume files for a local destination and after them for a remote one, and a destination managed by mokuro-bunko receives none of it; see [series-json.md](series-json.md) and [bunko.md](bunko.md).

## The finalize stream

`POST /session/{id}/finalize` answers `application/x-ndjson`, with response buffering disabled so frames arrive as they happen. Each frame is one JSON object with a `stage`, a human `message` and stage-specific fields. The order a client sees is:

| Stage | When | What it means |
| --- | --- | --- |
| `wait_ocr` | While the queue drains, at least every 750 ms | `pages_received`, `pages_ocr_done`, `pages_ocr_pending` and `pages_ocr_failed`; the first frame also carries `ocr_chunk_size` |
| `assemble` | Once OCR is complete | The `.mokuro` is being generated from the cached results |
| `pack` | After assembly | The archive and cover are being written |
| `series_json` | While the index is published | `ok` and `unchanged` on success, `ok: false` with the reason on failure, and `external: true` when mokuro-bunko owns the manifest |
| `upload` | Per file, before and after transfer | The file being uploaded; the batch frame also names every file and its size before any bytes move |
| `upload_progress` | During a remote transfer, throttled to about four per second | `file`, `bytes`, `total_bytes`, `current_bytes`, `percent`, `speed_bps` and `speed_human` |
| `cleanup` | When the working files are removed | The volume has been delivered and is being cleaned up |
| `done` | Terminal, success or partial | The final record: title, series, pages, method, paths, reader URL and the per-file `uploads` array |
| `error` | Terminal | A failure status such as `ocr_failed`, `ocr_cache_invalid`, `persistence_error`, `finalize_error` or `cleanup_error` |

A `done` frame with `status: "partial_upload"` means the volume files reached the destination but the index did not, which a client should surface rather than report as success. A stream that stops without a terminal frame is a truncated upload. The exact per-file schema is in [endpoints.md](endpoints.md); the client-side rules are in [integrating.md](integrating.md).

## The provider layer

`providers/` holds one module per backend: MEGA through megatools, Google Drive, OneDrive and WebDAV natively, and rclone for everything else. Each module exports a frozen `Provider` value with the same capabilities: `configured`, `creds_source`, `upload`, `remote_exists`, `download`, `list_folders`, `run_setup`, `account` and an optional `metadata_owner`. `providers/__init__.py` resolves a method id to an account, looks the provider up in a registry and dispatches. Accounts, aliases and default roots stay in `accounts.py`; a provider never enumerates accounts or parses ids. A browser setup signs the four services in through rclone, so `rclone_auth.py` owns the MEGA and WebDAV form flows and the Drive and OneDrive consent-tab flows, and the resulting account is an rclone account tagged with the service the user picked; the native backends remain for accounts configured by the terminal wizards and for existing installs. The full contract, the account view and the one-folder walk are in [destinations.md](destinations.md).

The overwrite policy is part of the contract: `fail` reports an existing file, `skip` treats it as success, `overwrite` updates in place where the backend can and otherwise keeps both copies (MEGA uploads a versioned sibling), and `replace` always ends with one file under that name. The series manifest uses `replace`, because a manifest renamed to a sibling is a manifest no reader reads.

### The rclone loopback WebDAV gateway

rclone re-authenticates on every invocation, which is a problem for an account with two-factor sign-in, and mokuro-bunko has no rclone backend at all. The bridge therefore runs one `rclone serve webdav` per rclone account on `127.0.0.1`, on an ephemeral port, with a random Basic-auth pair. The bridge's own rclone transfers go through that endpoint, and mokuro-bunko is configured with the same URL, so one authenticated rclone session and one view of the folder serve both processes.

The gateway's records live beside the Bunko data, in `~/Library/Application Support/mokuro-bridge/rclone` on macOS, `%LOCALAPPDATA%\mokuro-bridge\rclone` on Windows or `~/.local/share/mokuro-bridge/rclone` elsewhere (`MOKURO_BRIDGE_RCLONE_DIR`): `gateways.json` with one 0600 record per account (port, loopback username and password, pid), a bridge-owned `rclone.conf` holding the remotes that point at those ports, and one log per account. The recorded port and password survive a bridge restart so Bunko's saved URL keeps working. A gateway started in its own session also outlives the bridge. `MOKURO_BRIDGE_RCLONE_GATEWAY=0` disables the gateway; every transfer then talks to the cloud remote directly, which is the fallback when `rclone serve` cannot start.

## The dashboard and the terminal display

The configuration dashboard is served at `GET /` from `web/dashboard.html`, read from disk on every request with `Cache-Control: no-store`. It is one self-contained page: inline CSS and JavaScript, inline SVG icons, no external asset, no CDN and no font download, so it works offline and renders unchanged on a machine with no network. It reads live state from `/health`, configuration from `/config` and the optional engines from `/config/engines`, and it edits destinations, settings and engine installs through the `config_api.py` routes.

Configuration writes are gated to a loopback `Origin`, because the service is unauthenticated and any page in a browser can reach it. A request with a loopback `Origin`, or with no `Origin` at all (curl and the CLI), is accepted; anything else is refused with `403`. The Google Drive OAuth callback is the one exception, because it is a `GET` opened by Google's redirect, and it accepts only the one-time `state` this bridge created. Reads are not gated. No response ever returns a credential, token or client secret.

The terminal display in `status_tui.py` draws the same picture from `/health` for a service running under launchd or systemd: engine and tools, destinations with the default marked, the OCR queue, the worker and pipeline state, the fetch-proxy ports, the current activity and a live upload progress bar. It re-reads the terminal size every frame, measures display width with east-asian rules, honours `NO_COLOR` and `TERM=dumb`, and prints one full-height frame with no escape sequences when piped. [dashboard.md](dashboard.md) documents both surfaces in detail.

## The health snapshot

`/health` is the call every client makes first, and it used to do real work on the request path: walk the OCR queue, take session locks, rebuild the destination registry and ask the keychain. Now one dedicated thread refreshes a snapshot every second, and the route returns the latest snapshot plus the cheap live fields. The response carries `health_snapshot` with `age_ms`, `interval_ms`, `stale` (true past five seconds) and `partial`, so a client can see how fresh the answer is instead of guessing. `/health` itself never performs network I/O.

The snapshot is also where the capability block lives: `fetchProxyPorts`, `fetchUpstreams`, `fetchPathUpstream`, `upload_methods`, `upload_method_default`, `lanes` and the OCR counters. A client should read capabilities from there rather than assume them.

## The page-fetch accelerator

The bridge can open extra loopback ports that proxy page fetches for a browser client. A browser allows about six concurrent HTTP/1.1 connections per origin, and the port is part of the origin, so each extra port is another six lanes while the bridge fetches under no browser limit. Ports are advertised on `/health` as `fetchProxyPorts`, which is how a client discovers them; nothing needs configuring. The proxy is started by `cli.py` when `MOKURO_BRIDGE_FETCH_PORTS` is non-zero, needs no OCR engine, and is optional: capture never depends on it. [browser-acceleration.md](browser-acceleration.md) covers the browser limits, the file-descriptor cost and when to change the count.

## The update check

`update.py` compares the running version against the latest release from a cache refreshed in the background at most every six hours. A failed check is cached too, so an offline machine does not retry in a loop, and `MOKURO_BRIDGE_UPDATE_CHECK=0` turns it off. Nothing installs or restarts automatically, because a restart mid-OCR loses work. `/health` reports `update_check`, `latest_version`, `update_available`, `update_url` and `update_error`.

## Security posture

Stated plainly, because it is a design decision rather than an oversight:

- The service is unauthenticated. It binds to `127.0.0.1` by default (`MOKURO_BRIDGE_HOST`), and that loopback binding is the entire access control. Binding it to a LAN address means anyone who can reach the port can start a volume, feed it pages and have the result written or uploaded.
- Configuration writes are gated to a loopback `Origin`, as described above.
- CORS defaults to `*`, deliberately: a browser client runs on whichever page the user is reading, and a pinned list would silently break a new one. `CORS_ORIGINS` narrows it.
- Local ingest is confined to the user's home and the platform temp locations. `MOKURO_BRIDGE_INGEST_ROOTS` adds absolute paths, for a library on another drive. Page names are validated, symlinks are refused and uploads are size-limited before they are read, so a caller can drive the service but cannot read arbitrary files off the disk.
- The fetch proxy refuses targets that resolve to loopback, private, link-local, multicast or reserved addresses, so it cannot be pointed at the LAN or a cloud metadata endpoint.

## Testing

`python3 -m pytest` runs a hermetic suite: no network, no real credentials and no OCR model. The parts that matter for changes here:

- `test_endpoints.py` drives the real ASGI app for the session lifecycle, page ingest, the resume hand-off and destination checks, including that a provider failure never echoes its signed URL.
- `test_finalize_series_json.py` drives the finalize stream end to end with the engine stubbed: a local finalize writes the index, a second volume merges into it, an unchanged manifest is not rewritten, an unreadable one is never overwritten, a bunko destination uploads three files and reports the index as external, and the terminal frames carry the documented per-file fields.
- `test_bunko.py` and `test_bunko_agent.py` cover the marker rules, the connect payload, the setup conversation and the restart tolerance of the control client.
- `test_ocr_scheduling.py`, `test_optional_ocr.py` and `test_ocr_folder.py` cover the queue, the fair selector and the no-engine case.
- `test_rclone_gateway.py` runs a real `rclone serve webdav` against a local remote and proves the bridge and Bunko share one endpoint.
- `test_config_api.py`, `test_settings.py` and `test_status_tui.py` cover the dashboard API, the settings file and the terminal display.
- `test_packaging.py` keeps `pyproject.toml`, the requirements files and the wheel contents in step.

`ruff check .` is clean and configured in-tree, so the lint result does not depend on whatever configuration happens to be installed.

## Module map

| Module | Owns |
| --- | --- |
| `api.py` | The FastAPI app: capture routes, session lifecycle, ingest, finalize stream, health, upload snapshot |
| `web/dashboard.html` | The dashboard page: inline CSS and JavaScript, theme switch, destination cards, account dialogs, preferences |
| `ocr.py` | The OCR worker, queue, fair scheduling, page cache and the engine abstraction |
| `config_api.py` | The dashboard's JSON API: configuration, folder browsing, settings and browser account setup |
| `cli.py` | Argument parsing, the service entry point, the status display dispatch and startup |
| `providers/` | One module per destination backend, the registry and dispatch, and the rclone gateway |
| `sessions.py` | Session state, persistence, snapshots and page identity |
| `accounts.py` | The account registry and alias model |
| `series_json.py` | The published shape of the reader's `series.json`: validators, merge rules and wire order |
| `series_reference.py` | The pin on the upstream files that define that format |
| `rclone_gateway.py`, `rclone_auth.py` | The shared loopback gateway and browser-driven rclone account setup |
| `lanes.py` | The four bounded execution lanes and their stats |
| `fetchproxy.py` | The page-fetch accelerator |
| `status_tui.py` | The live terminal status display |
| `settings.py`, `creds.py`, `config.py` | Settings precedence, credential storage and paths |
| `update.py`, `mokuro_flavor.py`, `bunko_agent.py` | Update check, engine provenance and the Bunko installer and supervisor |
| `ocr_folder.py` | The standalone OCR-a-folder command |

Its age is the reason `api.py` is one large module. The seams are already visible (ingest, finalize, upload, health, the progress stream) and each is independently testable, so splitting it is mechanical rather than risky.

## Known limits

- The service is unauthenticated and loopback-only. Read that before running it anywhere but your own machine.
- Zero-page sessions and OCR that outlives its client are not bounded by a deadline in every path, so a wedged finalize can hold a worker slot longer than it should.
- No native cloud provider is exercised against a real service in the suite. The account setup routes and the finalize provider seam are tested with fakes; WebDAV is the exception, because `test_webdav_provider.py` runs the provider against the repository's stdlib-only test server in `tools/dav_server.py` over a real socket. The rclone gateway is tested against a real rclone binary and a local remote.
- The OCR engine lags Python: PyTorch wheels often trail a new release, so the newest Python may not work yet.
