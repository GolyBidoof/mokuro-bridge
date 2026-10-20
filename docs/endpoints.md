# HTTP endpoints

The bridge exposes twelve capture routes plus the dashboard's configuration API, all on the same loopback server (default `http://127.0.0.1:62642`). There is no authentication: binding to `127.0.0.1` is the entire access control, and every route is meant to be reachable only from the machine that runs it. Request bodies are `application/x-www-form-urlencoded` except where a file upload or JSON body is noted. The upload body limit is 100 MiB for pages and covers.

The routes the capture loop needs are `POST /session/start`, `POST /session/{id}/page` (or `POST /session/{id}/page-local`) and `POST /session/{id}/finalize`. Everything else exists so a client does not have to guess or repeat work. [clients.md](clients.md) is the protocol overview and [integrating.md](integrating.md) is the integration guide for a new client.

## The session snapshot

Several routes return the same snapshot, produced by `sessions.session_snapshot()`. It carries counts and state flags so a client can tell "pages are still arriving" from "the volume is being packaged" without inferring it from a counter that has stopped moving.

| Field | Meaning |
| --- | --- |
| `session_id`, `title`, `safe_title` | Identity of the session and the filesystem-safe volume name |
| `pages_received` | Pages accepted so far |
| `pages_ocr_done` | Pages whose OCR result is cached |
| `pages_ocr_failed` | Pages whose OCR failed |
| `pages_ocr_pending` | Queued or in-flight pages, including reserved ingests |
| `pages_ingesting` | Filenames currently reserved by an ingest |
| `source_ingesting` | True while a source directory is being synced, with no per-page reservation |
| `ingesting` | True while a source directory is being synced or a page is being ingested |
| `finalizing` | True from the moment finalize takes its exclusive lease |
| `message` | Human-readable progress line |
| `finalized` | True once the session reached its terminal state |
| `upload` | Live upload progress during finalize, or null |
| `cover_uploaded` | Metadata for an early cover upload, or null (never a share URL) |

## POST /session/start

Creates a session and its volume working directory. This is the first call of a streaming capture.

| Form field | Default | Meaning |
| --- | --- | --- |
| `title` | `manga` | Volume title; the series folder and artifact names derive from it |
| `reuse_existing` | `false` | When truthy (`1`, `true`, `yes` or `on`), pick up an existing volume with the same safe title instead of making a new one |

Response `200` is the new session record: `session_id`, `title`, `safe_title`, `vol_dir`, `message`, `ocr_chunk_size`, `reused`, `pages_received` and `pages_ocr_done`. A request that reuses a live session answers with the full session snapshot instead, plus `reused: true` and `vol_dir`.

Errors: `400` when the title is reserved or invalid (reserved names include `_ocr`, `output`, `local_dir_default.json` and `upload_method_default.json`), `400` when reusing a session that is already finalized, finalizing, cover-uploading or ingesting, and `409` when another request created the same title concurrently.

A fresh volume keeps the clean title. The name gets a short random suffix (`<volume>_a1b2c3`) only when that directory genuinely already exists, so a later `reuse_existing` lookup still finds it by the plain title. Covered by `tests/test_endpoints.py`.

## POST /session/resume

Hands over a folder of pages that already exist on disk. A same-machine client uses this instead of uploading bytes, so the pages never cross a socket. With no `source_dir` it simply reopens the volume as a session.

| Form field | Required | Meaning |
| --- | --- | --- |
| `title` | yes | Volume title |
| `source_dir` | no | Folder of page images to sync into the volume and queue for OCR |

Response `200` adds `vol_dir`, `synced_from_source`, `queued_for_ocr`, `ocr_cached` and `resumed: true` to the snapshot. An explicit resume clears the session's earlier per-page OCR failures, so a repaired engine or dependency requeues those pages, and it queues only pages whose OCR cache is missing.

