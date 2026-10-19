# How any client talks to the bridge

The bridge exposes one plain HTTP protocol, and that protocol is the contract. Any program that can produce page images can use it: a browser script, a desktop downloader, a shell script with `curl`, or an application that already has a folder of pages on disk. This page is the short map. [integrating.md](integrating.md) is the step-by-step integration guide with retry rules, the full finalize contract and a minimal client loop; [endpoints.md](endpoints.md) is the field-by-field HTTP reference.

A client is not required to know anything about the bridge's destinations, its OCR engine or the files it writes. It needs a base URL, a title, and a way to deliver images.

## One capture, end to end

Every client follows the same shape:

1. `GET /health` to find a running bridge and read its capability block.
2. `GET /upload-methods` to list destinations and the current default, then optionally `POST /destination/check` to skip a volume the destination already has.
3. Open a session: `POST /session/start` for a capture that is streaming now, or `POST /session/resume` to hand over a folder that already exists on this machine.
4. Deliver pages: `POST /session/{id}/page` once per page, `POST /session/{id}/page-local` to copy one from a local path, or one `/session/resume` for the whole folder.
5. Watch progress with `GET /session/{id}/status`, `GET /queue` and `GET /sessions`, and read the per-page counters the page route already returns.
6. `POST /session/{id}/finalize`, read the NDJSON stream to its terminal `done` frame, and treat an `error` frame or a stream that ends without `done` as a failure. A `partial_upload` terminal frame means the volume files arrived but the series index did not, which a client should surface rather than report as success.

## The two delivery models

**Streaming** suits a client that is capturing now, especially when the pages never exist as a folder: start a session, POST each page as it arrives, then finalize. OCR overlaps the capture, so the last page is often read while the first ones are already done.

**Folder hand-off** suits a client that already wrote a volume to this machine: one `POST /session/resume` with `title` and `source_dir` lets the bridge read the pages itself, so they never cross a socket. The route is confined to the bridge's ingest roots (the user's home and the system temp locations, plus any `MOKURO_BRIDGE_INGEST_ROOTS`), it syncs newer pages from the source, and it queues only the pages whose OCR cache is missing. A `403` means the path is outside those roots; a `404` or `405` means the route is not available on the bridge being talked to, so fall back to streaming.

## The finalize stream

`POST /session/{id}/finalize` answers `application/x-ndjson`, one JSON object per line, with buffering disabled. A client reads it as it arrives and acts on the `stage` field. The stages it can see, in the order they normally appear:

- `wait_ocr` while the OCR queue drains, at least every 750 ms, carrying `pages_received`, `pages_ocr_done`, `pages_ocr_pending` and `pages_ocr_failed`.
- `assemble`, then `pack`, while the `.mokuro`, the `.cbz` and the cover are produced.
- `series_json` when the per-series index is written, uploaded, found unchanged or found unwritable, and with `external: true` when mokuro-bunko owns that destination's metadata.
- `upload` first as a batch plan that names every file with `total_bytes` before any bytes move, then once per file with that file's `url` as it finishes; `upload_progress` in between carries `file`, `bytes`, `total_bytes`, `current_bytes`, `percent`, `speed_bps` and `speed_human`.
- `cleanup` when the working files are removed.
- `done` or `error`, always last.

The `done` frame carries the final record and a per-file `uploads` array. Every entry has `file`, `success`, `size` in bytes and `url`; a remote entry adds `bytes`, `total_bytes`, `current_bytes`, `percent` and `speed_bps`, and a local entry adds `path` and `unchanged`. `size` is present for both, so a client totals that one field. A mokuro-bunko destination lists three files rather than four, because Bunko compiles `series.json` itself.

## What a client must not assume

- **A stream that ends without `done` is not a success.** Treat it as a truncated upload and recover by re-finalizing.
- **`upload_method` is sticky.** Naming a destination on a finalize changes the default for every later request, including from other clients, until another explicit choice replaces it. The stored default is the account alias, and `GET /health` and `GET /upload-methods` report it as `upload_method_default`. See [destinations.md](destinations.md).
- **A `409` is not always an error, and identical bytes are not a `409`.** Re-sending exactly the bytes the bridge already holds returns `200` with `already_present: true`. A `409` saying `Page already has completed OCR` means the page is cached from different bytes, so the stored OCR no longer matches the image.
- **The bridge is unauthenticated.** Loopback binding is the only access control, so a client should reach it on `127.0.0.1` and no further.
- **The bridge does not rate limit with `429`.** It bounds concurrency through its own lanes and admission; a client should still back off exponentially on `5xx` and transport errors, and treat a `429` from a reverse proxy like any other `5xx`.

## Worked examples: the dokuha downloaders

The two [dokuha](https://github.com/GolyBidoof) downloaders are examples of clients that speak this protocol. They are not required, and the bridge has no dependency on them.

**dokuha-cli** is the browserless batch downloader. It prefers the folder hand-off: for a loopback bridge whose volume folder sits under an allowed ingest root, it sends one `POST /session/resume` with `title` and `source_dir` and reads `synced_from_source`, `queued_for_ocr` and `ocr_cached` from the response. When it has to capture page by page it starts a session with `reuse_existing=true`, pushes pages with bounded concurrency and backoff, recognises the already-cached `409` as a hit, and finalizes with an account alias, `delete_after_upload=true` and `overwrite` on a retry.

**dokuha-userscript** is the in-browser client. It calls `GET /health` to confirm the bridge and read `fetchProxyPorts`, `fetchUpstreams` and `fetchPathUpstream`, routes page fetches through the extra localhost ports when they are offered, starts a session with a title, streams each captured page as it arrives, and opens the `reader_url` from the `done` frame. With the bridge stopped it falls back to its own lanes, because downloading never depends on the bridge.

They follow this protocol; the bridge does not depend on them. A new client needs only the sections above and [integrating.md](integrating.md).

## Where the metadata is owned

A mokuro-bunko managed destination is the one case where the bridge does not write the series index. Bunko compiles `series.json` and the library catalog itself, the bridge uploads the three volume files and the `.mokuro` before the `.cbz`, and the finalize stream reports `series_json` with `external: true`. Client behaviour does not change: the same endpoints, the same `done` frame, one fewer file in `uploads`. See [bunko.md](bunko.md).
