# Dashboard and status display

Two ways to see and change a running bridge without reading JSON by hand: a page the bridge serves itself, and a live terminal display. The page reads `/health` for live state and `/config` for the configuration it edits; the terminal display reads `/health` only. Neither needs a restart to open.

## The web dashboard

Open the address the service prints at startup:

```
http://127.0.0.1:62642/
```

It is one self-contained page: inline CSS and JavaScript, inline SVG icons, no external assets, no CDN, no font download. It works offline and screenshots cleanly. The page is read from disk on every request (`Cache-Control: no-store`), so an edit to `mokuro_bridge/web/dashboard.html` shows up on reload without restarting a service that may be mid-upload.

Dark mode follows reader.mokuro.app: the `#030712` canvas, `#1f2937` header and panels, `#374151` borders, the `#ef562f` coral primary, white labels on coral buttons and Verdana throughout. The one deliberate divergence is the light theme: where the reader offers white or sepia, this page uses a warm-paper palette (`#f7f4ef` canvas, white panels) with a deeper coral (`#c2410c`) so links and buttons stay legible. A theme switch in the header offers Auto, Light and Dark; Auto is the default and follows the operating system, and the choice is remembered in `localStorage` under `mb-theme`. The chosen theme is applied before first paint, so reloading does not flash the wrong colours. The layout is responsive down to phone widths, and `prefers-reduced-motion` turns transitions off.

### What the page shows

