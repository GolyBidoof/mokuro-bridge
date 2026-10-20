# Integrating your app with mokuro-bridge

This guide is for someone who wants to plug their own program into a running mokuro-bridge. It is written protocol first and app independent: nothing here assumes a particular downloader, store or language. The bridge accepts page images over HTTP, turns them into a reader-ready volume, and delivers the result to the destination you choose.

Read this page with [endpoints.md](endpoints.md) open for exact fields and error codes. [clients.md](clients.md) is the shorter overview, and the two dokuha downloaders at the end are one worked example of a client, not a requirement.

## Before the first request

- **The bridge runs on loopback.** The default address is `http://127.0.0.1:62642`, and it is unauthenticated by design. An integration should run on the same machine, or reach the bridge through something that does authenticate, and should never expose it beyond loopback.
- **Discover it, do not hardcode it.** Start with the address the user configured, then `http://127.0.0.1:62642`, the default. The bridge listens wherever `--port` or `MOKURO_BRIDGE_PORT` put it, so a client that cannot find it should ask for a URL rather than probe more ports. Accept the first `GET /health` that answers `status: "ok"` or `app: "mokuro-bridge"`. Do not probe the neighbouring ports: the fetch accelerator binds `port+1` up to `port+MOKURO_BRIDGE_FETCH_PORTS` and serves only `GET /__bwdd_health` and page fetches there, not the bridge API.
- **Read capabilities from `/health`.** The capability block reports `version`, `mokuro_installed`, `mokuro_flavor`, `ocr_chunk_size`, `lanes`, `fetchProxyPorts`, `fetchUpstreams`, `fetchPathUpstream`, `upload_methods`, `upload_method_default` and `health_snapshot`. A client should branch on those fields rather than assume a version.
- **One bridge, many clients.** Sessions, the sticky default destination and the OCR queue are shared process state. Two clients driving one bridge can see each other's sessions through `GET /sessions`.

## The shape of an integration

Every integration is the same six steps, whatever its language or delivery model:

1. **Discover.** `GET /health`. Confirm the bridge is up and read its capabilities.
2. **Choose a destination.** `GET /upload-methods` lists every target with its `id` (the account alias) and `upload_method_default`. Optionally `POST /destination/check` asks whether a finished volume is already there, which lets a client skip work it does not need to redo.
3. **Open a session.** `POST /session/start` for a streaming capture, or `POST /session/resume` for a folder that already exists on this machine.
4. **Deliver pages.** `POST /session/{id}/page` per image, or `POST /session/{id}/page-local` per local path, or the entire folder in the resume call.
5. **Watch progress.** `GET /session/{id}/status`, `GET /queue` and `GET /sessions`, plus the counters returned by the page route itself. OCR runs while pages are arriving, so these numbers move during capture.
6. **Finalize.** `POST /session/{id}/finalize`, read the NDJSON stream frame by frame, and act on the terminal frame: success, `partial_upload`, or an `error` / truncated stream that must be reported as a failure.

## The two delivery models

| | Folder hand-off | Streaming capture |
| --- | --- | --- |
| Use when | The client already wrote the volume to this machine | The client is capturing now, or the pages exist only in memory or a browser |
| Route | `POST /session/resume` with `title` and `source_dir` | `POST /session/start`, then `POST /session/{id}/page` per page |
| What the bridge reads | The folder directly, so the pages never cross a socket | One uploaded image per request |
| OCR overlap | Queued as soon as the folder is scanned | Starts while later pages are still arriving |
| Constraint | `source_dir` must sit under the bridge's ingest roots, otherwise `403` | One image per request, up to 100 MiB per page |
| Response | A session snapshot with `synced_from_source`, `queued_for_ocr` and `ocr_cached` | A session snapshot; the per-page response carries the live counters |
| Best for | Batch tools and anything that already writes files | Browser clients, streamed captures and clients that never write a folder |

A client may combine them: resume a folder, add a page that arrived late with `page-local`, then finalize. The two models differ only in how pages reach the bridge; everything after the session exists is identical.

### Opening a session

