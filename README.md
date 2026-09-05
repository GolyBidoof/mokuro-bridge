# mokuro-bridge

A local OCR bridge for manga pages → **[reader.mokuro.app](https://reader.mokuro.app/)**.

Point it at a folder of page images (or POST pages from a capture script) and
it runs [mokuro](https://github.com/kha-white/mokuro) over them, producing the
three files reader.mokuro.app reads, arranged per series:

```
<output>/<series>/
  <volume>.cbz       # page images
  <volume>.mokuro    # OCR text + block data
  <volume>.webp      # cover
```

Output stays on your machine by default; uploading to your own MEGA account is
optional. Nothing about the OCR touches the cloud — it all runs locally, and
it works on macOS, Windows and Linux.

---

## Quickstart — just get it running

No architecture knowledge needed. Two terminals, five minutes.

**1. Install (once)**

```bash
git clone <your-url> mokuro-bridge
cd mokuro-bridge
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

That one `pip install` brings in everything the server needs *and* the OCR
engine: `fastapi`, `uvicorn`, `python-multipart`, `keyring` (for OS keychain
support) and `mokuro` (pulls PyTorch — the big one; first install takes a
while).

> Only if you run a **custom mokuro checkout** (e.g. an optimized fork):
> skip the `mokuro` line or ignore it and export `MOKURO_REPO=/path/to/checkout`.

**2. Start the bridge**

```bash
# macOS / Linux:
./run.sh
# Windows:
python server.py
```

You'll see `mokuro-bridge v0.2.0 on http://127.0.0.1:62642`.

**3. OCR a folder of pages you already have**

```bash
python ocr_folder.py "/path/to/my/manga pages" --title "My Manga 1巻"
```

(`python3 ocr_folder.py` on macOS/Linux.) It streams progress and prints where
the finished volume landed — by default `output/My Manga/My Manga 1巻.{cbz,mokuro,webp}`.

**4. Read it**

Open [reader.mokuro.app](https://reader.mokuro.app/) and point it at that
`output/` folder (via "Choose folder", or any folder reader), or upload to
MEGA (next section) and open your `/mokuro-reader` folder there.

That's the whole loop. Prefer scripting your own capture? Jump to
[Writing a capture client](#writing-a-capture-client) — four HTTP calls.

## Uploading to MEGA (optional)

MEGA upload is **off by default**. Enable it in three steps:

1. **Install megatools** for your OS: macOS `brew install megatools`,
   Debian/Ubuntu `apt install megatools`, others see
   [megatools.megous.com](https://megatools.megous.com/).
2. **Give the bridge your MEGA credentials** — one of these (first wins):
   - **Setup wizard (all platforms):** `python server.py --setup-mega` — asks
     once, then stores in your **OS keychain / credential store**: macOS
     Keychain, Windows Credential Manager, or Linux Secret Service
     (gnome-keyring). If no keychain is available it falls back to a
     permissions-restricted file (`~/.config/mokuro-bridge/credentials.env`).
   - **Environment variables:** `MEGA_EMAIL=you@example.com MEGA_PASSWORD=…`
     before starting the bridge.
   - **macOS-only helper script:** `./setup-keychain.sh` (writes the macOS
     Keychain directly).
3. **Turn uploads on**: export `MOKURO_BRIDGE_UPLOAD_DEFAULT=true` so every
   finished volume uploads, or pass `upload_to_mega=true` per request.

Uploaded layout mirrors the local one:

```
/Root/mokuro-reader/<series>/
  <volume>.cbz  <volume>.mokuro  <volume>.webp
```

Open `/mokuro-reader` in [reader.mokuro.app](https://reader.mokuro.app/) to read.

## How it works

```
┌────────────────────────────────────────────────────┐
│   capture client                                   │
│  (userscript / headless scraper / curl / …)        │
│                                                    │
└──────────────────────────┬─────────────────────────┘
                           │
                           │  POST /session/start — create a session
                           │  POST /session/{id}/page — one per captured page
                           │  GET  /session/{id}/status — poll progress
                           │  POST /session/{id}/finalize — NDJSON progress stream
                           ▼

┌────────────────────────────────────────────────────┐
│   mokuro-bridge  (http://127.0.0.1:62642)          │
│                                                    │
│   1. queue incoming pages                          │
│   2. chunked OCR via mokuro                        │
│   3. assemble <volume>.mokuro                      │
│   4. pack <volume>.cbz + cover .webp               │
│   5. local output folder  and/or  MEGA upload      │
└────────────────────────────────────────────────────┘

                           ▼
   <output>/<series>/<volume>.{cbz,mokuro,webp}  →  reader.mokuro.app
```

- Pages are OCR'd **as they arrive** (chunked batches, default 8 pages), so
  capture and OCR overlap instead of running one after the other.
- A **volume title** groups volumes into a shared series folder, stripping
  edition suffixes like `（２）`, `1巻`, `【電子限定…】`.
- Sessions survive server restarts (state is persisted under the work dir).
- One bridge serves many capture clients at once — each volume is its own
  session, so a whole series can be captured in parallel.

## Configuration

Everything is environment variables. Copy `.env.example` and `source` it, or
export in your shell/launcher.

| Variable | Default | Purpose |
|---|---|---|
| `MOKURO_BRIDGE_HOST` / `MOKURO_BRIDGE_PORT` | `127.0.0.1` / `62642` (spells "MANGA" on a phone keypad 🙂) | Bind address. Keep loopback unless you know why not. |
| `MOKURO_BRIDGE_WORK_DIR` | `~/mokuro-input` | Scratch space: page images + OCR JSON mid-session. |
| `MOKURO_BRIDGE_OUTPUT_DIR` | `<repo>/output` | Where finished volumes land when not uploading to MEGA. |
| `MOKURO_BRIDGE_UPLOAD_DEFAULT` | `false` | `true` = finalize uploads to MEGA unless told otherwise. |
| `CORS_ORIGINS` | BookWalker viewer origins | Comma-separated origins allowed to POST from the browser (userscripts). |
| `MOKURO_REPO` | *(none)* | Path to a mokuro checkout to use instead of the installed package. |
| `MEGA_LIBRARY_ROOT` | `/Root/mokuro-reader` | Remote MEGA folder that receives series folders. |
| `MEGA_EMAIL` / `MEGA_PASSWORD` | *(none)* | MEGA credentials (alternative to the setup wizard). |
| `MEGA_CREDS_FILE` | `~/.config/mokuro-bridge/credentials.env` | Credentials file used when env vars are absent and no OS keychain entry exists. |
| `OCR_CHUNK_SIZE` | `8` | Pages per OCR batch (tune for your GPU/CPU). |
| `OCR_IDLE_FLUSH_S` | `1.5` | Seconds to wait for a fuller batch before flushing. |
| `MIN_PAGES_FOR_MEGA` | `10` | Refuse MEGA upload below this many pages (failed-scrape guard). |
| `UVICORN_RELOAD` | `0` | Dev auto-reload (wipes in-memory sessions on change). |

## HTTP API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Dependency/config status (mokuro, megatools, MEGA creds…). |
| `POST` | `/session/start` | `title`, `reuse_existing` → new session id. |
| `POST` | `/session/resume` | `title`, `source_dir` — OCR a folder on disk (what `ocr_folder.py` uses). |
| `POST` | `/session/{id}/page` | Multipart `page` image + `filename` (browser capture). |
| `POST` | `/session/{id}/page-local` | `path` — ingest an image already on this machine (headless scrapers). |
| `GET` | `/session/{id}/status` | Capture/OCR progress snapshot. |
| `GET` | `/sessions` | All live sessions. |
| `POST` | `/session/{id}/finalize` | `upload_to_mega`, `delete_after_upload` → NDJSON progress stream. |

`finalize` form fields:

- `upload_to_mega` — `true` → MEGA; `false` → local output; unset → env default.
- `delete_after_upload` — `true` (default) removes the session's working files
  after a successful run (in MEGA mode that's everything; in local mode the
  finished trio in the output dir is kept).

## Writing a capture client

A client only needs four HTTP calls:

1. `POST /session/start` with a `title` → get `session_id`;
2. `POST /session/{id}/page` once per captured page (multipart image + filename);
3. optionally poll `GET /session/{id}/status`;
4. `POST /session/{id}/finalize` when capture finishes.

Storefront-specific capture scripts (browser userscripts, headless scrapers)
are intentionally **not** part of this repository — they embed account/session
handling. In browsers, a userscript can POST straight from the storefront
origin as long as that origin is in `CORS_ORIGINS`.

## Security notes

- The bridge binds to **127.0.0.1 only** by default. It is a local tool; don't
  expose it to a network without adding authentication.
- MEGA credentials are never stored in this repository. They live in your OS
  keychain / credential store (macOS Keychain, Windows Credential Manager,
  Linux Secret Service), an optional permissions-restricted file, or your
  environment. The temporary `.megarc` files written during uploads are
  deleted afterwards.
- CORS is an allow-list, not `*`. `page-local` ingest is restricted to your
  home directory and system temp paths.
- Session/persisted files under the work dir — page copies, `.mokuro`/`.html`
  siblings and mokuro's per-volume OCR cache (`<work>/_ocr/<volume>/`) — are
  removed after a successful finalize (`delete_after_upload=true`).

## Troubleshooting

**`mokuro_installed: false` in /health**
`pip install -r requirements.txt` should have installed mokuro. Otherwise
`pip install mokuro` or set `MOKURO_REPO` to a checkout.

**`mega_configured: false`**
Run `python server.py --setup-mega` (stores in your OS keychain/credential
store or a file), export `MEGA_EMAIL`/`MEGA_PASSWORD`, or on macOS run
`./setup-keychain.sh`. On headless Linux, keychain storage needs a Secret
Service daemon (gnome-keyring); the wizard then falls back to a 0600 file.

**OCR is slow**
Normal without a GPU. Raise `OCR_CHUNK_SIZE`/`OCR_IDLE_FLUSH_S` or use a
GPU/MPS-capable mokuro build via `MOKURO_REPO`. First run downloads models.

**Upload fails with `partial_upload`**
Check the `stderr` in the NDJSON error frame. Ensure `/mokuro-reader` (or your
`MEGA_LIBRARY_ROOT`) is creatable by your account — the bridge tries to create
it automatically.

**Port 62642 already in use**
Another process holds the port. Stop it, or pick another port with
`MOKURO_BRIDGE_PORT=62643 ./run.sh`. If an older auto-start agent from a
previous install is running, unload it: `launchctl bootout
gui/$(id -u)/com.mokuro-bridge` (macOS).

## Development

```bash
python3 -m py_compile server.py          # syntax check
./run.sh                                 # run with default config
UVICORN_RELOAD=1 python3 server.py       # dev auto-reload
python3 ocr_folder.py --help             # folder helper usage
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

Developed and refined with the help of **DeepSeek V4 Flash**.