| Area | Content |
| --- | --- |
| Header | Brand, an `Open reader` link to reader.mokuro.app (hidden on narrow screens, where the footer's reader link remains), the status chip, the Auto/Light/Dark switch and Add destination |
| Status chip | `Connecting…`, `Not connected`, `Ready`, or the stage the bridge is in right now (`uploading`, `ocr`, `packaging`), from the same activity record `/health` reports |
| Hero and stats | One sentence about what the bridge is doing, then pages waiting to be read, volumes reading now, and the default destination it files to |
| Browser acceleration | Live `fetchProxyPorts`: the open port count and its first and last port, plus six extra browser lanes per port, or `off` when no ports are open. `Adjust` scrolls to and focuses `Faster downloads in the browser`, or explains that an environment variable pins it; `How this works` opens [browser-acceleration.md](browser-acceleration.md) |
| Notices | A banner when the bridge is not answering, when a newer version exists (with a release link and the `mokuro-bridge --update` command), and when a destination needs signing in again (with a Reconnect button) |
| Destinations | Connected accounts are cards with their alias, provider brand glyph (a Google Drive triangle, M/O/W monograms for MEGA, OneDrive and WebDAV, or a folder glyph for local), note, email when the bridge knows it, folder, state chip and default mark, with Change folder, Make default, Details, Rename, Remove and Copy id. Accounts that are not connected render as single-column reader-style rows under `Not connected yet`, with the provider glyph and name, a one-line value, Connect or Reconnect, and Details |
| Add a destination | A card at the end of the grid, plus header and section buttons, that opens the provider picker |
| Preferences | The three settings, with their source and a Save changes button |
| Footer | Version, a link to reader.mokuro.app, update state (`Checking for updates…`, `Up to date`, `Update checks are off` or `Could not check for updates`), and a Developer details section with raw `/health` and `/config` links, the terminal command, the settings file path, the engine and the fetch-proxy state |

On a first run, with no remote destination configured, the page also shows a three-step onboarding row: add a destination, pick a folder, then use its alias from scripts.

### Browser acceleration row

The status card has a `Browser acceleration` row that reads the live `fetchProxyPorts` list from `/health`. With ports open it shows the count, the first and last port, and the number of extra browser lanes at six per port; when the count is zero it says `off` and notes that the browser downloader uses its usual connections. `Adjust` scrolls to and focuses `Faster downloads in the browser` in Preferences, or explains that the row is pinned by an environment variable, and `How this works` opens [browser-acceleration.md](browser-acceleration.md). That page covers the per-origin connection limit, why the port is part of the origin, the browser's own global connection ceiling and when to change the port count.

### Account details

Details opens a dialog for one destination with these rows, all read from `/config` and rendered escaped:

- Provider, account name, and the alias (the id scripts use).
- The note (`label`) when one was set, and the MEGA email when the bridge can derive it from stored metadata.
- Folder, the folder uploads land in for that account.
- Status.
- Sign-in, which is where the credential material lives: this computer's keychain, a credentials file on this computer, or a saved browser sign-in.
- Script id, shown as `upload_method=<alias>`, with a Copy id button.
- A link to the provider's web interface (mega.nz, Google Drive, OneDrive); WebDAV shows the server URL instead.

Secrets are never returned by `/config`, so the dialog shows where a credential is stored, never the credential.

### Adding an account from the browser

Add a destination opens a provider picker. A provider whose optional library or command-line tool is missing is disabled, names what is missing, and links to the setup it needs: megatools for MEGA, the Google Cloud console for a Drive OAuth client, the Azure portal for a OneDrive app registration, or the `requests` library for WebDAV.

- **MEGA** asks for the account email and password once. The login is verified with `megatools` before anything is stored. Credentials go to the existing OS keychain, with a 0600 `credentials.env` fallback, exactly as the terminal wizard. The remote folder can be changed afterwards from the account's Change folder button.
- **Google Drive** opens Google's sign-in in a new tab. The redirect returns to `GET /config/oauth/drive/callback` on this same bridge, which stores the refresh token and shows a small page saying the account is connected. A Google client ID and secret are only needed when this computer has no preset client (`DRIVE_CLIENT_ID`, `DRIVE_CLIENT_SECRET`, or `DRIVE_CLIENT_SECRET_FILE`); the posted pair is used for that sign-in and is not saved.
- **OneDrive** runs the Microsoft device-code flow. The page shows a short code with a copy button and a link to Microsoft's sign-in page, polls `/config/accounts/onedrive/status`, and finishes by itself once the code is entered. It needs an Azure app registration in `ONEDRIVE_CLIENT_ID`.
- **WebDAV** asks for the server URL, username and password. The URL is the collection to upload into, for example a Nextcloud `remote.php/dav/files/<user>/` address; the bridge sends one `PROPFIND` before saving anything, so a wrong URL or password is an error on the form instead of an account that fails later. The URL and username are stored with the account and the password in a 0600 credential file (plus the keychain for the first account). Reconnect keeps the stored URL, username and password when a field is left empty.

Each provider can hold any number of accounts, each with its own alias, folder, note and credentials. Adding another account of the same provider is the same flow: pick the provider again and choose a free alias.

### Managing destinations

- **Change folder** browses the provider's real folder tree through the provider's own API (`megatools ls` for MEGA, `files.list` for Drive, the children endpoint for OneDrive, a `Depth: 1` `PROPFIND` for WebDAV). The chosen folder becomes that account's root, so later uploads land in `root/<series>/`. For the local destination it chooses the remembered local output folder, which applies to the next finalize. New folder is offered only in the local picker; it creates a directory under the same ingest roots the bridge already allows.
- **Make default** sets the sticky default. The starred target receives every upload until a client names another one. It applies to the next finalize and needs no restart.
- **Reconnect** appears when an account's saved sign-in has expired. It runs the provider's sign-in flow again with the same alias and keeps the account's folder, note and default.
- **Rename** changes the alias and the note. Root, credentials and the default flag are preserved, and if the account was the default the sticky default follows the new alias. An alias already used by another account is refused.
- **Remove** deletes the saved sign-in and the account metadata. Files already uploaded are untouched and the local folder is never touched. If the removed account was the default, the sticky default is cleared and the bridge falls back to its configured seed, which is the local destination unless `MOKURO_BRIDGE_UPLOAD_DEFAULT` names another target.
- **Copy id** copies `upload_method=<alias>`, which is the value a dokuha script passes to select that account.

The page shows provider links for the account you are editing and for the setup consoles, so a missing app registration or OAuth client can be created without leaving the docs.

### Aliases and historical ids

Every upload account has an alias: `mega`, `mega-2`, `drive-next`. The alias is the account's `id` in `/config`, `/health` and `/upload-methods`, and the value clients pass as `upload_method` or `destination`.

- Aliases are 1 to 32 characters from lowercase letters, digits and hyphens, starting with a letter or digit. `local` is reserved, and another provider's bare name (`mega`, `drive`, `onedrive`, `webdav`) is reserved for that provider's first account.
- The first account of a provider is suggested as the bare provider name, later ones as `mega-2`, `mega-3`, and so on, skipping anything taken. The page prefills the suggestion; any free alias can be typed instead.
- Aliases are globally unique across providers. A duplicate on the add routes means "reconnect this account of this provider"; a duplicate across providers is refused.
- Historical spellings keep working as input forever: `mega`, `mega:work`, `drive:main` and `provider:default` all resolve to the same account, and a stored sticky default in an old spelling still resolves to the same account. Displays now show the alias, so an old `mega:work` appears as `mega-work`.
- Renaming changes only the alias. The storage name, root, note, credentials and keychain entries stay, and the legacy `provider:name` spelling keeps resolving. The freed old alias becomes available.

Accounts created before aliases existed derive one on read without their files being rewritten. The terminal setup commands still work as a fallback: `mokuro-bridge --setup-upload drive --name next` creates the account whose alias becomes `drive-next`, `--list-uploads` shows the ids and `--remove-upload drive:next` removes one.

### Settings storage and precedence

The page shows three settings, because they are the three a person configuring uploads actually decides:

| Setting | What it changes | Validation |
| --- | --- | --- |
| Faster downloads in the browser | How many extra local ports the page downloader may fetch through | Whole number from 0 to 256 |
| Where finished volumes are saved on this computer | The fallback folder local copies are written to, with series folders inside it | Absolute path to an existing folder, or to a new folder whose parent exists and is writable |
| Other folders the bridge may read | Extra read roots, for a library that lives outside your home folder | Comma-separated absolute paths |

The output-dir preference is the fallback used when no folder has been chosen for the local destination. Choosing a folder on the local destination card sets a remembered folder that applies immediately to the next finalize and takes precedence over this fallback. The other two settings are read when the process starts, so a change applies after the bridge restarts; the page shows the restart command after saving, with a copy button.

The page writes to `~/.config/mokuro-bridge/settings.json` (override with `MOKURO_BRIDGE_SETTINGS_FILE`). Every reader asks in this order:

1. the environment variable, so a launchd plist or a shell export always wins,
2. the settings file, which is what the page writes,
3. the built-in default.

A row whose environment variable is set is disabled and labelled `Pinned by <VARIABLE>`. The save endpoint also skips an environment-pinned key when a write arrives from the page or from curl, so the stored file value is kept for the day the variable is unset and can never be clobbered by a save. Removing the variable and restarting is what makes the stored value take effect.

Rejected values are listed by label, for example `Where finished volumes are saved on this computer: is a file, not a folder; choose a folder or a new name inside an existing folder`, and nothing is written when any value is rejected. The file is plain JSON, hand-editable, written atomically with mode 0600, and holds no secrets. Everything else the bridge understands is set from the environment and documented in `.env.example`; a hand-edited settings file can carry those keys as well, and they apply.

### Writes are gated to this machine

The bridge is unauthenticated and loopback-only, and `CORS_ORIGINS` defaults to `*`, so any page in your browser can already reach it. Every configuration write (`/config/account`, `/config/default`, `/config/local-output`, `/config/new-folder`, `/config/settings`, `/config/forget` and every `/config/accounts/*` route) checks the `Origin` header: a request from a loopback origin (`http://127.0.0.1:*`, `http://localhost:*`, `http://[::1]:*`) or with no `Origin` at all (curl, the CLI) is accepted, and anything else gets `403`. The Google Drive OAuth callback is a `GET` opened by Google's redirect, so it is not Origin-gated; it only accepts a one-time `state` created by this bridge. Reads are not gated, so a monitoring script can still poll. No response ever returns a credential, token or client secret.

## The terminal status display

```
mokuro-bridge --status
```

A self-refreshing full-screen view for when the service runs in the background under launchd or systemd and has no console of its own. It shows the engine and its tools, every destination with its alias and the default starred, the OCR queue, the pipeline and worker state, the proxy ports, what the bridge is doing right now, and a live upload progress bar while an upload is active, with file name, percent, bytes and speed. The bar is backed by the `upload` object `/health` reports while an upload is running and is omitted when nothing is uploading.

| Key | Action |
| --- | --- |
| `r` | Refresh now |
| `c` | Show the dashboard URL and what that page edits |
| `?` | Help overlay: the keys, the `★` default and `●`/`○` configured markers, and where settings are edited |
| `q` | Quit |

`?` and `c` leave the alternate screen, wait for a key, then re-enter it, and `q` quits from either. The display honours `NO_COLOR` and `TERM=dumb` (colour off, layout unchanged), re-reads the terminal size every frame so a resize is picked up, and measures display width with `wcwidth` when available and Unicode east-asian and combining rules otherwise, so CJK titles and combining marks do not break the frame. Borders, the header URL and the footer keep their corners down to 30 columns, and below 46 columns the layout switches to a compact body that still shows the status, the default destination, the worker state and the queue.

It reads `--status-url` (default `http://127.0.0.1:62642`, or `MOKURO_BRIDGE_HOST` / `MOKURO_BRIDGE_PORT`) and `--status-interval` seconds. `--status-url` accepts a bare `host:port`, adds the scheme once when it is missing, and brackets IPv6 literals. `--host` and `--port` are honoured when no URL is given. The fetch has a 3 second timeout and a failed fetch is reported inside the frame instead of ending the display; on a bridge that is not answering, a keypress can take up to that timeout to register. Piped output is one full-height frame with no escape sequences, so `mokuro-bridge --status | head -30` is a snapshot you can paste into an issue. The terminal is restored on exit, on Ctrl-C and on an exception.

## Related

- [endpoints.md](endpoints.md) documents the routes behind the page, including their error codes.
- [destinations.md](destinations.md) covers the destination model the page edits.
- [troubleshooting.md](troubleshooting.md) covers a page that will not load, sign-ins that need reconnecting and settings that need a restart.
- [browser-acceleration.md](browser-acceleration.md) explains the fetch-proxy ports behind the Browser acceleration row and the browser limits they run into.
- [architecture.md](architecture.md) has the module map.