`POST /session/start` takes `title` and optional `reuse_existing`. The response carries `session_id`, `title`, `safe_title`, `vol_dir`, `message`, `ocr_chunk_size`, `reused`, `pages_received` and `pages_ocr_done`. `reuse_existing=true` reopens a live session with the same safe title instead of creating a duplicate, which is what a retrying client wants; a session that is already finalizing or finalized answers `400`, because there is nothing left to add.

`POST /session/resume` takes `title` and optional `source_dir`. With no `source_dir` it simply reopens the volume as a session, which is useful after a restart. It clears earlier OCR failures because an explicit resume is the operator's retry boundary, syncs newer pages from the folder, and queues only pages without a valid cache entry. It answers with the snapshot plus `vol_dir`, `synced_from_source`, `queued_for_ocr`, `ocr_cached` and `resumed`.

A title is the volume's identity. The bridge sanitizes it, validates it, and adds a short random suffix if that directory already exists in the work area, so a batch may reuse a title safely. The series folder name is derived from the title by stripping edition and imprint tags; the volume files keep the full title.

### Delivering pages

`POST /session/{id}/page` is multipart with `page` (the image bytes), `filename` and `page_num`. The bridge sniffs the magic bytes, applies a 100 MiB limit, stores the file in the session's working directory and queues it for OCR. The response is the session snapshot plus `filename`, `page_num` and, when the bytes were identical to what the bridge already held, `already_present: true`. Images may be WebP, JPEG, PNG, GIF, BMP, AVIF or TIFF; the bridge keeps the extension the filename carries.

`POST /session/{id}/page-local` takes `path`, `filename` and optional `page_num` and copies the image from a path on this machine, under the same ingest roots as `source_dir`. Its response adds `source` and `copied`.

`POST /session/{id}/cover` uploads a cover early, so a destination has it before the volume is ready. It is optional: finalize generates the cover from the first WebP page, or the first image when there is no WebP, and reuses a cover whose digest still matches.

Page number, not filename, is the identity. Re-sending page 12 under a new name replaces the file the old name held, which is what stops a client that changes its naming scheme from doubling the volume. Two different extensions for the same stem are refused with `409`, because the volume would contain two files for one page.

## The finalize contract

`POST /session/{id}/finalize` takes `upload_method`, the legacy `upload_to_mega`, `delete_after_upload` (default true), `local_dir` when the target is `local`, and `overwrite` (`fail`, `skip` or `overwrite`, default `fail`). It answers `application/x-ndjson`: one JSON object per line, no caching, no proxy buffering, at least one frame every 750 ms while the OCR queue drains and one per file during an upload.

A client must parse by line and dispatch on `stage`. Unknown stages and unknown fields must be ignored, so a newer bridge can add frames without breaking an older client. The frames a client can see, and what it should do with each:

| Stage | What it carries | What a client does |
| --- | --- | --- |
| `wait_ocr` | `pages_received`, `pages_ocr_done`, `pages_ocr_pending`, `pages_ocr_failed`; the first frame also has `ocr_chunk_size` | Show OCR progress; a non-zero `pages_ocr_failed` is a warning, and a terminal `error` follows if OCR cannot recover |
| `assemble` | A message only | Keep the progress line alive |
| `pack` | A message; a local destination follows with a second `pack` frame carrying `output_dir`, `series` and the final `files` list | Note that the volume files are being written |
| `series_json` | A result with `ok`, `unchanged` or `external` and `file`, or (remote, bridge-published manifest) an announcement with `file` and `remote_path` before the upload | Report an index failure if `ok` is false; do not treat it as success |
| `upload` (plan) | `files`, an array of `{file, total_bytes}` sent before any bytes move, plus `series`, `remote_path` and `method`; a local destination sends no upload frames at all | Pre-size an overall progress bar |
| `upload_progress` | `upload` with `file`, `bytes`, `total_bytes`, `current_bytes`, `percent`, `speed_bps` and `speed_human` | Update the progress line; values are cumulative, not deltas |
| `upload` (per file) | `file`, `method` and, on success, `url` | Record the file; a failure is reported in the terminal frame |
| `cleanup` | A message | The delivery is done and working files are being removed |
| `done` | `status` (`success` or `partial_upload`), `title`, `series`, `pages`, `pages_ocr_done`, `method`, `remote_path`, `output_dir`, `staging`, `reader_url` and `uploads` | Success. Open `reader_url` for a remote delivery when present, and record the per-file `uploads` |
| `error` | `status` (`ocr_failed`, `ocr_cache_invalid`, `persistence_error`, `finalize_error`, `cleanup_error`, `invalid_local_output` and so on) and a message | Terminal failure. Raise it, keep the session retryable, and surface the message |

