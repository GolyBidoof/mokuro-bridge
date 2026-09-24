# Changelog

## v0.5.1

The OCR engine is no longer a base dependency.

`mokuro` depends on PyTorch, so `pip install -r requirements.txt` pulled several
GB onto machines that only ever wanted page capture, the fetch accelerator or
uploads. The bridge already ran without an engine, with `/health` reporting
`mokuro_installed: false`, so this is a packaging fix rather than a behavioural
one.

### Changed

- `requirements.txt` is the server only now: fastapi, uvicorn, python-multipart,
  httpx and keyring.
- `mokuro` moved to the new `requirements-ocr.txt`, to install when you want OCR.
  A `MOKURO_REPO` checkout still works and needs neither file.
- README quickstart and troubleshooting updated: `mokuro_installed: false` is the
  expected state on a base install, not a fault to chase.

### Added

- `tests/test_optional_ocr.py` imports the API, the OCR module, the providers and
  `/health` with `mokuro` blocked on the meta path, and checks the fetch
  accelerator still answers.
- `tests/test_imports.py` walks the AST of every module and fails if anything
  outside the base requirements is imported at module level, so the OCR engine
  and the cloud SDKs cannot quietly creep back into the startup path.

## v0.5.0

Turns the bridge into a page-fetch accelerator for the downloader userscript.
Chrome allows 6 concurrent HTTP/1.1 connections per *origin*, and the page CDN
refuses HTTP/2, so a viewer download was pinned to 6 sockets. Since an origin
includes the port, the bridge now opens a range of extra localhost ports, each
worth another 6 sockets to the browser.

### Added

- **Multi-port fetch proxy** (`mokuro_bridge/fetchproxy.py`). Serves the CDN
  proxy on `MOKURO_BRIDGE_FETCH_PORTS` extra ports, 48 by default. The ports it
  manages to bind are advertised on `/health` as `fetchProxyPorts`, so the
  userscript discovers them with no configuration on either side.
- **File-descriptor headroom.** Every proxied page holds a descriptor on each
  side of the bridge, so 48 ports at 6 sockets is roughly 600 at once. The
  bridge raises `RLIMIT_NOFILE` at startup, which covers launchd, `run.sh` and
  manual runs alike. macOS hands a launchd agent a soft limit of 256, which
  previously surfaced as `OSError: Too many open files` on accept() and
  ECONNRESET in the browser, with nothing pointing at the ulimit.
- **Test suite** under `tests/`, run with `python3 -m pytest`. Covers port
  binding, the proxy's forwarding and streaming behaviour, the host allow-list
  guard, upstream failure handling, and the environment defaults. No mokuro,
  torch or network access required.

### Changed

- The startup banner reports the fetch-proxy port range and the socket count it
  buys, or says why the proxy is off.
- `httpx` is a direct dependency now rather than an incidental one.

### Notes

- `UVICORN_RELOAD=1` disables the fetch proxy: the reloader spawns a second
  process that would contend for the same ports.
- The proxy refuses any host outside its allow-list, so it cannot be used as an
  open proxy. Only the CDN host is reachable through it.
- Downloading never depends on the bridge. With it stopped, the userscript falls
  back to its own page and background-context lanes.
