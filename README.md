# mokuro-bridge

**mokuro-bridge is where all three dokuha projects converge.** It is a small local service that runs mokuro OCR on finished pages and uploads the result, producing the `.cbz` / `.mokuro` / `.webp` trio that [reader.mokuro.app](https://reader.mokuro.app/) reads.

## The three projects

- **dokuha-cli** is the engine and the batch downloader. A Node CLI that downloads manga volumes from five stores (BookWalker, CMOA, ebookjapan, Kindle and k-manga) at full resolution, unscrambles them offline, and owns *series* downloads: give it a series URL and it finds the free volumes and previews itself.
- **dokuha-userscript** is the in-browser companion. What you can install today is the earlier userscript release, [bookwalker-ebookjapan-cmoa-native-downloader](https://github.com/GolyBidoof/bookwalker-ebookjapan-cmoa-native-downloader), which covers BookWalker, CMOA and ebookjapan: it downloads the single volume you already have open, with no install beyond a userscript manager. The rewrite that adds Kindle and k-manga is in development and not published yet.
- **mokuro-bridge** (this repository) is where all three converge. A small local service that runs mokuro OCR on finished pages and uploads the result to MEGA, Google Drive, OneDrive or a local folder, producing the `.cbz` / `.mokuro` / `.webp` trio that reader.mokuro.app reads. Both downloaders hand finished volumes to it, so OCR and upload are identical no matter which one produced the pages. It is also useful on its own for any client that can produce page images.

`dokuha-cli` is the batch downloader, `dokuha-userscript` is the browser companion, and this repository is the shared OCR and delivery service. Both downloaders call the same HTTP API here, so a volume captured headlessly and a volume captured in a browser are processed by exactly the same code.

## What this service is responsible for

The bridge owns one half of the capture pipeline and only that half: it never touches a storefront and holds no store account, cookie or store protocol. A client gets the images out however it likes and hands them over, which is why the bridge does not break when a store changes and why anything that can produce a page image can use it.

| | |
| --- | --- |
| **What it is** | A local FastAPI service: pages in, `.cbz` + `.mokuro` + `.webp` out |
| **Runs on** | macOS, Windows and Linux, Python 3.10 or newer |
| **Needs** | Any mokuro plus PyTorch for OCR; the fetch accelerator is in the base install |
| **Delivery** | A local folder by default; MEGA, Google Drive and OneDrive opt in |
| **Exposure** | Loopback only, unauthenticated, and meant to stay that way |

The output is always the same three files, under a per-series folder:

```
<output>/<series>/
  <volume>.cbz       page images
  <volume>.mokuro    OCR text and block data
  <volume>.webp      cover
```

A volume folder keeps a clean name unless that name is genuinely taken, in which case the bridge adds a short random suffix (`<volume>_a1b2c3`) so two different volumes never share a directory.

## Where it converges with the other two

dokuha-cli hands over a folder of finished pages with `--mokuro`. dokuha-userscript streams each captured page to the bridge as it downloads, so OCR overlaps the capture. Both end at the same `POST /session/{id}/finalize`, which assembles the trio, uploads it and reports progress as NDJSON on the same connection.

The three projects are documented together under [docs/clients.md](docs/clients.md), which gives the exact request sequence each client uses.

## Key advantages

- **Store-agnostic.** The bridge knows nothing about any store, so a storefront redesign cannot break OCR or delivery.
- **OCR runs as pages arrive**, in chunks of 8 with an idle flush after 1.5 s, so a long volume is not a capture followed by a separate wait.
- **Twelve HTTP routes** cover capture, resume, status, queue, health and destination checks; the full table is in [docs/endpoints.md](docs/endpoints.md).
- **Named destinations.** Each provider can hold several accounts (`mega`, `mega:work`, `drive:personal`), each with its own remote root and credentials. See [docs/destinations.md](docs/destinations.md).
- **A sticky default.** Once any client names a destination explicitly, that choice becomes the default for every later request, including from other clients, until another explicit choice replaces it.
- **A page-fetch accelerator for the browser companion.** A browser allows six connections per origin, and the port is part of the origin, so the bridge opens 48 extra localhost ports by default and lists them on `/health`. Each is worth six more sockets, so the userscript gets 288 extra lanes with nothing to configure.
- **Safe by default.** It binds to `127.0.0.1`, refuses to read outside your home and the system temp locations, and stages uploads inside the work volume.

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

Without an engine the bridge still runs, `/health` reports `mokuro_installed: false`, and only OCR is unavailable. An upload provider's SDK also stays out of the base install, and its setup wizard offers to install the right requirements file when one is missing. `pip install "mokuro-bridge[cloud]"` adds both Google Drive and OneDrive without PyTorch.

The fork is the faster engine, and `mokuro-bridge --install-mokuro` replaces whichever `mokuro` is installed in the environment that runs the service, then verifies the import. `/health` reports which engine is loaded as `mokuro_flavor`. The exact commands, how to go back to upstream, and the per-environment caveat are in [docs/mokuro.md](docs/mokuro.md).

## Quick start

Start the bridge, then OCR a folder of pages you already have:

```bash
mokuro-bridge-ocr ./my-volume/ --title "Volume title"
```

Or let a client drive the whole pipeline, folder hand-off first and streaming capture second:

```bash
dokuha --mokuro 'https://bookwalker.jp/de00000000-0000-4000-8000-000000000001/'
```

Configure a cloud destination once and it stays the default:

```bash
mokuro-bridge --setup-upload mega
mokuro-bridge --list-uploads
```

On macOS, `./install-launchd.sh` installs a launchd agent that starts the bridge at login. When a newer release exists, the service logs one line about it at startup, and `mokuro-bridge --check-update` reports it without changing anything (exit 0 up to date, 1 update available, 2 check failed). `mokuro-bridge --update` is the one command that acts: it detects whether this copy came from pipx, `uv tool`, a plain pip install or a git checkout, prints the exact upgrade command, runs it and prints how to restart the service. Nothing restarts by itself, because a restart mid-OCR loses work.

The bridge is unauthenticated and binds to `127.0.0.1`, which is the whole access control. A library outside your home directory needs `MOKURO_BRIDGE_INGEST_ROOTS`. CORS, the fetch allowlist and the file-descriptor cost of 48 proxy ports are covered in [docs/architecture.md](docs/architecture.md).

## Docs

| | |
| --- | --- |
| [docs/endpoints.md](docs/endpoints.md) | Every HTTP route: method, path, form fields, response shape, error codes and test coverage |
| [docs/destinations.md](docs/destinations.md) | Upload targets, named accounts, credential stores and the sticky default |
| [docs/clients.md](docs/clients.md) | How dokuha-cli and dokuha-userscript drive the bridge, with the exact request sequence |
| [docs/architecture.md](docs/architecture.md) | Request lifecycle, sessions, scheduling, the fetch proxy and the security posture |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Install failures, missing OCR, file-descriptor limits, 403s |
| [docs/mokuro.md](docs/mokuro.md) | The faster mokuro fork: why it is faster, the one install command, going back to upstream |
| [CHANGELOG.md](CHANGELOG.md) | What changed, release by release |
| [Releases](https://github.com/GolyBidoof/mokuro-bridge/releases) | The prebuilt wheel and sdist |

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest        # 292 tests, about 10s, hermetic
python3 -m ruff check .
```

## Credits and licence

MIT, see [LICENSE](LICENSE).

OCR runs on [the mokuro fork](https://github.com/GolyBidoof/mokuro), which adds batched recognition and is the recommended engine. It forks [kha-white's original mokuro](https://github.com/kha-white/mokuro), still supported as the slower fallback under its own licence. Cloud delivery uses the MEGA, Google Drive and OneDrive clients, each under its own terms.

The code was written by DeepSeek V4.1 Flash.