The per-file `uploads` array is the authoritative record of what was delivered. Every entry has `file`, `success`, `size` in bytes, `url`, `stderr` and `duration_s`; a remote entry adds `bytes`, `total_bytes`, `current_bytes`, `percent` and `speed_bps`, and a local entry adds `path` and `unchanged`. `size` is the field to total. A remote finalize lists the three volume files and then `series.json`; a local finalize lists only `series.json`, because the volume files were written by the pack step and are named in the second `pack` frame's `files` list; a mokuro-bunko destination lists only the three volume files and reports the index with `external: true`.

Three rules matter more than the table:

- **`done` is required.** A stream that closes without a terminal frame is a truncated upload, even if every file looked finished. Re-finalize the same session, with `overwrite` or `skip` set when the destination may already hold files from the interrupted attempt.
- **`partial_upload` is not success.** The volume files reached the destination but the series index did not, and a series without an index is a series the reader cannot build. Surface it, and retry once the cause is fixed; the working files are kept for that.
- **`error` is terminal.** Raise it rather than polling for more. The statuses are listed above; `ocr_cache_invalid` means the cache was found missing or corrupt before assembly, and `persistence_error` means the session state could not be written.

## Idempotency and retries

The bridge is built to be retried by a client that lost its place, and to be safe when two attempts overlap.

- **Identical page bytes are `200`.** Re-sending them returns `already_present: true` and leaves the queued OCR alone.
- **A `409` says why, and not all `409`s are fatal.** The meanings a client will meet: `Page already has completed OCR` (the cached result belongs to different bytes, so the page changed under the same name), `Page is currently being OCR'd`, `Page is currently being ingested`, `Page is already queued or present`, `Image filename stem already exists with another extension`, `Session finalization is already in progress`, and `Session is finalizing or already finalized`. The two in-flight cases are transient and worth a backoff retry; the rest are conflicts that retrying the same request will not fix.
- **A duplicate session is recoverable.** `POST /session/start` with `reuse_existing=true` reopens the live session rather than duplicating it. A `409` reading `created concurrently; retry reuse` means another request won a race: retry once with `reuse_existing=true` and continue with the session id the title already has.
- **Retry `5xx` and transport errors, not `4xx`.** A validation `400` and an authorisation `403` will not improve on retry. Use exponential backoff with jitter for `5xx`, timeouts and refused connections, and stop after a bounded number of attempts.
- **The bridge does not emit `429`.** It bounds concurrency through its own worker lanes, so no client-facing rate limiter exists. A `429` can only come from something in front of the bridge; treat it like a `5xx` with a longer backoff.
- **Finalize is the idempotent resume point.** A client that lost the stream should call finalize again on the same session, with `overwrite` set when the destination may already have files from the interrupted attempt. `fail` (the default) reports an existing file; `skip` treats it as success; `overwrite` replaces it, except on MEGA where the new copy lands as a versioned sibling and the old one is kept.
- **The missing-cache check protects the output.** Before assembly the bridge validates the per-page OCR cache and refuses with `ocr_cache_invalid` rather than emitting an incomplete `.mokuro`, so a retry is the correct response.

## Destination ownership

`upload_method` is an account alias (`local` included), never a filesystem path. A bare provider name works only because it is that provider's first account alias, and the historical `provider:name` and boolean spellings still resolve, but a new client should read the ids from `GET /upload-methods` and send one on finalize. Remember that the choice becomes the bridge-wide sticky default for every client until another explicit choice replaces it. A client that must not move another client's destination should always name its own on every finalize.

A destination managed by mokuro-bunko owns the compiled metadata. The bridge uploads only the three volume files, sends the `.mokuro` before the `.cbz`, and reports `series_json` with `external: true`; `catalog.json` and the per-series `series.json` are Bunko's. The client contract does not change, and the `done` frame simply has three entries in `uploads`. A destination can also be declared as bridge-owned or Bunko-owned explicitly through `series_owner` in its account metadata; see [bunko.md](bunko.md).

