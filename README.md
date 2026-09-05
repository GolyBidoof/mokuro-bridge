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

Output stays on your machine by default; uploading to your own cloud is
optional — **MEGA, Google Drive or OneDrive**.
Nothing about the OCR touches the cloud — it all runs locally, and it works on
macOS, Windows and Linux. It's built around **Japanese manga**: mokuro's OCR
model reads Japanese text, and the input is ordinary page images (`.jpg`,
`.png` or `.webp`).

---

## Quickstart — just get it running

No architecture knowledge needed. Two terminals — one for the server
(step 2), one for the OCR command (step 3).

**1. Install (once)**

```bash
git clone https://github.com/GolyBidoof/mokuro-bridge
cd mokuro-bridge
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

That one `pip install` brings in everything the server needs *and* the OCR
engine: `fastapi`, `uvicorn`, `python-multipart`, `keyring` (for OS keychain
support) and `mokuro` (pulls PyTorch — the big one; first install takes a
while and a few GB of disk).

**Cloud uploads are opt-in, including their dependencies.** The base install
has none of the cloud libraries. Each provider's setup wizard will detect the
missing packages and offer to `pip install` them for you when you run
`python server.py --setup-upload <mega|drive|onedrive>` (or install
them yourself with the matching `requirements-*.txt`).

**You need Python 3.10+.** That's mokuro's floor, and the very newest Python
release may not work yet — PyTorch wheels often lag new Python versions. If
`pip install` fails on `torch`, install a slightly older Python.

> **Windows:** replace `python3` with `python` (or `py`) throughout, create
> the venv with `py -m venv .venv`, and activate it with
> `.venv\Scripts\activate` — in **every new terminal** you open.

> **Which mokuro engine?** The bridge works with either the stock PyPI
> `mokuro` package or [a custom mokuro fork](https://github.com/GolyBidoof/mokuro)
> with a faster batch OCR API. The fork is the **recommended** choice — see
> [Choosing a mokuro engine](#choosing-a-mokuro-engine). For a quick start,
> the stock package is fine: keep the `mokuro` line above and skip ahead.
> To use the fork, leave the `mokuro` line out and follow that section.

**2. Start the bridge**

```bash
# macOS / Linux:
./run.sh
# Windows:
python server.py
```

You'll see `mokuro-bridge v0.3.0 on http://127.0.0.1:62642`.

**3. OCR a folder of pages you already have**

In a **second terminal** (the first is running the server), OCR a folder of
page images as a single volume:

```bash
# macOS / Linux:
python3 ocr_folder.py "/path/to/my/manga pages" --title "My Manga 1巻"
# Windows:
python ocr_folder.py "C:\path\to\my manga pages" --title "My Manga 1巻"
```

All images in the folder (`.jpg`, `.png` or `.webp`) are treated as one
volume; the folder name is the default title. `ocr_folder.py` only uses the
standard library, so you don't need the venv active in this terminal. The
first run downloads the OCR model and can look stalled for a few minutes; then
it streams progress and prints where the finished volume landed — by default
`output/My Manga/My Manga 1巻.{cbz,mokuro,webp}`.

**4. Read it**

