# mokuro-bridge

**It makes your browser downloader up to 48x faster, and then OCRs what it caught.**

Two jobs, and you can take either without the other.

**1. A capture accelerator for browser clients.** A browser will not open more than
six connections to one origin, and an origin is scheme + host + **port**. A store's
page CDN is a single host that refuses HTTP/2, so an in-browser download is pinned
to six sockets no matter how fast the link is. This bridge opens a range of extra
localhost ports, each serving the same small proxy. The browser sees each as a fresh
origin and gets six more. Out of the box that is **6 sockets becomes 288**, with
nothing to configure.

**2. An OCR and delivery back end.** Point it at a folder of page images, or POST
pages from a capture script, and it runs [mokuro](https://github.com/kha-white/mokuro)
over them and produces the three files [reader.mokuro.app](https://reader.mokuro.app/) reads:

```
<output>/<series>/
  <volume>.cbz       page images
  <volume>.mokuro    OCR text + block data
  <volume>.webp      cover
```

Everything runs on your machine. Output stays local by default; **MEGA, Google Drive
or OneDrive** are optional. Works on macOS, Windows and Linux, and is built for
Japanese manga, because the model reads Japanese text.

## Why it exists

OCR for manga is heavy. The model wants PyTorch, which is several gigabytes, and
mokuro's own scripts are shaped for a person at a terminal who already has a folder
of pages. What is missing is the bit in between: something that accepts pages *as a
download produces them*, recognises them while they are still arriving, and puts
the result wherever you actually read it.

That is the whole job. Hand it pages, get a `.mokuro` back.

**It is the OCR half of a capture pipeline, and only that half.** The bridge never
touches a storefront: it holds no store account, no cookie, no store protocol and no
store code. A client gets the images out however it likes and POSTs them here. That
boundary is deliberate -- it is why the bridge does not break when a store changes,
and why any client that can produce a JPEG can use it.

Two working clients:

- **[bookwalker-ebookjapan-cmoa-native-downloader](https://github.com/GolyBidoof/bookwalker-ebookjapan-cmoa-native-downloader)**
  -- a userscript that captures pages in the browser and streams them here as it
  descrambles them. It uses **both** halves: the extra ports take its download from
  six sockets to 288, and the bridge OCRs and delivers what it caught.
- **[dokuha-cli](https://github.com/GolyBidoof/dokuha-cli)** -- a browserless CLI for
  BookWalker, CMOA, ebookjapan, Kindle and k-manga. No browser, so no six-socket
  ceiling to work around; it uses the OCR and delivery half, as two independent
  stages, so a volume waiting on a slow upload never stalls the network behind it.

## Six sockets to 288

A browser allows six concurrent HTTP/1.1 connections per **origin**, and an origin is
scheme + host + **port**. A store's page CDN is one host and refuses to negotiate
HTTP/2, so a viewer download is pinned to six sockets however fast the connection
is. No amount of page-level concurrency in the downloader gets past that; it is a
browser rule.

Because the port is part of the origin, the bridge opens a range of extra localhost
ports, each serving the same small proxy. The browser treats every port as a fresh
origin and so gets six more sockets per port, while the bridge itself does the
fetching under no browser limit at all.

```
mokuro-bridge v0.7.0 on http://127.0.0.1:62642
  fetch proxy: 48 extra port(s) 63443-63490  ->  288 browser sockets for the downloader
```

There is nothing to configure. The bridge binds what it can and advertises the range
on `/health` as `fetchProxyPorts`, and the client picks it up on its own. 48 is
chosen against Chrome's own ceiling: a profile gets roughly 300 sockets in total, so
48 ports leaves room for the page itself, the userscript and the downloader's
fallback lanes. `MOKURO_BRIDGE_FETCH_PORTS` raises or lowers it; `0` turns it off.

**You do not need the OCR engine to use this.** The accelerator is part of the base
server and is started regardless of whether mokuro is installed. A client that only
wants its browser download to stop being connection-bound can run the bridge and
ignore everything else.

**And downloading never depends on the bridge.** With it stopped, a client falls
back to its own page and background-context lanes. This is an accelerator, not a
dependency: it makes capture much faster, and nothing breaks without it.

One caveat the maintainer should know: each port costs a file descriptor on each
side of the bridge, so 48 ports at six sockets is roughly 600 at peak. The bridge
raises `RLIMIT_NOFILE` at startup to cover that, which matters on macOS, where a
launchd agent starts with a soft limit of 256 and exhausting it shows up as
`OSError: Too many open files` on `accept()` and ECONNRESET in the browser, with
nothing pointing at a ulimit. Lower `MOKURO_BRIDGE_FETCH_PORTS` instead if you would
rather not raise it.

Separately, pages are recognised **as they arrive** in chunked batches, so capture
and OCR overlap rather than running back to back: a 250-page volume is not a
250-page wait followed by a recognition pass.

## Quickstart

**1. Install.** A prebuilt wheel with [pipx](https://pipx.pypa.io/) (or `uv tool
install`) is the shortest route, and keeps the bridge in its own environment:

```bash
pipx install mokuro-bridge
```

From a checkout instead:

```bash
git clone https://github.com/GolyBidoof/mokuro-bridge && cd mokuro-bridge
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.10 or newer. The base install is small: `fastapi`, `uvicorn`,
`python-multipart`, `httpx` and `keyring`. Cloud libraries are opt-in per provider
and are not installed unless you ask for them.

**2. Start it.**

```bash
mokuro-bridge          # pipx / uv install
./run.sh               # macOS / Linux, from a checkout
python server.py       # Windows, from a checkout
```

```
mokuro-bridge v0.7.0 on http://127.0.0.1:62642
  fetch proxy: 48 extra port(s) 63443-63490  ->  288 browser sockets for the downloader
```

**3. OCR pages you already have.** In a second terminal:

```bash
mokuro-bridge-ocr ./my-volume/ --title 'Volume title'
```

Done. It writes the `.cbz`, the `.mokuro` and the cover, arranged per series, with
edition suffixes stripped so one series does not split across four folders.

To go straight from a download instead, let the client do it:

```sh
dokuha --mokuro 'https://bookwalker.jp/de00000000-0000-4000-8000-000000000001/'
```

## The OCR engine: install the fork

`mokuro` depends on PyTorch, which is several GB, so it is not in the base
requirements. The bridge runs without it -- `/health` reports
`mokuro_installed: false` and only OCR is unavailable. When you want OCR:

```bash
pip install "mokuro @ git+https://github.com/GolyBidoof/mokuro"
```

**That is the recommended engine, and it is one line.** It is
[GolyBidoof's fork of mokuro](https://github.com/GolyBidoof/mokuro), which adds a
batched recognition API. On a measured workload it is worth about **1.8x** -- about
23.5 ms per crop against 43 at one beam versus four, on MPS. Recognition is the
slowest part of a long volume, so this is the single largest speedup available to
you after the port trick.

Nothing else is needed. The bridge detects the fork's batch API by introspection
rather than by configuration, so installing it is enough -- there is no environment
variable to set and no flag to pass. `/health` will report the engine as installed
and the batch path active.

To develop on the fork itself, or to run an uninstalled checkout, set `MOKURO_REPO`
to its path and the bridge will put it ahead of anything installed:

```bash
git clone https://github.com/GolyBidoof/mokuro
export MOKURO_REPO="$PWD/mokuro"
```

**Stock mokuro from PyPI still works**, and is the fallback if you would rather not
track a git dependency:

```bash
pip install -r requirements-ocr.txt
```

The bridge uses the slower single-crop path with it. Nothing breaks; it is just
slower.

## Deliver it wherever you read

Local disk by default. MEGA, Google Drive and OneDrive are opt-in:

```bash
mokuro-bridge --setup-upload mega
mokuro-bridge --list-uploads
```

**More than one account per provider.** `--setup-upload mega --name work` adds a
second, addressed as `mega:work`, each with its own remote root and credentials. A
bare provider id still means the default, so nothing existing breaks.

Credentials go to the OS keychain where one exists (macOS Keychain, Windows
Credential Manager, Linux Secret Service), or to 0600 files under
`~/.config/mokuro-bridge/`. They are never written to this repository.

## Runs unattended

```bash
./install-launchd.sh      # macOS: a launchd agent that starts on login
```

`KeepAlive` with a throttle interval, so it comes back if it dies without
crash-looping. Nothing is uploaded or updated without you asking: `--check-update`
reports a newer release and prints the command for how *this* copy was installed,
and applying it stays your call, because a restart mid-OCR loses work.

## Being straight about where it is

- **The bridge is unauthenticated.** No token, no login. It binds to `127.0.0.1` and
  that is the entire access control. The intended deployment is loopback-only: do
  not bind it to a LAN address, because anyone who can reach the port can start a
  volume and have the result written or uploaded to your account.
- **CORS is a wildcard by default**, deliberately, since the client runs on whichever
  storefront you happen to be reading. `CORS_ORIGINS` narrows it.
- **There is no path traversal.** Page names are validated, the ingest resolver is
  confined to your home and the system temp locations, and uploads are
  extension-checked before anything is read. A caller can drive the bridge; it cannot
  read arbitrary files off your disk.
- **A library outside those roots needs one setting.** The local-ingest routes are
  confined to your home directory and the platform temp locations. On Windows that
  means `C:\Users\<you>\...`; a library on `D:\` is refused with a 403 until you
  add it:

  ```bash
  MOKURO_BRIDGE_INGEST_ROOTS='D:\manga' mokuro-bridge
  ```

  Comma-separated, absolute paths only. A relative entry is ignored rather than
  resolved against the working directory, so it cannot quietly widen the gate to
  wherever the server happened to start.
- **The page-fetch proxy is not restricted to one CDN by default.** It refuses
  loopback, private and link-local targets, so it cannot reach your network -- but
  any public host is fetchable. `MOKURO_BRIDGE_FETCH_ALLOWED_HOSTS` closes it to one.
- **The OCR engine lags Python.** PyTorch wheels often trail new releases.

## API

It is a small HTTP API, and anything that can POST an image can use it:

| | |
| --- | --- |
| `POST /session/start` | create a session for a volume title |
| `POST /session/{id}/page` | one page image |
| `POST /session/{id}/finalize` | run OCR, package, deliver; answers a progress stream |
| `POST /session/resume` | hand over a folder that already exists on disk |
| `GET /session/{id}/status` | poll progress |
| `GET /health` | readiness, engine state, bound ports, update status |

`finalize` streams newline-delimited JSON rather than returning one result, so a
client reads progress off the same connection doing the work. The full surface,
including the progress frame format and the upload methods, is in
[docs/architecture.md](https://github.com/GolyBidoof/mokuro-bridge/blob/main/docs/architecture.md).

## Development

```bash
pip install -r requirements-dev.txt
python3 -m pytest        # 195 tests, ~10s, hermetic
python3 -m ruff check .
```

No network, no credentials and no model are needed to run the suite. There is a
packaging contract test that fails if `pyproject.toml` and the `requirements*.txt`
files drift apart.

## Credits and licence

MIT. [LICENSE](https://github.com/GolyBidoof/mokuro-bridge/blob/main/LICENSE)

OCR is done by [mokuro](https://github.com/kha-white/mokuro), which carries its own
licence and is installed separately. Cloud delivery uses the MEGA, Google Drive and
OneDrive clients, each under its own terms.