Errors: `400` for a reserved or invalid title, `403` when `source_dir` resolves outside the allowed ingest roots (home directory, the system temp locations, plus `MOKURO_BRIDGE_INGEST_ROOTS`), `400` when the source is not a directory or a source page is a symlink or its destination is unsafe, and `409` when a source filename collides on stem with another extension or another request created the same title concurrently. Covered by `tests/test_endpoints.py`.

## POST /session/{session_id}/page

Streams one captured page into the session and queues it for OCR immediately. The request is multipart.

| Form field | Required | Meaning |
| --- | --- | --- |
| `page` | yes | The image file |
| `filename` | yes | Destination name; must be a plain filename with an image extension |
| `page_num` | no | Advisory page identity, used to supersede an older copy of the same page |

Response `200` is the snapshot plus `filename` and `page_num`. Re-sending the exact bytes the bridge already holds is an idempotent retry: the response carries `already_present: true` and the queued OCR item is left alone.

Errors: `400` when the session is finalizing or finalized, when `filename` is not a plain image filename, when the bytes are not a recognized image, when the destination is a symlink, or when the OCR cache path is unsafe; `404` for an unknown session; `409` when the page is currently being OCR'd, is currently being ingested, collides on stem with another extension, already has completed OCR with different bytes, or is already queued or present; `413` when the image exceeds 100 MiB. Covered by `tests/test_endpoints.py`.

## POST /session/{session_id}/page-local

Same-machine ingest: copies a page from a path on this machine instead of accepting bytes.

| Form field | Required | Meaning |
| --- | --- | --- |
| `path` | yes | Source file path, under an allowed root |
| `filename` | yes | Destination name in the volume |
| `page_num` | no | Advisory page identity |

Response `200` is the snapshot plus `filename`, `page_num`, `source` and `copied`.

Errors: `403` when the path is outside the allowed roots, `400` when the source is a symlink, is not a file, has an unsupported extension, is not a recognized image, or when the session is finalizing or the destination is unsafe; `409` for the same per-page conflicts as `POST /page`; `413` when the source exceeds 100 MiB. Not covered by an HTTP test; the ingest guards are exercised indirectly through `/session/resume`.

## POST /session/{session_id}/cover

Pushes the cover `.webp` early, before OCR finishes, so the destination receives something immediately and the client can show upload progress. The request is multipart.

| Form field | Required | Meaning |
| --- | --- | --- |
| `cover` | yes | The cover image; normalized to WebP |
| `upload_method` | no | Destination for this cover; unset uses the current default |
| `local_dir` | no | Output folder when the method resolves to `local` |

Response `200` is JSON, not a stream: `{ok, method, file, size, sha256}`, with `path` plus a null `remote_path` added for a local destination and `remote_path` plus `url` added (and no `path`) for a remote one. A later finalize to the same destination and matching digest skips re-uploading the cover.

Errors: `400` when the session is busy, the method is unknown, the cover is not a recognized image, or the local directory is unsafe or overlaps working state; `404` for an unknown session; `502` when the provider upload fails; `500` when the written or uploaded cover cannot be verified or its metadata cannot be persisted. Not covered by an HTTP test.

## GET /sessions

Lists the live sessions, restoring persisted ones from disk first so a client that reconnects after a restart can still see work it had started. Response `200` is `{sessions: [snapshot, ...], count}`. Already-live sessions are served from memory rather than re-read from JSON on every poll. Not covered by an HTTP test.

## GET /session/{session_id}/status

Polls one session. Response `200` is the snapshot. `404` for an unknown session, and `400` if a finalized session record is still on disk. Covered by `tests/test_endpoints.py`.

## POST /session/{session_id}/finalize

Waits for the OCR queue, assembles the `.mokuro`, packs the CBZ and cover, publishes the series `series.json`, then keeps the output locally or uploads it. The response is `application/x-ndjson`: one JSON frame per event, at least every 750 ms while the queue drains and one per file during an upload, with no caching and no proxy buffering.

