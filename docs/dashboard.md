# Dashboard and status display

Two ways to see and change a running bridge without reading JSON by hand: a page it serves itself, and a live terminal display. Both read the same `/health` payload, so they never disagree about what the bridge is doing.

## The web dashboard

Open the address the service prints at startup:

```
http://127.0.0.1:62642/
```

It is one self-contained page: inline CSS and JavaScript, no external assets, no CDN, no font download. It works offline, and it is the surface to screenshot for a README or a release post.

| Panel | What it shows |
| --- | --- |
| Status pill | `idle` or the stage the bridge is in right now (`uploading`, `ocr`, `packaging`), from the same activity record `/health` reports |
| Cards | The engine in use and whether the batched fork API is present, the OCR queue and its chunk size, active sessions, open proxy ports and the browser sockets they add, and whether a newer release exists |
| Destinations | Every upload target with its provider, account, credentials source and **folder**, the default marked with a star, and buttons to browse, set as default, or remove it |
| Settings | Each configurable value with its current source and a `restart` badge where the value is read when the process starts |

### What the wizard changes

- **A destination's default upload folder.** Browse opens the provider's real folder tree and reads it through the provider's own API (a `megatools ls` for MEGA, `files.list` for Drive, the children endpoint for OneDrive). The folder you pick becomes that account's root, so later uploads land in `root/<series>/`.
- **The default destination.** The starred target receives every upload until a client names another one. This is the same sticky default the API already had, so the change applies to the next finalize rather than the next restart.
- **The default local output folder.** For `local` destinations, browse lists directories under your home directory and the system temp locations, the same boundary the bridge enforces when a client sends `local_dir`. New folder creates one there.
- **Settings.** Proxy port count, upstream fetch concurrency, the default output folder, extra read roots, the OCR chunk size and idle flush, and the minimum page count for a remote upload. These are read when the process starts, so the page marks them `restart` and shows the command that restarts the service.

Adding an account still happens in the terminal, because acquiring credentials is interactive: `mokuro-bridge --setup-upload drive --name work`. The page shows that command, and every account it sees afterward can be browsed and reconfigured from the page.

### Settings storage and precedence

The page writes to `~/.config/mokuro-bridge/settings.json` (override with `MOKURO_BRIDGE_SETTINGS_FILE`). Every reader asks in this order:

1. the environment variable, so a launchd plist or a shell export always wins,
2. the settings file, which is what the wizard writes,
3. the built-in default.

Each settings row in the page names its source, so a value that is pinned by the environment cannot be silently overridden. The file is plain JSON and hand-editable; it holds no secrets, which stay in the keychain or the provider credential files.

### Writes are gated to this machine

The bridge is unauthenticated and loopback-only, and `CORS_ORIGINS` defaults to `*`, so any page in your browser can already reach it. Configuration writes therefore check the `Origin` header: a request from a loopback origin (`http://127.0.0.1:*`, `http://localhost:*`, `http://[::1]:*`) or with no `Origin` at all (curl, the CLI) is accepted, and anything else gets `403`. Reads are not gated, so a monitoring script can still poll.

## The terminal status display

```
mokuro-bridge --status
```

A self-refreshing full-screen view for when the service runs in the background under launchd or systemd and has no console of its own. It shows the engine and its tools, every destination with the default starred, the OCR queue, the current upload with a progress bar, the proxy ports, and what the bridge is doing right now.

| Key | Action |
| --- | --- |
| `r` | Refresh now |
| `c` | Print the dashboard URL |
| `q` | Quit |

It reads `--status-url` (default `http://127.0.0.1:62642`, or `MOKURO_BRIDGE_HOST` / `MOKURO_BRIDGE_PORT`) and `--status-interval` seconds. Piped output is a single frame with no escape sequences, so `mokuro-bridge --status | head -30` is a snapshot you can paste into an issue. A failed fetch is reported inside the frame instead of ending the display, and the terminal is restored on exit and on Ctrl-C.

## Related

- [endpoints.md](endpoints.md) documents the routes behind the page, including their error codes.
- [destinations.md](destinations.md) covers the destination model the wizard edits.
- [architecture.md](architecture.md) has the module map.