## One worked example: the dokuha downloaders

The two [dokuha](https://github.com/GolyBidoof) downloaders are external clients that speak exactly this protocol. They are useful to read as examples, and nothing in the bridge depends on them.

- **dokuha-cli** is the browserless batch tool. It favours the folder hand-off: for a loopback bridge and a volume folder under an ingest root, one `POST /session/resume` with `title` and `source_dir`, then it reads `synced_from_source`, `queued_for_ocr` and `ocr_cached`. When it captures page by page instead, it starts with `reuse_existing=true`, pushes pages with bounded concurrency and backoff, treats a page the bridge already holds (`200` with `already_present`) as a hit rather than a failure, and finalizes with an alias, `delete_after_upload=true` and `overwrite` on a rerun.
- **dokuha-userscript** is the browser client. It reads `fetchProxyPorts` and `fetchUpstreams` from `GET /health`, streams each captured page with `POST /session/{id}/page`, shows the per-page counters as a live pipeline, and opens the `reader_url` from the `done` frame. When the bridge is absent it falls back to its own fetch lanes.

Neither tool is required to use the bridge, and the bridge does not integrate with either one specifically.

## A minimal client loop

The following pseudo-code is enough to integrate a new producer of page images. It assumes the pages exist one at a time; a folder client replaces steps 3 to 5 with one `session/resume` call.

```text
base = first of [configured_url, "http://127.0.0.1:62642"] where GET /health answers

methods = GET base/upload-methods
destination = preferred alias from methods.methods, or methods.upload_method_default

session = POST base/session/start form {title, reuse_existing: true}
id = session.session_id
ocr_chunk = session.ocr_chunk_size

for index, page in pages:
    attempt = 0
    loop:
        response = POST base/session/{id}/page multipart {page, filename, page_num: index}
        if response.status == 200: break
        if response.status == 409:
            if "being OCR'd" in response.detail or "being ingested" in response.detail:
                attempt += 1
                if attempt > 5: fail("page stuck in flight")
                sleep(backoff(attempt)); continue      # transient, retry the same page
            fail(response.detail)                      # different bytes or an extension clash
        if response.status == 400:
            fail(response.detail)                      # do not retry a validation error
        if response.status >= 500 or transport error:
            attempt += 1
            if attempt > 5: fail("bridge unavailable")
            sleep(min(2 ** attempt, 30) + random jitter)
            continue
        fail(response.detail)

stream = POST base/session/{id}/finalize form {
    upload_method: destination,
    delete_after_upload: true,
    overwrite: "overwrite" when retrying a previous attempt,
}

for line in stream.lines:
    frame = parse_json(line)
    switch frame.stage:
        case "wait_ocr":     show_ocr(frame.pages_ocr_done, frame.pages_received, frame.pages_ocr_failed)
        case "upload_progress": show_upload(frame.upload.file, frame.upload.percent, frame.upload.speed_human)
        case "series_json":  remember_index_result(frame)
        case "error":        fail(frame.status, frame.message); return failure
        case "done":
            if frame.status == "partial_upload":
                warn("volume delivered, series index missing")
            else:
                record(frame.uploads)              # file, size, success, url
                if frame.reader_url: open_reader(frame.reader_url)
            return success

fail("stream ended without a terminal frame")   # truncated upload, retry finalize
```

## Integration checklist

- Discovery starts from a configured address and confirms `/health`, rather than assuming a version or probing ports that belong to the fetch accelerator.
- The client names its destination on finalize and does not rely on the sticky default.
- Every page POST handles `200`, the transient in-flight `409` with a backoff retry, the conflicting `409` cases, `400` validation and `5xx` backoff separately.
- The finalize stream is parsed per line, unknown stages are ignored, and `done` is required for success.
- `partial_upload` is reported as its own outcome, never as success.
- The per-file `uploads` array, not the progress frames, is used as the record of what was delivered.
- A retried finalize passes `overwrite` (or `skip`) because an interrupted attempt may have left files at the destination; `skip` treats an existing file as success and `overwrite` replaces it. A `409` from finalize itself only means another finalization already owns the session.
- The bridge is reached on loopback only.