| Form field | Default | Meaning |
| --- | --- | --- |
| `upload_method` | current default | An account alias such as `mega-work` or `drive-next`, or `local`. Historical spellings (`mega`, `mega:work`, `drive:main`, `provider:default`) keep resolving to the same account |
| `upload_to_mega` | empty | Legacy alias: `true` means MEGA, `false` means local |
| `delete_after_upload` | `true` | Remove the volume's working files once every step succeeded |
| `local_dir` | empty | Output folder when the resolved method is `local`; remembered as the sticky default |
| `overwrite` | `fail` | Collision policy for the volume files: `fail`, `skip` or `overwrite`; `overwrite` replaces in place except on MEGA, where the new copy is uploaded under a versioned sibling name and the previous file survives. `series.json` is always updated in place, see [series-json.md](series-json.md) |

Frames carry a `stage` and a `message`: `wait_ocr`, `assemble`, `pack`, `series_json`, `upload`, `upload_progress`, `cleanup`, then a terminal `done` or `error`. A local finalize emits no `upload` or `upload_progress` frames: its first `pack` frame announces packaging and a second one carries `output_dir`, `series` and the final `files` list. A `wait_ocr` frame carries the live counters `pages_received`, `pages_ocr_done`, `pages_ocr_pending` and `pages_ocr_failed`, and the first one also carries `ocr_chunk_size`; those are the only place a client can read the failure count while the stream is still running, because the terminal `error` frame reports it only when OCR actually aborts. `assemble` and the first `pack` frame carry a message only. A remote finalize sends one `upload` frame that plans the batch (`files` with `file` and `total_bytes`, plus `series`, `remote_path` and `method`) before any bytes move, then `upload_progress` frames (`upload` holds `file`, `bytes`, `total_bytes`, `current_bytes`, `percent`, `speed_bps` and `speed_human`, and the legacy top-level `mega_path` mirrors `remote_path`), then one `upload` frame per file carrying `file`, `method` and, on success, `url`. A `series_json` frame is either a result (`ok: true`, `ok: true` with `unchanged: true` when the destination already described this volume, or `ok: false` with a message when it could not be written) or, for a remote destination that does not own the manifest, an announcement before its upload carrying only `file` and `remote_path`. The destination that owns the manifest, mokuro-bunko, gets one frame with `ok: true` and `external: true`; the bridge never reads, merges or uploads that destination's `series.json`. A failed write leaves the destination's existing file untouched.

