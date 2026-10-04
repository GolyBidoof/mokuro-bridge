# mokuro-bridge

**OCR and delivery for manga capture, plus the accelerator that makes the capture itself fast.**

I built this to fill one gap. OCR for manga is heavy (the model wants PyTorch) and
mokuro's own scripts assume you already have a folder of pages. The missing piece is
the bit in between: something that takes pages *as a download produces them*,
recognises them while they arrive, and writes the result where you read. Hand it pages,
get a `.mokuro` back.

The bridge is the OCR half of a capture pipeline, and only that half: it never touches
a storefront, and holds no store account, cookie or store code. A client gets the
images out however it likes and POSTs them here, which is why it does not break when a
store changes, and why anything that can produce a JPEG can use it.

## At a glance

| | |
| --- | --- |
| **What it is** | A local HTTP back end: POST pages in, get `.cbz` + `.mokuro` + cover out |
| **The accelerator** | Six browser sockets become 288, with nothing to configure |
| **Runs on** | macOS, Windows and Linux, Python 3.10 or newer |
| **Needs** | Any mokuro plus PyTorch for OCR; the accelerator is in the base install |
| **Delivery** | Local disk by default, MEGA / Google Drive / OneDrive opt-in |
| **Exposure** | Loopback only, unauthenticated, and meant to stay that way |

## What it does

**It makes browser capture fast.** A browser allows six connections per origin, and the
port is part of the origin, so the bridge opens 48 extra localhost ports and advertises
them on `/health`: 6 sockets become 288, with nothing to configure.

**It OCRs and delivers what you captured**, recognising pages as they arrive rather
than in one pass at the end, and writes what
[reader.mokuro.app](https://reader.mokuro.app/) reads:

```
<output>/<series>/
  <volume>.cbz       page images
  <volume>.mokuro    OCR text + block data
  <volume>.webp      cover
```

Two clients do the capturing:
[bookwalker-ebookjapan-cmoa-native-downloader](https://github.com/GolyBidoof/bookwalker-ebookjapan-cmoa-native-downloader)
in a browser, and [dokuha-cli](https://github.com/GolyBidoof/dokuha-cli) browserless.

## Install and run

Install with [pipx](https://pipx.pypa.io/) (or `uv tool install`), which keeps the
bridge in its own environment:

```bash
pipx install mokuro-bridge
mokuro-bridge
```

From a checkout:

```bash
git clone https://github.com/GolyBidoof/mokuro-bridge && cd mokuro-bridge
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
./run.sh              # or python server.py on Windows
```

The base install is small: `fastapi`, `uvicorn`, `python-multipart`, `httpx` and
`keyring`. To OCR a folder of pages you already have:

```bash
mokuro-bridge-ocr ./my-volume/ --title 'Volume title'
```

Or let a client drive the whole pipeline:

```sh
dokuha --mokuro 'https://bookwalker.jp/de00000000-0000-4000-8000-000000000001/'
```

## The OCR engine: any mokuro works

`mokuro` needs PyTorch, which is several GB, so it is not in the base requirements.
Without it the bridge still runs (`mokuro_installed: false` on `/health`) and only OCR
is unavailable. Either engine works:

```bash
pip install "mokuro @ git+https://github.com/GolyBidoof/mokuro"   # my fork, batched, ~1.8x
pip install -r requirements-ocr.txt                               # kha-white's original, slower
```

I recommend the fork: batched recognition is worth about 1.8x (roughly 23.5 ms per crop
against 43 with four beams), and recognition is the slowest part of a long volume.
Neither needs configuring, and without the batch API you get the slower single-crop
path.

## Where the output goes

Local disk by default. MEGA, Google Drive and OneDrive are opt-in, with more than one
account per provider (`mega:work`) and credentials in the OS keychain or 0600 files
under `~/.config/mokuro-bridge/`:

```bash
mokuro-bridge --setup-upload mega
mokuro-bridge --list-uploads
```

## Running it, and what it will not do

`./install-launchd.sh` installs a macOS launchd agent that starts on login. Nothing is
uploaded or updated without you asking: `--check-update` reports a newer release, and
applying it is your call because a restart mid-OCR loses work.

The bridge is unauthenticated and binds to `127.0.0.1`, which is the whole access
control. A library outside your home directory needs `MOKURO_BRIDGE_INGEST_ROOTS`; CORS,
the fetch allowlist and the file-descriptor cost of 48 ports are in
[docs/architecture.md](https://github.com/GolyBidoof/mokuro-bridge/blob/main/docs/architecture.md).

## Docs

| | |
| --- | --- |
| [docs/architecture.md](https://github.com/GolyBidoof/mokuro-bridge/blob/main/docs/architecture.md) | Request lifecycle, sessions, the API surface, security posture |
| [docs/troubleshooting.md](https://github.com/GolyBidoof/mokuro-bridge/blob/main/docs/troubleshooting.md) | Install failures, missing OCR, file-descriptor limits, 403s |
| [CHANGELOG.md](https://github.com/GolyBidoof/mokuro-bridge/blob/main/CHANGELOG.md) | What changed, release by release |
| [Releases](https://github.com/GolyBidoof/mokuro-bridge/releases) | The prebuilt wheel and sdist |

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest        # 197 tests, ~10s, hermetic
python3 -m ruff check .
```

## Credits and licence

MIT. See [LICENSE](https://github.com/GolyBidoof/mokuro-bridge/blob/main/LICENSE).

OCR runs on [my fork of mokuro](https://github.com/GolyBidoof/mokuro), which adds
batched recognition and is the engine I recommend. It forks
[kha-white's original mokuro](https://github.com/kha-white/mokuro), still supported as
the slower fallback under its own licence. Cloud delivery uses the MEGA, Google Drive
and OneDrive clients, each under its own terms.

The code was written by DeepSeek V4.1 Flash.
