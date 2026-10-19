# mokuro-bridge

**mokuro-bridge is where all three dokuha projects converge.** It is a small local service that runs mokuro OCR on finished pages and uploads the result, producing the `.cbz` / `.mokuro` / `.webp` trio that [reader.mokuro.app](https://reader.mokuro.app/) reads.

## The three projects

- **dokuha-cli** is the engine and the batch downloader. A Node CLI that downloads manga volumes from five stores (BookWalker, CMOA, ebookjapan, Kindle and k-manga) at full resolution, unscrambles them offline, and owns *series* downloads: give it a series URL and it finds the free volumes and previews itself.
- **dokuha-userscript** is the in-browser companion. What you can install today is the earlier userscript release, [bookwalker-ebookjapan-cmoa-native-downloader](https://github.com/GolyBidoof/bookwalker-ebookjapan-cmoa-native-downloader), which covers BookWalker, CMOA and ebookjapan: it downloads the single volume you already have open, with no install beyond a userscript manager. The rewrite that adds Kindle and k-manga is in development and not published yet.
- **mokuro-bridge** (this repository) runs mokuro OCR on finished pages and uploads the result to MEGA, Google Drive, OneDrive, any WebDAV server or a local folder. Both downloaders hand finished volumes to it, so OCR and upload are identical no matter which one produced the pages, and it is useful on its own for any client that can produce page images. They end at the same `POST /session/{id}/finalize`, which assembles the volume files and the series index, uploads them and reports progress as NDJSON on the same connection; [docs/clients.md](docs/clients.md) gives the exact request sequence each client uses.

## What this service is responsible for

The bridge owns one half of the capture pipeline and only that half: it never touches a storefront and holds no store account, cookie or store protocol. A client gets the images out however it likes and hands them over, which is why the bridge does not break when a store changes and why anything that can produce a page image can use it. The output is always the same files, under a per-series folder.

| | |
| --- | --- |
| **What it is** | A local FastAPI service: pages in, `.cbz` + `.mokuro` + `.webp` + `series.json` out |
| **Runs on** | macOS, Windows and Linux, Python 3.10 or newer |
| **Needs** | Any mokuro plus PyTorch for OCR; the fetch accelerator is in the base install |
| **Delivery** | A local folder by default; MEGA, Google Drive, OneDrive and WebDAV opt in |
| **Exposure** | Loopback only, unauthenticated, and meant to stay that way |

```
<output>/<series>/
  series.json        the reader's per-series index, one per series folder
  <volume>.cbz       page images
  <volume>.mokuro    OCR text and block data
  <volume>.webp      cover
```

`series.json` is what keeps a library uploaded by a script visible in reader.mokuro.app, which only builds that index itself for series it already knows locally. The bridge writes it for the first volume and merges each later volume into it. The format is [Gnathonic's mokuro-reader](https://github.com/Gnathonic/mokuro-reader) (GPL-3.0), pinned by commit rather than vendored: see [docs/series-json.md](docs/series-json.md). A volume folder keeps a clean name unless that name is genuinely taken, in which case the bridge adds a short random suffix (`<volume>_a1b2c3`).

## Dashboard, accounts and live status

The bridge serves its own configuration dashboard at the address it prints at startup, `http://127.0.0.1:62642/`. It is one self-contained page, styled after reader.mokuro.app, with an Auto/Light/Dark switch that follows the operating system by default and is remembered in the browser. It shows the live status, the pages waiting to be read, the volumes reading now, the destination it files to, a live browser acceleration row with the open fetch-proxy ports, and one card per upload account with its alias, provider, note, folder and state; an Open reader link sits in the header.

Accounts are added and managed in the page: MEGA with an email and password, Google Drive through Google's sign-in, OneDrive through Microsoft's device-code flow, WebDAV with a server URL and Basic credentials, with reconnect, rename, remove, set default and folder browsing on each account card. Each account gets a memorable alias such as `mega`, `mega-2` or `drive-next`; a dokuha script selects one with `upload_method=mega-work`. Historical ids (`mega:work`, `drive:main`) keep resolving, so old scripts do not break. Three preferences cover the browser downloader's extra ports, the fallback local output folder and extra read roots; everything else stays in `.env.example`. See [docs/dashboard.md](docs/dashboard.md).

For a service running in the background, `mokuro-bridge --status` draws the same picture in a terminal: destinations with the default starred, the OCR queue, the pipeline and worker, the proxy ports, a live upload progress bar when one is active, and `?` for help. Keys are `r` refresh, `c` dashboard address, `?` help, `q` quit. Piped output is one full-height frame with no escape sequences, so `mokuro-bridge --status | head -30` is a snapshot you can paste into an issue.

## Key advantages

- **Store-agnostic.** The bridge knows nothing about any store, so a storefront redesign cannot break OCR or delivery.
- **OCR runs as pages arrive**, in batches with an idle flush, so a long volume is not a capture followed by a separate wait.
- **A dashboard and a terminal status display.** Accounts, folders, the default destination and three preferences are editable in the page the bridge serves, with browser setup for every provider; `mokuro-bridge --status` draws the same state for a background service.
- **Named destinations with aliases.** Each provider can hold any number of accounts (`mega`, `mega-2`, `drive-next`), each with its own remote root and credentials, and scripts select one by alias while legacy ids keep working.
- **WebDAV, for a server you already run.** Nextcloud, ownCloud, Synology, a NAS or any other WebDAV collection is a destination: give the page a server URL and Basic credentials and the bridge creates `<root>/<series>/` and uploads the same four files it puts anywhere else.
- **A sticky default.** Once any client names a destination explicitly, that choice becomes the default for every later request, including from other clients, until another explicit choice replaces it.
- **Twenty-nine HTTP routes** cover capture, resume, status, queue, health, destination checks, configuration and browser account setup; the full table is in [docs/endpoints.md](docs/endpoints.md).
- **A page-fetch accelerator for the browser companion.** A browser allows six connections per origin, and the port is part of the origin, so the bridge opens 48 extra localhost ports by default and lists them on `/health`. Each is worth six more sockets, so the userscript gets 288 extra lanes with nothing to configure. The numbers, the browser-side limits and when to change them are in [docs/browser-acceleration.md](docs/browser-acceleration.md).
- **Safe by default.** It binds to `127.0.0.1`, refuses to read outside your home and the system temp locations, stages uploads inside the work volume, and gates configuration writes to loopback origins.

## Use cases

- **Headless batch capture.** Run dokuha-cli over a series with `--mokuro` and come back to a finished library with OCR text, covers and cloud copies.
- **One volume from the browser.** Use the userscript on a page you already have open; the bridge supplies the extra fetch lanes, the OCR and the upload.
- **OCR a folder you already have.** `mokuro-bridge-ocr ./my-volume/ --title "Volume title"` drives a running bridge over HTTP.
- **Any other client.** Anything that can POST page images can use the bridge; there is no store-specific code to satisfy.

## Install

Install with [pipx](https://pipx.pypa.io/) or `uv tool install`, which keeps the bridge in its own environment:

```bash
pipx install mokuro-bridge
mokuro-bridge
```

Or from a checkout:

```bash
git clone https://github.com/GolyBidoof/mokuro-bridge && cd mokuro-bridge
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
./run.sh
```

The base install is only `fastapi`, `uvicorn`, `python-multipart`, `httpx` and `keyring`. The OCR engine is optional because it pulls in PyTorch, which is several GB. Either engine works:

```bash
pip install "mokuro @ git+https://github.com/GolyBidoof/mokuro"   # the fork, batched, about 1.8x
pip install -r requirements-ocr.txt                               # stock mokuro, slower
```

Without an engine the bridge still runs, `/health` reports `mokuro_installed: false`, and only OCR is unavailable. An upload provider's SDK also stays out of the base install, and the dashboard's Add destination names what is missing per provider, with a link to the console that creates the OAuth client or app registration. `pip install "mokuro-bridge[cloud]"` adds every upload provider (Google Drive, OneDrive and WebDAV) without PyTorch.

The fork is the faster engine, and `mokuro-bridge --install-mokuro` replaces whichever `mokuro` is installed in the environment that runs the service, then verifies the import. `/health` reports which engine is loaded as `mokuro_flavor`. The exact commands, how to go back to upstream, and the per-environment caveat are in [docs/mokuro.md](docs/mokuro.md).

## Quick start

Start the bridge, open `http://127.0.0.1:62642/`, and press Add destination. The page walks through the provider sign-in and folder choice, and prints the alias a script should pass. On macOS, `./install-launchd.sh` installs a launchd agent that starts the bridge at login.

If you prefer the terminal, the same accounts can be added there:

```bash
mokuro-bridge --setup-upload mega
mokuro-bridge --list-uploads
```

Then hand over pages, either a folder or a live capture:

```bash
mokuro-bridge-ocr ./my-volume/ --title "Volume title"
dokuha --mokuro 'https://bookwalker.jp/de00000000-0000-4000-8000-000000000001/'
```

When a newer release exists the service logs one line about it at startup, and `mokuro-bridge --check-update` reports it without changing anything (exit 0 up to date, 1 update available, 2 check failed). `mokuro-bridge --update` is the one command that acts: it detects whether this copy came from pipx, `uv tool`, a plain pip install or a git checkout, prints the exact upgrade command, runs it and prints how to restart the service. Nothing restarts by itself, because a restart mid-OCR loses work. The `series.json` format is upstream's, so the bridge pins it. `mokuro-bridge --check-series-reference` re-fetches the pinned files and reports whether they still match (exit 0 matching and current, 1 upstream moved, 2 check failed), and `mokuro-bridge --refresh-series-reference` re-pins them. The bridge is unauthenticated and binds to `127.0.0.1`, which is the whole access control. A library outside your home directory needs `MOKURO_BRIDGE_INGEST_ROOTS`. CORS, the fetch allowlist and the file-descriptor cost of 48 proxy ports are covered in [docs/architecture.md](docs/architecture.md).

## Docs

| | |
| --- | --- |
| [docs/dashboard.md](docs/dashboard.md) | The web dashboard, browser account setup, aliases, settings precedence, the terminal status display and the origin gate |
| [docs/browser-acceleration.md](docs/browser-acceleration.md) | The fetch-proxy ports, the browser connection limits behind them, and when to change the port count |
| [docs/series-json.md](docs/series-json.md) | The `series.json` sidecar: its format, why the uploader writes it, and the pin on upstream |
| [docs/endpoints.md](docs/endpoints.md) | Every HTTP route: method, path, form fields, response shape, error codes and test coverage |
| [docs/destinations.md](docs/destinations.md) | Upload targets, named accounts, credential stores and the sticky default |
| [docs/clients.md](docs/clients.md) | How dokuha-cli and dokuha-userscript drive the bridge, with the exact request sequence |
| [docs/architecture.md](docs/architecture.md) | Request lifecycle, sessions, scheduling, the fetch proxy and the security posture |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Install failures, missing OCR, dashboard and sign-in problems, settings that need a restart |
| [docs/mokuro.md](docs/mokuro.md) | The faster mokuro fork: why it is faster, the one install command, going back to upstream |
| [CHANGELOG.md](CHANGELOG.md) | What changed, release by release |
| [Releases](https://github.com/GolyBidoof/mokuro-bridge/releases) | The prebuilt wheel and sdist |

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest        # 558 tests, about 11s, hermetic
python3 -m ruff check .
```

## Credits and licence

MIT, see [LICENSE](LICENSE).

OCR runs on [the mokuro fork](https://github.com/GolyBidoof/mokuro), which adds batched recognition and is the recommended engine. It forks [kha-white's original mokuro](https://github.com/kha-white/mokuro), still supported as the slower fallback under its own licence. Cloud delivery uses the MEGA, Google Drive and OneDrive clients, each under its own terms, or any WebDAV server you already run. The `series.json` format is defined by [Gnathonic's mokuro-reader](https://github.com/Gnathonic/mokuro-reader) (GPL-3.0) and implemented a second time by [mokuro-bunko](https://github.com/Gnathonic/mokuro-bunko) (MPL-2.0). Neither codebase is copied here: this project pins the upstream files and ports the contract.

The code was written by DeepSeek V4.1 Flash.