A `done` frame has `status: "success"` or `status: "partial_upload"` and both statuses carry the same fields: `title`, `series`, `pages`, `pages_ocr_done`, `method`, `remote_path`, `output_dir`, `staging`, `reader_url`, the legacy `mega_path` mirror of `remote_path` for MEGA, and a per-file `uploads` array. Every `uploads` entry, for a local and for a remote destination alike, carries `file`, `success`, `size` (the file's byte count), `url`, `stderr` and `duration_s`; a remote entry adds `bytes`, `total_bytes`, `current_bytes`, `percent` and `speed_bps`, and a local entry adds `path` and `unchanged`. `size` is the field clients total, so it is present everywhere. What the array lists depends on the destination: a local finalize reports one entry for `series.json`, because the volume's `.webp`, `.cbz` and `.mokuro` were written by the pack step and are named in the second `pack` frame's `files` list; a remote finalize that publishes the manifest reports the three volume files and then `series.json`; a mokuro-bunko destination compiles `series.json` itself, so its `done.uploads` has three entries and the stream carries a `series_json` frame with `external: true` instead, see [bunko.md](bunko.md). An `error` frame is terminal and must be raised by the client: `status` names the failure when the bridge knows one, with values including `ocr_failed`, `ocr_cache_invalid`, `page_name_collision`, `page_set_mismatch`, `artifact_invalid`, `too_few_pages`, `invalid_local_output`, `invalid_output_path`, `persistence_error`, `cleanup_error` and `finalize_error`; a few packaging failures carry only a message and no `status`. A stream that simply ends without a terminal frame is a truncated upload, not a success.

A destination that received the volume files but not the index is reported as `partial_upload`, not success: a folder without `series.json` is a series the reader cannot build. The working files are kept in that case, so a retry is cheap.

Errors before the stream starts: `400` for an unknown upload account, `404` for an unknown session, and `409` when a finalization is already in progress for that session. The stream is covered end to end by `tests/test_finalize_series_json.py` for a local folder and for a remote destination with the provider seam faked; the client-side stream parsing is tested in `tests/test_ocr_folder.py`.

## GET /upload-methods

Lists every upload target and the current default. Response `200` is `{upload_method_default, upload_method_selected: null, methods: [...]}`, where each method carries `id` (the account alias, or `local`), `alias` (the same value, present on every row including `local`), `name`, `configured`, `default`, `current_folder`, the generic `provider`, `account`, `creds_source` and `tracked` keys, and provider-specific extras (for an MEGA account, `library_root` and `email`; for a Drive or OneDrive account, `root`; for a WebDAV account, `root`, `url`, `username` and, when set, `insecure`; for an rclone account, `remote` and `root`). `upload_method_default` is an alias, and the historical spellings are still accepted as input. Covered at the route level by `tests/test_endpoints.py` and the registry by `tests/test_upload_methods.py`.

## POST /destination/check

Answers, read-only, whether a finished CBZ is already at a destination, so a client can decide whether to download and OCR a volume at all. It never creates a folder or uploads a byte.

| Form field | Default | Meaning |
| --- | --- | --- |
| `method` | `local` | Destination to check |
| `title` | required | Volume title; a blank one falls back to a generated name, matching `/session/start`, while a reserved or invalid one is a `400` |
| `local_dir` | empty | Base folder for a `local` check |

Response `200` is `{method, title, file, exists}`, plus `remote_dir` for a remote destination. For `local` it stats the finished `.cbz`; for a remote account it walks folders. Errors: `400` for an unknown or unconfigured destination or a reserved or invalid title, `422` when `title` is missing, and `502` when the provider check fails. Provider failures are redacted, because Graph and Drive errors embed a request URL that carries an access token. Covered by `tests/test_endpoints.py`.

## The configuration dashboard

`GET /` serves one self-contained HTML page (inline CSS and JavaScript, no external assets) that shows the bridge's health and edits its destinations, default folders and settings. It is documented for users in [dashboard.md](dashboard.md); the configuration routes that are part of the public contract are here, and the page-internal ones are named at the end of this section.

Every write below is refused with `403` when the request carries an `Origin` header that is not loopback (`127.0.0.1`, `localhost` or `[::1]`, any port). A request with no `Origin` at all, which is what curl and the CLI send, is accepted. Reads are not gated. The Google Drive OAuth callback is the one exception: it is a `GET` opened by Google's redirect, so it is not Origin-gated, and it accepts only the one-time `state` this bridge created.

The table below documents the configuration routes that are part of the public contract: the destination model, the account lifecycle and the settings a client or script may reasonably drive. The bundled page also calls routes that are deliberately internal and versioned with the page rather than promised here: engine install, update, reload and uninstall under `/config/engines*`, the bridge's own updater (`POST /config/update/apply`, `POST /config/update/check`), the mokuro-bunko sync (`POST /config/bunko/sync`), the rclone shortcut cards and consent flows under `/config/rclone/*`, and the page's presentation helpers (`GET /config/folder-link`, `GET /config/account-email`, `GET /config/usage`, `GET /config/rclone/usage`, `GET /config/rclone/verify/status`). They are not forbidden to a script, but they change with the page's layout and the installation model behind it, so they carry no compatibility promise.

### Accounts and aliases

Every upload account has a memorable, globally unique alias: `mega`, `mega-2`, `drive-next`. The alias is the `id` in `/config` and `/health` and the value clients pass as `method`/`upload_method`. Historical spellings keep working forever: a bare provider (`mega`), `mega:work`, `drive:main` and `provider:default` all resolve to the same account, so `upload_method_default.json` and old scripts need no migration. An account added from the page persists its alias in its metadata file; an account configured before aliases existed derives one on read (`mega:work` becomes `mega-work`) without its files being rewritten. Aliases are 1-32 lowercase letters, digits or hyphens, cannot be `local` or another provider's bare name, and are suggested as `<provider>`, `<provider>-2`, `<provider>-3`, skipping anything taken. An unknown target answers `400` listing the known aliases. Passing the alias of an account that already exists re-credentials that account (the dashboard's Reconnect) while keeping its folder, note and default; an alias that belongs to another provider is a `400`.