[reader.mokuro.app](https://reader.mokuro.app/) is the web reader for mokuro
output: it shows each page alongside its OCR text (selectable, copyable), and
it understands the `.cbz` archives and `.mokuro` files this bridge produces.
There are two ways to get your volumes in:

- **Local import — desktop Chromium only** (Chrome, Edge, Brave, Opera):
  drag a series folder from `output/` straight into the app, or use its
  local-folder import in settings. This needs a folder-picker that only
  Chromium-based desktop browsers expose to websites — Safari and Firefox
  can't do it.
- **Cloud import — any browser:** connect the matching cloud account inside
  the reader and open its `mokuro-reader` folder (see next section). This is
  also the way to read on a phone or tablet.

That's the whole loop. Prefer scripting your own capture? Jump to
[Writing a capture client](#writing-a-capture-client) — four HTTP calls.

---

## Uploading to a cloud drive (optional)

Remote upload is **off by default**. The bridge has a generic *upload method*
system — `local` (default) or a configured remote — and `/upload-methods`
lists what's configured. `ocr_folder.py` accepts
`--upload-method local|mega|drive|onedrive` (and `--list-methods` to
print what the bridge reports); the HTTP API accepts `upload_method=` on
`/session/{id}/finalize` (legacy `upload_to_mega=true` still means `mega`).

**Sticky defaults.** Whichever method (and, for local, whichever `local_dir`)
a client *explicitly* asks for on a finalize is remembered and becomes the
default for later requests — until another explicit choice replaces it. The
choice persists across restarts in `<work>/upload_method_default.json` and
`<work>/local_dir_default.json` (0600). Requests that omit the field just use
the current default and never change it.

### MEGA

1. **Install megatools** for your OS: macOS `brew install megatools`,
   Debian/Ubuntu `apt install megatools`, others see
   [megatools.megous.com](https://megatools.megous.com/).
2. **Give the bridge your MEGA credentials** — one of these (first wins):
   - **Setup wizard (all platforms):** `python server.py --setup-upload mega`
     (the older `--setup-mega` still works) — asks once, then stores in your
     **OS keychain / credential store**: macOS Keychain, Windows Credential
     Manager, or Linux Secret Service (gnome-keyring). If no keychain is
     available it falls back to a permissions-restricted file
     (`~/.config/mokuro-bridge/credentials.env`).
   - **Environment variables:** `MEGA_EMAIL=you@example.com MEGA_PASSWORD=…`
     before starting the bridge.
   - **macOS-only helper script:** `./setup-keychain.sh` (writes the macOS
     Keychain directly).
3. **Upload** with `--upload-method mega` (or `MOKURO_BRIDGE_UPLOAD_DEFAULT=true`
   to make it the default).

### Google Drive

1. **Create a free Google OAuth client (one time, ~2 min)** and then sign in —
   the wizard does the rest:
   ```bash
   python server.py --setup-upload drive
   ```
   It prints the exact steps (Google Cloud Console → Credentials → create a
   **Desktop app** OAuth client). You paste the **Client ID** and **Client
   secret** (no `client_secrets.json` file needed — PKCE still protects the
   flow, but Google requires the secret at token exchange). The wizard
   verifies with Google that the client is valid, opens your browser once so
   you can sign in to the account whose Drive you want to use, and stores a
   refresh token at `~/.config/mokuro-bridge/drive_credentials.json` (0600) —
   the client secret itself is never saved. It installs the Google client
   libraries when needed (or run `pip install -r requirements-drive.txt`
   yourself).

   > **Why a one-time client?** Google only lets an OAuth client run in the
   > project that registered it — a client shared across users fails with
   > `401 invalid_client`. So each user needs their own (free, ~2 min). The
   > wizard pre-checks your pasted ID+secret so a typo gives a clear message
   > instead of a raw Google error page.

   - **Alternative:** set `DRIVE_CLIENT_ID=<your client id>` and
     `DRIVE_CLIENT_SECRET=<your client secret>` (or point
     `DRIVE_CLIENT_SECRET_FILE` at a downloaded `client_secrets.json`) to skip
     the paste step.
   - **Service account:** save a service-account JSON at that same
     `DRIVE_CREDS_FILE` path. Note: files land in the service account's own
     Drive, which you must share with your account (or use domain-wide
     delegation on Workspace).
2. **Upload** with `--upload-method drive`.

### OneDrive

1. **Register a small Azure app** (one time, ~2 min): [Azure portal → App
   registrations → New registration](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
   — name it anything; under **Authentication → Add platform → Mobile and
   desktop applications**, tick `https://login.microsoftonline.com/common/oauth2/nativeclient`; under
   **API permissions** add Microsoft Graph **delegated** `Files.ReadWrite`;
   set the app to allow public client flows. Copy the **Application (client) ID**.
2. **Log in** — the easy headless way, no redirect URI or secret. The wizard
   installs `msal` + `requests` when needed (or run
   `pip install -r requirements-onedrive.txt` yourself):
   ```bash
   export ONEDRIVE_CLIENT_ID=<your app client id>
   python server.py --setup-upload onedrive
   ```
   It prints a URL + code; open it, sign in, paste the code. The token is
   stored (0600) and auto-refreshes. Re-run only when it expires.
3. **Upload** with `--upload-method onedrive`.

### Where things land

Every provider stores a finished volume under a `mokuro-reader` library
folder in that provider's root — the exact layout reader.mokuro.app scans:

```
mokuro-reader/<series>/
  <volume>.cbz  <volume>.mokuro  <volume>.webp
```

- MEGA → `/Root/mokuro-reader/<series>/`
- Google Drive → `mokuro-reader/<series>/` at the top of My Drive
- OneDrive → `mokuro-reader/<series>/` at the top of your OneDrive
- local → `<MOKURO_BRIDGE_OUTPUT_DIR>/<series>/` (or the `local_dir` you pass)

`GET /upload-methods` reports all of this as JSON:
`methods[]` (each with `id`, `name`, `configured`, `default`, provider info
like `creds_source`, and `current_folder` — where that method writes),
plus `upload_method_default`.

---

## Choosing a mokuro engine

The bridge's OCR is powered by [mokuro](https://github.com/kha-white/mokuro).
There are two ways to get it, and they differ in speed:

### 1. Recommended: the GolyBidoof mokuro fork

[github.com/GolyBidoof/mokuro](https://github.com/GolyBidoof/mokuro) is a
maintained fork of mokuro that adds a **batch OCR API**: instead of running
page-by-page, it detects text across the whole volume first, then recognizes
all crops in batched passes. The bridge detects this fork's API at runtime
and uses it automatically.

**Why the fork is worth it:**
- **Faster volume OCR** — batching avoids per-page model round-trips and
  keeps the GPU/CPU busy, which matters most for long volumes.
- **Live per-page progress** — the fork exposes a `detect_and_extract` /
  batched `recognize_text` flow that the bridge streams progress from, so you
  see OCR advance page-by-page instead of a single long wait.
- **Actively refined** — the fork includes optimizations that are not in the
  PyPI release (see the fork's README for the full optimization summary).

**Install (one time):**
```bash
git clone https://github.com/GolyBidoof/mokuro
# skip the `mokuro` line in requirements.txt — the fork is used instead
```
Point the bridge at it:
```bash
# export this before starting the bridge (or set it in your .env)
export MOKURO_REPO=/path/to/GolyBidoof/mokuro
./run.sh
```
The startup banner prints the mokuro path and whether the fork API is in use;
`/health` reports `mokuro_custom_fork: true` and `mokuro_fork_api: true`.

### 2. Simpler: stock mokuro from PyPI

Just keep `mokuro` in `requirements.txt`:
```bash
pip install mokuro
```
No `MOKURO_REPO` needed. This uses the upstream release — same output
format, but OCR runs per-page without the fork's batching, so long volumes
take longer. Use this if you want zero setup or prefer the upstream package.

> **Both produce identical `.mokuro`/`.cbz`/`.webp` output** — the reader,
> the upload providers and your capture scripts don't care which engine you
> chose. You can switch at any time by changing `MOKURO_REPO` / reinstalling.

---

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
│   5. keep locally and/or upload to a cloud method  │
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

---

## Configuration

Everything is environment variables — the server does **not** read a `.env`
file by itself. Copy `.env.example` to `.env`, uncomment what you need, then
load it in the shell before starting the server:

```bash
set -a; source .env; set +a        # macOS / Linux
```

…or just `export` the variables in your shell/launcher.

| Variable | Default | Purpose |
|---|---|---|
| `MOKURO_BRIDGE_HOST` / `MOKURO_BRIDGE_PORT` | `127.0.0.1` / `62642` (spells "MANGA" on a phone keypad 🙂) | Bind address. Keep loopback unless you know why not. |
| `MOKURO_BRIDGE_WORK_DIR` | `~/mokuro-input` | Scratch space: page images + OCR JSON mid-session. |
| `MOKURO_BRIDGE_OUTPUT_DIR` | `<repo>/output` | Where finished volumes land when not uploading to MEGA. |
| `MOKURO_BRIDGE_UPLOAD_DEFAULT` | `false` | `true` = finalize uploads to MEGA unless told otherwise. |
| `CORS_ORIGINS` | The four BookWalker web viewers — `viewer`, `viewer-trial`, `viewer-ptrial`, `viewer-subscription` (`*.bookwalker.jp`) | Comma-separated origins allowed to POST from the browser (userscripts). |
| `MOKURO_REPO` | *(none)* | Path to a mokuro checkout to use instead of the installed package. |
| `MEGA_LIBRARY_ROOT` | `/Root/mokuro-reader` | Remote MEGA folder that receives series folders. |
| `MEGA_EMAIL` / `MEGA_PASSWORD` | *(none)* | MEGA credentials (alternative to the setup wizard). |
| `MEGA_CREDS_FILE` | `~/.config/mokuro-bridge/credentials.env` | Credentials file used when env vars are absent and no OS keychain entry exists. |
| `DRIVE_ROOT_NAME` | `mokuro-reader` | Google Drive folder (at My Drive root) that receives series folders. |
| `DRIVE_CREDS_FILE` | `~/.config/mokuro-bridge/drive_credentials.json` | Google OAuth token or service-account JSON (0600). |
| `DRIVE_CLIENT_ID` | *(none)* | Your Google Cloud OAuth client ID (Desktop app) — used by `--setup-upload drive` instead of asking you to paste it. |
| `DRIVE_CLIENT_SECRET` | *(none)* | Client secret for the above (required by Google's token endpoint; never stored). |
| `DRIVE_CLIENT_SECRET_FILE` | *(none)* | Optional: path to a downloaded Google OAuth `client_secrets.json` (takes precedence over the ID/secret env vars). |
| `ONEDRIVE_CLIENT_ID` | *(none)* | Azure app (public client) ID for OneDrive. |
| `ONEDRIVE_ROOT_NAME` | `mokuro-reader` | OneDrive folder (at your OneDrive root) that receives series folders. |
| `ONEDRIVE_TOKEN_FILE` | `~/.config/mokuro-bridge/onedrive_token.json` | msal token cache (0600). |
| `OCR_CHUNK_SIZE` | `8` | Pages per OCR batch (tune for your GPU/CPU). |
| `OCR_IDLE_FLUSH_S` | `1.5` | Seconds to wait for a fuller batch before flushing. |
| `MIN_PAGES_FOR_MEGA` | `10` | Refuse remote upload below this many pages (failed-scrape guard). |
| `UVICORN_RELOAD` | `0` | Dev auto-reload (wipes in-memory sessions on change). |

---

## HTTP API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Dependency/config status (mokuro, engines, creds, upload methods…). |
| `GET` | `/upload-methods` | Configured upload methods + their current folder (JSON). |
| `POST` | `/session/start` | `title`, `reuse_existing` → new session id. |
| `POST` | `/session/resume` | `title`, `source_dir` — OCR a folder on disk (what `ocr_folder.py` uses). |
| `POST` | `/session/{id}/page` | Multipart `page` image + `filename` (browser capture). |
| `POST` | `/session/{id}/page-local` | `path` — ingest an image already on this machine (headless scrapers). |
| `GET` | `/session/{id}/status` | Capture/OCR progress snapshot. |
| `GET` | `/sessions` | All live sessions. |
| `POST` | `/session/{id}/finalize` | `upload_method`, `local_dir`, `delete_after_upload` → NDJSON progress stream. |

### `finalize` form fields

- `upload_method` — destination: `local` (default), `mega`, `drive`, or
  `onedrive`. Unset → falls back to the legacy `upload_to_mega`, then the
  `MOKURO_BRIDGE_UPLOAD_DEFAULT` env var.
- `local_dir` — when `upload_method=local`, write the finished volume into
  this folder instead of the default output dir. Ignored for remote methods.
- `upload_to_mega` — legacy alias; `true` → MEGA, `false` → local.
- `delete_after_upload` — `true` (default) removes the session's working files
  after a successful run (in remote mode that's everything; in local mode the
  finished trio in the output dir is kept).

### Upload methods

`GET /upload-methods` returns what a client can target before it uploads:

```json
{"upload_method_default":"local","upload_method_selected":null,
 "methods":[
   {"id":"local","name":"Local output directory","configured":true,
    "default":true,"current_folder":"/Users/you/mokuro-bridge/output"},
   {"id":"mega","name":"MEGA (megatools)","configured":true,"default":false,
    "creds_source":"keychain","library_root":"/Root/mokuro-reader",
    "current_folder":"/Root/mokuro-reader"},
   {"id":"drive","name":"Google Drive","configured":false,"default":false,
    "creds_source":null,"root":"mokuro-reader",
    "current_folder":"mokuro-reader (My Drive root)"},
   {"id":"onedrive","name":"OneDrive","configured":false,"default":false,
    "creds_source":null,"root":"mokuro-reader",
    "current_folder":"mokuro-reader (OneDrive root)"}
 ]}
```

### Busy state

`GET /health` includes a live **busy flag** so a client can tell whether the
bridge is working on something, and what:

| Field | Value | Meaning |
|---|---|---|
| `busy` | `true` / `false` | Something is in progress right now (OCR or upload). |
| `busy_stage` | `"idle"`, `"ocr"`, `"uploading"` | What it's doing. |
| `busy_detail` | free text | e.g. `waiting for OCR: 12 pending`, `MEGA → /Root/mokuro-reader/<Series>`, `5 pages queued for OCR`. |

`busy_stage` is `"ocr"` while a finalize waits for/streams OCR, and
`"uploading"` while a volume uploads to a remote method; it returns to
`"idle"` when the finalize finishes (success or error). `busy` is also
`true` when pages are queued for OCR outside of a finalize
(`ocr_queue_depth > 0`).

A polling client can wait for a volume to finish by looping on `/health`
until `busy` is `false`, or by streaming the finalize NDJSON directly and
waiting for the `done`/`error` stage — the two are complementary.

**Polling cadence.** `/health` is cheap; while the bridge reports `busy:
true`, poll it fast (e.g. once a second) so a UI notices the instant the
bridge goes idle — then back off (e.g. 10 s) once `busy: false` returns.
While *you* are the one running the finalize, streaming its NDJSON (or
polling `/session/{id}/status`, below) is the tighter loop; `/health` is the
right way to watch background work started by another client (e.g.
`ocr_folder.py`).

While a remote upload runs, `GET /session/{id}/status` also carries a live
`upload` object (same schema as the `upload` field of `upload_progress`
events below) with the in-flight file's bytes/percent/speed plus the final
per-file `url` when it completes — handy for clients that poll instead of
streaming.

### Progress stream format

`/session/{id}/finalize` streams **NDJSON** — one JSON object per line. Every
line has a `stage` and `message`; `stage` is one of: `wait_ocr`, `ocr`,
`assemble`, `pack`, `upload`, `upload_progress`, `cleanup`, `done`, `error`.

`upload_progress` events are emitted **live** while bytes are actually
uploading (each file, every remote method) — the stream is not buffered until
the end, so a client reading the response body gets a smooth 0→100% walk per
file as the transfer progresses. They look like:

```json
{"stage":"upload_progress","message":"My Manga 1巻.cbz: 42.5%",
 "upload":{"file":"My Manga 1巻.cbz","bytes":12451840,"total_bytes":29125632,
           "current_bytes":12451840,"percent":42.5,"speed_bps":5452595,
           "speed_human":"5.2 MiB/s","method":"mega"},
 "current_bytes":12451840,"total_bytes":29125632,"percent":42.5,
 "speed_bps":5452595,"remote_path":"/Root/mokuro-reader/My Manga",
 "mega_path":"/Root/mokuro-reader/My Manga","method":"mega"}
```

- `upload` — per-file progress: `file`, `bytes`/`current_bytes` (uploaded so
  far), `total_bytes`, `percent` (0–100), `speed_bps`, `speed_human`, `method`.
  The top-level `current_bytes`/`total_bytes`/`percent`/`speed_bps` fields are
  mirrors of the same values for convenience.
- `remote_path` — where the file is going on that provider (`mega_path` is a
  legacy alias of the same value).

The final `done` event includes `status`, an `uploads` array (one entry per
file, mirroring the `upload` schema plus `duration_s`), and `remote_path`:

```json
{"stage":"done","message":"Done! 132 pages → MEGA /Root/mokuro-reader/My Manga/",
 "status":"success","method":"mega","remote_path":"/Root/mokuro-reader/My Manga",
 "uploads":[{"file":"My Manga 1巻.cbz","bytes":29125632,"total_bytes":29125632,
             "current_bytes":29125632,"percent":100.0,"speed_bps":5452595,
             "speed_human":"5.2 MiB/s","duration_s":5.3,"success":true},
            {"file":"My Manga 1巻.mokuro","bytes":4128768,"total_bytes":4128768,
             "current_bytes":4128768,"percent":100.0,"speed_bps":1032192,
             "speed_human":"984.4 KiB/s","duration_s":4.0,"success":true},
            {"file":"My Manga 1巻.webp","bytes":512000,"total_bytes":512000,
             "current_bytes":512000,"percent":100.0,"speed_bps":256000,
             "speed_human":"250.0 KiB/s","duration_s":2.0,"success":true}]}
```

On partial failure the stream ends with a `done` event carrying
`"status":"partial_upload"` and the `uploads` array shows which files failed.

---

## Writing a capture client

A client only needs four HTTP calls:

1. `POST /session/start` with a `title` → get `session_id`;
2. `POST /session/{id}/page` once per captured page (multipart image +
   filename);
3. optionally poll `GET /session/{id}/status`;
4. `POST /session/{id}/finalize` when capture finishes.

Storefront-specific capture scripts (browser userscripts, headless scrapers)
are intentionally **not** part of this repository — they embed account/session
handling. In browsers, a userscript can POST straight from the storefront
origin as long as that origin is in `CORS_ORIGINS`.

---

## Security

- The bridge binds to **127.0.0.1** by default and is a local tool — don't
  expose it to a network without adding authentication.
- Credentials are stored in your **OS keychain / credential store** (macOS
  Keychain, Windows Credential Manager, Linux Secret Service) or in
  permissions-restricted 0600 files under `~/.config/mokuro-bridge/` — never
  in this repository.
- `page-local` ingest only accepts paths under your home directory or system
  temp locations.
- CORS is an allow-list, not `*`. `CORS_ORIGINS` controls which storefront
  origins may POST from a browser userscript.
- After a successful finalize, the session's working files are removed
  (`delete_after_upload=true` default); in local mode the finished trio in the
  output dir is kept.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mokuro_installed: false` in `/health` | `pip install -r requirements.txt` should have installed mokuro. Otherwise `pip install mokuro` or set `MOKURO_REPO` to a checkout. |
| `mega_configured: false` | Run `python server.py --setup-upload mega` (stores in your OS keychain/credential store or a 0600 file), export `MEGA_EMAIL`/`MEGA_PASSWORD`, or use `./setup-keychain.sh` (macOS). On headless Linux, keychain storage needs a Secret Service daemon (gnome-keyring). |
| Upload fails with `partial_upload` | Check the `stderr` in the NDJSON error frame. Make sure the destination is creatable by your account — the bridge creates the `mokuro-reader` folder automatically. |
| OCR is slow | Normal without a GPU. Raise `OCR_CHUNK_SIZE` / `OCR_IDLE_FLUSH_S`, or use the batch-OCR fork via `MOKURO_REPO`. First run downloads the model. |
| Port `62642` already in use | Another process holds it. Stop it, or pick another port with `MOKURO_BRIDGE_PORT=62643 ./run.sh`. If an older launchd auto-start agent is running: `launchctl bootout gui/$(id -u)/com.mokuro-bridge` (macOS). |
| reader can't see your `output/` folder | Local import only works in desktop Chromium (Chrome, Edge, Brave, Opera). In Safari/Firefox, upload to a cloud provider and connect it inside the reader, or drag a single series folder into the app. |

---

## Development

```bash
python3 -m py_compile server.py        # syntax check
./run.sh                               # run with default config
UVICORN_RELOAD=1 python3 server.py     # dev auto-reload
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

Maintained by **GolyBidoof**. Developed and refined with the help of
**DeepSeek V4 Flash**.
