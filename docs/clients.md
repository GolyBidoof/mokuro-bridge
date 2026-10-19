# How the clients drive the bridge

Both downloaders are thin HTTP clients. Neither embeds store-specific knowledge in the bridge, and neither is required for the other to work: dokuha-cli is browserless and hands over a finished folder, while dokuha-userscript captures in the browser and streams pages as they arrive. This document gives the exact request sequence each one uses.

The reference for a streaming capture is the bundled BookWalker userscript `bookwalker-downloader-mokuro.user.js` in this repository, which is the same protocol the dokuha-userscript family speaks. The dokuha-cli sequence below was read from that project's vendored bridge client, `vendor/ebookjapan/bridge.mjs`, and its `src/bridge.js` orchestration.

## One capture, end to end

Every client follows the same shape:

1. `GET /health` to find a running bridge and read its capability block.
2. Discover the destination with `GET /upload-methods`, then optionally check `POST /destination/check` to skip a volume already delivered.
3. Open a session: `POST /session/start` for a streaming capture, or `POST /session/resume` for a folder that already exists on this machine.
4. Deliver pages: `POST /session/{id}/page` once per page, or one `/session/resume` for the whole folder.
5. Watch progress with `GET /session/{id}/status`, `GET /queue` and `GET /sessions`.
6. `POST /session/{id}/finalize`, read the NDJSON stream to its terminal `done` frame, and treat an `error` frame or a stream that ends without `done` as a failure. The `series_json` frames in that stream report the per-series index the reader needs, and a `partial_upload` terminal frame means the volume files arrived but the index did not, which a client should surface rather than report as success.

## dokuha-cli

dokuha-cli is the batch downloader and the heavier client. It prefers the folder hand-off, since a local bridge can read the folder directly and the pages never cross a socket.

**Discovery.** It walks a candidate list (an explicit URL, `BWDD_BRIDGE_URL`, `MOKURO_BRIDGE_URL`, then `http://127.0.0.1:62642`, `:63442` and `:63443`) and accepts the first `GET /health` that answers `status: "ok"` or `app: "mokuro-bridge"`.

**Destination.** It calls `GET /upload-methods`, whose ids are account aliases (the first account of a provider is the bare provider name, later ones are `mega-2`, `drive-next` and so on), and sends the chosen id as `upload_method`. `--dest` takes an alias, and the historical `<provider>:<name>` spelling still resolves to the same account; no `--dest` keeps the bridge's current sticky default. `--dest-folder` becomes `local_dir` only when the method is `local`. Before downloading a volume it can call `POST /destination/check` and skip work the destination already has.

**Duplicate avoidance.** It calls `GET /sessions` and `GET /queue` first, and refuses to submit a duplicate when a session for the same title is already live. If `POST /session/start` answers `400` or `409` because a session owns the title, it inspects that live session instead of guessing.

**Folder hand-off.** For a loopback bridge when the volume folder sits under an allowed ingest root, it makes one `POST /session/resume` with `title` and `source_dir`. The response carries `session_id`, `synced_from_source`, `queued_for_ocr` and `ocr_cached`. A `403` (path refused), `404` or `405` (route absent) falls back to the per-page path; a `5xx` also falls back rather than losing the volume.

**Streaming capture.** Otherwise it calls `POST /session/start` with `reuse_existing=true`, then pushes pages with `POST /session/{id}/page` (multipart `page`, `filename`, `page_num`), four concurrent by default, with backoff on `429` and `5xx`. Re-pushing a page the bridge already OCR'd comes back as a `409` that this client recognises as a cache hit rather than a failure. It polls `GET /session/{id}/status` with a cooldown that grows from 2 s to 15 s while nothing changes.

**Finalize.** It always ends with one `POST /session/{id}/finalize`, sending `upload_method` as an account alias (for example `mega-work`, or the legacy `upload_to_mega=true`), `local_dir` when the method is local, `delete_after_upload=true`, and `overwrite` on a re-run so an existing destination file does not fail the retry. It reads the NDJSON stream, raises on an `error` frame, and requires a `done` frame; a stream that ends without one is treated as a truncated upload.

## dokuha-userscript

dokuha-userscript is the in-browser companion. The bundled BookWalker script shows the shape: capture on the store page, stream to the bridge, then finalize. Nothing is installed beyond a userscript manager, and the bridge must already be running.

**Health and fetch lanes.** Like the bundled script, it calls `GET /health` first to confirm the bridge is up and to read its capability block. That block is also where the accelerator is advertised: `fetchProxyPorts` (the extra localhost ports the bridge bound), `fetchUpstreams` (which CDN hosts they may serve) and `fetchPathUpstream`. A userscript that wants the extra lanes reads those ports and routes page fetches through them, each of which is a fresh browser origin worth six more connections; the `/_bwdd/<host>/<path>` form keeps a request CORS-simple so it avoids a preflight per page. With the bridge stopped the client falls back to its own lanes, because downloading never depends on the bridge. The bundled BookWalker userscript in this repository contains the capture loop only and does not use the extra ports; the multi-port lanes are a later addition on the userscript side.

**Session.** It calls `POST /session/start` with `title` only, then reads `session_id` and `ocr_chunk_size` from the response.

**Pages.** As each page is captured it makes one `POST /session/{id}/page` with multipart `page` (the blob), `filename` and `page_num`. OCR overlaps the capture: the per-page response already carries `pages_ocr_done` and `pages_received`, which the script shows as a live pipeline progress line.

**Status.** It polls `GET /session/{id}/status` on an interval and stops when the capture finishes.

**Finalize.** It calls `POST /session/{id}/finalize` with `delete_after_upload=true` and the legacy `upload_to_mega=true` (the bundled script names MEGA explicitly, which is what makes MEGA the sticky default after its first run). It parses the NDJSON stream, surfaces each frame's `message` in the panel, throws on an `error` frame, and opens `reader_url` from the `done` frame.

## What a client must not assume

- **A stream that ends without `done` is not a success.** Both clients treat it as a truncated upload.
- **`upload_method` is sticky.** Naming a destination on a finalize changes the default for every later request, including from the other client, until another explicit choice replaces it. The stored default is the account alias, and `GET /health` and `GET /upload-methods` report it as `upload_method_default`. See [destinations.md](destinations.md).
- **A `409` is not always an error, and identical bytes are not a `409`.** Re-sending the exact bytes the bridge already holds comes back as `200` with `already_present: true`. A `409` reading `already has completed OCR` means the page is already cached from different bytes, so the stored OCR no longer matches the image.
- **The bridge is unauthenticated.** Loopback binding is the only access control, so a client should reach it on `127.0.0.1` and no further.