| Route | Body | Answer |
| --- | --- | --- |
| `GET /` | none | `200` with the page, read from disk on every request with `Cache-Control: no-store`; `500` when the packaged page is missing |
| `GET /config` | none | `200` with `app`, `version`, `host`, `default_method`, `local_output`, `local_output_remembered`, `output_dir`, `work_dir`, `accounts_dir`, `settings_file`, `settings`, `providers`, `methods`, `restart_command`, `platform` and `writable`. Each `providers` row carries `id`, `label`, `available`, `browser_setup`, `setup_hint`, `missing` and `alias_suggestion`, plus on the four native cards `rclone_backed`, `service` and `default_root` when rclone can drive them and `legacy_native` (the inverse), `client_preset` for Drive, `client_id`, `client_id_source`, `client_preset` and `needs_client_id` for OneDrive, and `remotes`, `services`, `recommended` and `default_root` for rclone. Each `methods` row starts with `local` and carries `id`/`alias`, `name`, `configured`, `default`, `provider`, `provider_label`, `account`, `label`, `root`, `creds_source`, `current_folder`, `needs_reauth`, `state`, `tracked`, `service` and a nested `extra` object with the provider extras. `state` is `connected`, `needs_sign_in` or `not_configured` |
| `GET /config/browse` | query: `method`, `path` | `200` with `{path, parent, entries, truncated}` and `home` for a local method or `root` for a remote one. Local browsing lists directories only and only under the ingest roots, so a path outside them is `400`. A remote listing may also carry file entries (`type` is `folder` or `file`, with `size` for a file). A provider failure is `502` with the error redacted |
| `POST /config/account` | `method`, `root`, `label` | `200 {ok, method, root}`; `400` for an unknown provider, a non-portable root or an invalid label; `404` for a named account that does not exist yet, because named accounts are created by the `/config/accounts/*` routes, while the provider's implicit first account is accepted. `method` accepts an alias or any historical spelling; an empty `root` or `label` leaves that field unchanged |
| `POST /config/default` | `method` | `200 {ok, default_method}`; `400` for an unknown target; `404` for an unknown destination; `409` when the destination has no working credentials yet, because an unconfigured account is never made the default |
| `POST /config/local-output` | `path` | `200 {ok, local_output}`; `400` for a path outside the allowed roots. The folder picker only submits a folder it has just listed |
| `POST /config/new-folder` | `parent`, `name` | `200 {ok, path}`; `400` for a nested or hidden name, a name that is not a folder, or a parent outside the local ingest roots. Local destinations only |
| `POST /config/settings` | JSON object of setting name to value, for the settings the page offers (download ports and concurrency, the update check, the OCR batch size, the fallback output folder and extra read roots) | `200 {ok, saved, settings, restart_command}`, or `400 {ok: false, problems}` naming every rejected value, including a setting the page does not offer. Nothing is written when any value is rejected. A key whose environment variable is set and non-empty is skipped before validation and omitted from `saved`, so its stored value can never be clobbered; a path must be absolute, an output folder that is an existing file or has a missing parent is rejected, ingest roots must be absolute, numbers are range-checked per row, and a switch accepts on/off, true/false or 1/0 and is stored as `true` or `false` |
| `POST /config/forget` | `method` (alias or historical id) | `200 {ok, id, removed, notes}`; `400` for `local` or a malformed target. Removes the account's metadata, its secret files and its keychain item, the same work as `--remove-upload`; a default that pointed at it is reset |
| `POST /config/accounts/webdav` | `alias`, `label`, `url`, `username`, `password`, `root`, `insecure` | `200 {ok, method, root, made_default, server, username, discovered, insecure, folder_ready}`. Probes with `PROPFIND` before storing anything; when the pasted address is not itself a WebDAV folder it also tries the well-known paths under it (Nextcloud/ownCloud `remote.php/dav/files/<user>/`, `/dav/`, `/webdav/`), stores the address that answered, and reports `discovered: true`. `insecure` (`on`/`true`) accepts a self-signed certificate for that account only and is kept as non-secret metadata. The upload folder is created at setup (`folder_ready`), the address and username go to the account metadata, and the password to the account's 0600 credential file (plus the OS keychain for the first account). Using an existing account's alias re-credentials it in place and keeps its folder, note and default; an empty field falls back to the stored value. `400` for a bad address, rejected credentials, a web page instead of a WebDAV folder, an untrusted certificate, a missing `requests` library, or an alias owned by another provider; the `detail` is written for a first-time user (for example naming app passwords on a `401`) |
| `POST /config/accounts/mega` | `alias`, `label`, `email`, `password`, `root` | `200 {ok, method, root, made_default}`. Verifies the login with megatools before storing anything in the existing keychain/`credentials.env` store. Using an existing account's alias re-credentials that account. `400` for bad credentials (`MEGA login failed - credentials were not stored`), an alias owned by another provider, missing megatools, or a first account that is already configured outside the page |
| `POST /config/accounts/onedrive/start` | `alias`, `label`, `client_id` | `200 {ok, flow_id, user_code, verification_uri, expires_in, interval_s, client_id, client_id_source, client_id_saved}`; the sign-in runs in a thread and the account is saved when it completes. `client_id` is the one-time Azure Application (client) ID: when posted it is validated as a GUID, stored in `ONEDRIVE_APP_FILE` (0600) and used from then on, so the page can set it without an environment variable or restart. `400` when msal is missing, the alias belongs to another provider, the posted ID is malformed (the detail names the Application (client) ID versus the Object/Directory ID), no ID is configured at all (the detail lists the five portal steps), an app ID pinned by `ONEDRIVE_CLIENT_ID` would be overridden, or Microsoft rejects the app (`AADSTS65001`, `AADSTS700016` and friends are translated into the step they need). Poll the status route with `flow_id` |
| `GET /config/accounts/onedrive/status` | query: `flow_id` | `200 {status, method?, detail?}` where `status` is `pending`, `complete` or `error`; an unknown or expired flow is `status: "error"` |
| `POST /config/accounts/rclone` | `alias`, `label`, `remote`, `root` | `200 {ok, method, remote, root, folder_ready, remotes}`. The remote must be one of `rclone listremotes`; it is probed with `rclone lsf` before anything is stored, and the upload folder is created at setup. Using an existing alias re-points that account and keeps its folder, note and default. `400` when rclone is not installed (the detail names the install command), no remote is configured, the name is unknown (the detail lists the configured ones), or the remote is configured but not usable. rclone keeps the credentials; nothing secret is stored or returned |
| `POST /config/accounts/drive/start` | `alias`, `label`, `client_id`, `client_secret` | `200 {ok, flow_id, auth_url, redirect_uri}`. Reuses `DRIVE_CLIENT_ID`/`DRIVE_CLIENT_SECRET`/`DRIVE_CLIENT_SECRET_FILE` when set, otherwise validates the posted pair and never persists the client secret. `400` for an unrecognized client, a missing client ID or missing google libraries. Open `auth_url` in a browser |
| `GET /config/oauth/drive/callback` | query: `code`, `state`, plus `error`/`error_description` when Google refuses | Google's redirect target. Exchanges the code, saves the refresh token at the account's Drive credential path and answers a small self-contained HTML page that says the account is connected and closes the tab. `400` HTML for an unknown/expired `state`, a refusal reported by Google or a failed exchange |
| `GET /config/accounts/drive/status` | query: `flow_id` | the same shape as the OneDrive status route |
| `POST /config/accounts/update` | `method`, `alias`, `label` | `200 {ok, method, alias, label, default_method}` renames the alias and/or changes the display label while preserving root, credentials and the default; `409` for a taken alias, `400` for an invalid one, `404` for an unknown account. An omitted field is left alone; an empty `label` clears it |

