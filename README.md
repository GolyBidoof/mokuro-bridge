# mokuro-bridge

A local OCR bridge for manga captures → **[reader.mokuro.app](https://reader.mokuro.app/)**.

Store-agnostic, single-purpose: any client that can capture page images of a
volume can POST them to this tiny local HTTP service. The bridge runs
[mokuro](https://github.com/kha-white/mokuro) over the pages and produces the
three files reader.mokuro.app understands, arranged per series:

```
<output>/<series>/
  <volume>.cbz       # page images
  <volume>.mokuro    # OCR text + block data
  <volume>.webp      # cover
```

Output can be kept locally (default) or uploaded to a MEGA account of your
choice (opt-in). There is no cloud dependency for the OCR itself — everything
runs on your machine.

> Formerly "bw-mokuro-bridge" (BookWalker-specific). The bridge core was
> already store-agnostic, so this project is just that core made configurable
> and documented.

---

## How it works

```
capture client (userscript / headless scraper / curl / …)
        │  POST /session/start            ┌───────────────────────────┐
        │  POST /session/{id}/page  ◄────►│  mokuro-bridge (local)    │
        │  GET  /session/{id}/status      │  · page queue + chunked   │
        │  POST /session/{id}/finalize    │    OCR (mokuro)           │
        └─────────────────────────────────►│  · .mokuro assemble      │
                                           │  · CBZ + cover pack      │
                                           │  · local output folder   │
                                           │    and/or MEGA upload    │
                                           └───────────────────────────┘
```

- Pages are OCR'd **as they arrive** (chunked batches, default 8 pages), so
  capture and OCR overlap instead of running one after the other.
- A **volume title** groups volumes into a shared series folder, stripping
  edition suffixes like `（２）`, `1巻`, `【電子限定…】`.
- Sessions survive server restarts (state is persisted under the work dir).

## Requirements

- **Python 3.10+**
- **mokuro** — the OCR engine:
  - vanilla: `pip install mokuro`, or
  - a custom checkout (e.g. an optimized fork): set `MOKURO_REPO=/path/to/fork`
- Optional, only for MEGA uploads: [megatools](https://megatools.megous.com/)
  (macOS: `brew install megatools`) + MEGA credentials (see below).

No GPU required — mokuro runs on CPU (slow but works). The bridge is agnostic
to the storefront; built-in example clients for BookWalker/ebookjapan live in
this repo for reference.

## Quickstart

```bash
# 1. Install
git clone <your-url> mokuro-bridge
cd mokuro-bridge
python3 -m pip install -r requirements.txt
python3 -m pip install mokuro          # OCR engine (or set MOKURO_REPO)

# 2. Run the bridge
./run.sh
#   or: python3 server.py

# 3. Check health — all three flags should be true for the full pipeline:
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
```

Health fields you care about: `mokuro_installed`, `megatools_installed`
(only needed for MEGA), `mega_configured` (only needed for MEGA).

### Minimal capture session (local output, no MEGA)

```bash
# Start a session for a volume
curl -s -F 'title=My Manga 1巻' http://127.0.0.1:8765/session/start
# → {"session_id": "ab12cd34ef56", ...}   (note it down)

# POST each captured page (page_001.webp, page_002.webp, …)
curl -s -F 'page=@page_001.webp' -F 'filename=page_001.webp' \
     http://127.0.0.1:8765/session/ab12cd34ef56/page

# Wait for OCR + pack everything. Output lands in ./output/My Manga/
curl -sN -F 'upload_to_mega=false' \
     http://127.0.0.1:8765/session/ab12cd34ef56/finalize
```

The finalize call streams progress as NDJSON (`wait_ocr` → `assemble` →
`pack` → `done`). Resulting layout:

```
output/
└── My Manga/                    # series folder (derived from the title)
    ├── My Manga 1巻.cbz
    ├── My Manga 1巻.mokuro
    └── My Manga 1巻.webp
```

Point [reader.mokuro.app](https://reader.mokuro.app/) at that folder (via
"Choose folder" in a MEGA-linked browser, or any folder reader).

## Uploading to MEGA (optional)

MEGA upload is **off by default**. Enable it once:

1. `brew install megatools` (or your platform's equivalent).
2. Provide credentials in one of three ways (first match wins):
   - **Env vars:** `MEGA_EMAIL=you@example.com MEGA_PASSWORD=… ./run.sh`
   - **Setup wizard:** `python3 server.py --setup-mega` — asks once and stores
     in the macOS Keychain, or `~/.config/mokuro-bridge/credentials.env`
     (chmod 600) elsewhere.
   - **macOS Keychain script:** `./setup-keychain.sh`
3. Either export `MOKURO_BRIDGE_UPLOAD_DEFAULT=true` to make every finalize
   upload, or pass `upload_to_mega=true` per request.

Uploaded layout (mirrors the local one):

```
/Root/mokuro-reader/<series>/
  <volume>.cbz
  <volume>.mokuro
  <volume>.webp
```

Then open that `/mokuro-reader` folder in [reader.mokuro.app](https://reader.mokuro.app/).

## Configuration

Everything is environment variables. Copy `.env.example` and `source` it, or
export in your shell/launcher.

| Variable | Default | Purpose |
|---|---|---|
| `MOKURO_BRIDGE_HOST` / `MOKURO_BRIDGE_PORT` | `127.0.0.1` / `8765` | Bind address. Keep loopback unless you know why not. |
| `MOKURO_BRIDGE_WORK_DIR` | `~/mokuro-input` | Scratch space: page images + OCR JSON mid-session. |
| `MOKURO_BRIDGE_OUTPUT_DIR` | `<repo>/output` | Where finished volumes land when not uploading to MEGA. |
| `MOKURO_BRIDGE_UPLOAD_DEFAULT` | `false` | `true` = finalize uploads to MEGA unless told otherwise. |
| `CORS_ORIGINS` | BookWalker viewer origins | Comma-separated origins allowed to POST from the browser (userscripts). |
| `MOKURO_REPO` | *(none)* | Path to a mokuro checkout to use instead of the installed package. |
| `MEGA_LIBRARY_ROOT` | `/Root/mokuro-reader` | Remote MEGA folder that receives series folders. |
| `MEGA_EMAIL` / `MEGA_PASSWORD` | *(none)* | MEGA credentials (alternative to wizard/Keychain). |
| `MEGA_CREDS_FILE` | `~/.config/mokuro-bridge/credentials.env` | Credentials file used when env vars are absent. |
| `OCR_CHUNK_SIZE` | `8` | Pages per OCR batch (tune for your GPU/CPU). |
| `OCR_IDLE_FLUSH_S` | `1.5` | Seconds to wait for a fuller batch before flushing. |
| `MIN_PAGES_FOR_MEGA` | `10` | Refuse MEGA upload below this many pages (failed-scrape guard). |
| `UVICORN_RELOAD` | `0` | Dev auto-reload (wipes in-memory sessions on change). |

## HTTP API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Dependency/config status (mokuro, megatools, MEGA creds…). |
| `POST` | `/session/start` | `title`, `reuse_existing` → new session id. |
| `POST` | `/session/resume` | `title`, `source_dir` — re-OCR an on-disk volume, syncing missing pages from a folder. |
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

## Example clients in this repo

The bridge is transport-agnostic; these are the two store-specific clients the
project grew from, kept as reference implementations:

- `bookwalker-downloader-mokuro.user.js` — Tampermonkey script: captures pages
  from the BookWalker web viewer and POSTs them straight into a bridge session
  (must stay `@grant none`; requires the storefront origin in `CORS_ORIGINS`).
- `bookwalker/` + `ebookjapan.yahoo.co.jp/` — Puppeteer headless scrapers that
  drive the same session protocol from Node (cookie-authenticated, see their
  local READMEs).

Both upload by default (`upload_to_mega=true`) to keep the old
capture→OCR→MEGA behavior. The `bookwalker/README.md` files document their
usage.

## Security notes

- The bridge binds to **127.0.0.1 only** by default. It is a local tool; don't
  expose it to a network without adding authentication.
- MEGA credentials live in the macOS Keychain, a chmod-600 file, or your
  environment — never in this repository. The temporary `.megarc` files
  written during uploads are deleted afterwards.
- CORS is an allow-list, not `*`. `page-local` ingest is restricted to your
  home directory and system temp paths.
- Session/persisted files under the work dir are removed on finalize
  (`delete_after_upload=true`).

## Troubleshooting

**`mokuro_installed: false` in /health**
Install mokuro (`pip install mokuro`) or point `MOKURO_REPO` at a checkout.

**`mega_configured: false`**
Run `python3 server.py --setup-mega`, export `MEGA_EMAIL`/`MEGA_PASSWORD`,
or (macOS) `./setup-keychain.sh`.

**OCR is slow**
Normal without a GPU. Raise `OCR_CHUNK_SIZE`/`OCR_IDLE_FLUSH_S` or use a
GPU/MPS-capable mokuro build via `MOKURO_REPO`. First run downloads models.

**Upload fails with `partial_upload`**
Check the `stderr` in the NDJSON error frame. Ensure `/mokuro-reader` (or your
`MEGA_LIBRARY_ROOT`) is creatable by your account — the bridge tries to create
it automatically.

**Port 8765 already in use**
Old launchd agent? See `install-launchd.sh` (it unloads the legacy
`com.bw-mokuro-bridge` label automatically), or kill the stale process.

## Development

```bash
python3 -m py_compile server.py          # syntax check
./run.sh                                 # run with default config
UVICORN_RELOAD=1 python3 server.py       # dev auto-reload
```

## License

MIT — see [LICENSE](LICENSE).