The Drive and OneDrive setup flows are held in memory for 10 minutes; a restart cancels a sign-in in progress, and an unknown or expired `flow_id` answers `200 {status: "error"}` so a polling client only branches on JSON. Covered by `tests/test_config_api.py`, including the origin gate, the local-root boundary, the credential check on the default, and the redaction of a provider failure. The browser account flows and alias resolution are covered by `tests/test_account_setup_api.py`, `tests/test_accounts.py` and `tests/test_upload_methods.py`.

## GET /queue

Read-only OCR queue metrics for backpressure monitoring, with session ids and numeric counts only. Response `200` is the `ocr_queue_metrics()` snapshot: `scheduler` (`round_robin` or `fifo`), `queue_depth`, `processing_depth`, `total_depth`, `per_session` rows, `active_batch_sessions`, `active_sessions`, `source_ingesting_sessions` and a `worker` object with `started`, `alive`, `state` and `model_loaded`. The call is bounded: if a page writer holds the OCR lock, it answers a best-effort snapshot in tens of milliseconds with `partial: true` rather than waiting. Covered by `tests/test_ocr_scheduling.py` and `tests/test_responsiveness.py`.

## GET /health

The one call a client makes before anything else. It never performs network I/O and never waits on the OCR queue or a session lock: a dedicated thread refreshes one snapshot per second (see `_health_snapshot_loop`), and this route returns the latest one plus the cheap live fields. Response `200` includes:

- `app`, `version`, `status`
- `update_check` (`disabled`, `pending`, `ok` or `error`), `latest_version`, `update_available`, `update_url`, `update_error`
- `mokuro_installed`, `mokuro_flavor`, `mokuro_custom_fork`, `mokuro_repo`, `mokuro_fork_api`, and `mokuro_version` plus `mokuro_path` only when an engine is installed
- `megatools_installed`, `mega_configured`, `mega_creds_source`, `mega_library_root`; `megatools_installed` is global, while `mega_configured`, `mega_creds_source` and `mega_library_root` describe the first MEGA account, and every account's state is in `upload_methods`
- `upload_default` (the legacy boolean), `upload_method_default`, `upload_method_selected`, and one `upload_methods` entry per destination (`local` plus one per account, each carrying `id`, `alias`, `name`, `configured`, `default`, `current_folder`, the generic `provider`, `account`, `creds_source` and `tracked` keys, and the provider extras)
- `work_dir`, `output_dir`, `cors_origins`
- `fetchProxyPorts`, `fetchUpstreams`, `fetchPathUpstream`
- `active_sessions`, `ocr_chunk_size`, `ocr_idle_flush_s`, `ocr_queue_depth`, `ocr_processing_depth`, `ocr_queue_sessions`, `ocr_active_batch_sessions`, `ocr_worker`
- `busy`, `busy_stage` and `busy_detail`, derived from the activity tracker plus queued work
- `lanes`, one entry per independent work lane (`control`, `data`, `finalize`, `upload`) with `workers` and `queued`, so a developing stall names the lane that is behind
- `bunko`, the mokuro-bunko state for the terminal display: `installed`, `running`, `commit`, `method`, `folder`, `backend`, `default_method`, `in_sync` and `update_available`, or `null` when the support module is unavailable
- `health_snapshot`, the freshness of the cached document: `age_ms`, `interval_ms`, `stale` (true past five seconds) and `partial`; `error` is present when the last refresh failed and the previous snapshot is still being served
- `upload`, present only while a volume is uploading: `file`, `percent`, `current_bytes`, `total_bytes` and `speed_human`, taken from the active session's upload state. The field is absent when nothing is uploading

Covered by `tests/test_optional_ocr.py` at the route level, `tests/test_update.py` for the update fields, `tests/test_endpoints.py` for the flavor field, and `tests/test_responsiveness.py` for the no-stall guarantee.
