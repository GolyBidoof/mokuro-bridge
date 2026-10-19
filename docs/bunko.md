# Running mokuro-bunko next to the bridge

`mokuro-bridge --setup-bunko` installs [mokuro-bunko](https://github.com/Gnathonic/mokuro-bunko), signs it in to the same cloud account the bridge uploads to, and starts it. Bunko then compiles `series.json` and `catalog.json` in the cloud folder, and the bridge stops writing `series.json` for that destination.

This exists because the bridge's own manifest is a workaround. reader.mokuro.app only builds a series index for series it already knows locally, so an externally filled folder needs a `series.json` written by the uploader. Bunko owns a library and compiles that file itself, so when Bunko is present the bridge's copy is redundant and can fail for permission reasons on a Bunko-hosted library.

## What it needs

- A cloud upload account that is already configured in the bridge: MEGA, Google Drive, OneDrive or WebDAV.
- The first run builds Bunko from source: `git`, the Rust toolchain (`rustup`), and a few minutes of CPU. Later runs reuse the binary.
- Bunko 0.8 remote storage is not released yet. The bridge follows the head of the `0.8/remote-storage` development branch: every install resolves the branch's newest commit, records it in the install manifest, and builds the server-only binary (`--no-default-features`, no OCR engines, no tray). Rerun `--setup-bunko` or use `--update-bunko` to move to a newer commit; the old binary is replaced only after a build succeeds. `MOKURO_BUNKO_REF` pins a branch, tag or commit for a reproducible install, `MOKURO_BUNKO_OFFLINE=1` keeps the installed build, and `MOKURO_BUNKO_BIN` points at an existing binary instead of building one.
- No cloud secret is copied into Bunko's config. The sign-in goes through Bunko's own local control API, which only listens on `127.0.0.1` and requires the token in its 0600 `.control.json`. MEGA's password is read from the bridge's existing credential store at connect time, and the two-factor code is asked on the terminal (or taken from `MOKURO_BUNKO_MEGA_TOTP`).

## The command

```bash
mokuro-bridge --setup-bunko            # the bridge's default upload account
mokuro-bridge --setup-bunko mega       # or an explicit account id
mokuro-bridge --setup-bunko webdav:home
```

What it does, in order:

1. Builds or reuses the pinned Bunko binary under the bridge's data directory (`~/Library/Application Support/mokuro-bridge/bunko` on macOS, `~/.local/share/mokuro-bridge/bunko` elsewhere, `MOKURO_BRIDGE_BUNKO_DIR` overrides).
2. Writes a Bunko config that points at the same cloud folder as the bridge account, binds the HTTP UI to `127.0.0.1`, disables reader serving and local OCR, and leaves the rest at Bunko's defaults.
3. Creates Bunko's local admin user on first setup.
4. Starts Bunko and signs it in through Bunko's control API, creating the library folder when it does not exist.
5. Waits up to a minute for the `.mokuro-bunko.json` marker in the cloud folder, then returns even while Bunko is still claiming the library. The claim has a settle wait of `max(60 s, 2 x feed interval)`, which is about ten minutes on MEGA and about a minute on WebDAV, so the marker can appear well after setup returns; the bridge writes `series.json` until it does and then stops. Once the marker is fresh, the bridge's finalize leaves `series.json` to Bunko and uploads the `.mokuro` before the `.cbz` so Bunko never queues OCR for a pre-OCR'd volume.

After setup the command prints the instance status and returns. Bunko keeps running in the background; `mokuro-bridge` starts it again on later runs when it is configured and stopped (`MOKURO_BRIDGE_BUNKO_AUTOSTART=0` disables that).

## Sign-in support

| Destination | Setup |
| --- | --- |
| MEGA | automatic: email and password from the bridge's account, optional `MOKURO_BUNKO_MEGA_TOTP` for two-factor accounts |
| WebDAV | automatic: URL, user and password from the bridge's WebDAV account |
| Google Drive | Bunko prints a sign-in address or a code; finish it in the browser. Use the same reader pairing (release or beta) as the reader you use. |
| OneDrive | Bunko prints a sign-in address; a personal Microsoft account needs the pasted redirect address, which this automatic setup cannot supply, so use the Bunko UI for that account |

Google Drive has one caveat worth knowing: Bunko signs in with the reader's Google project so it sees the same files the reader sees. If the bridge uploaded the library with its own OAuth client, that project is different, and neither the reader nor Bunko lists those files. MEGA and WebDAV do not have this split.

## Managing it

```bash
mokuro-bridge --bunko-status    # installed, configured, running; exit 1 when not running
mokuro-bridge --bunko-logs      # last 80 lines of Bunko's log
mokuro-bridge --update-bunko    # rebuild if the development branch moved, restart
mokuro-bridge --stop-bunko      # stop the background instance
```

The bridge and Bunko are independent processes. Bunko survives a bridge restart because setup starts it in its own session; stopping the bridge does not stop Bunko, and `--stop-bunko` does not touch the cloud library. The full Bunko admin UI is at `http://127.0.0.1:<port>/_admin` for the local admin user, and `mokuro-bunko library status` (with the config the bridge writes) shows the store, lease and sync state.

## What the bridge changes when Bunko is present

- Finalize skips reading, merging and uploading `series.json`.
- The upload order becomes cover, `.mokuro`, `.cbz` so Bunko's OCR debt never sees an archive without a sidecar.
- The finalize stream reports `series_json` with `external: true` instead of uploading the file.
- If the marker disappears or goes stale, the bridge writes `series.json` again on the next finalize. That is also what the reader does after two marker TTLs.

Nothing changes for a destination without Bunko: the bridge writes and merges `series.json` as before.

## Removing it

Stop Bunko, then run `mokuro-bunko library disconnect` with the config in the bridge's Bunko directory if you want Bunko to release the cloud library cleanly (it writes `released` into the marker and leaves the library files). The bridge's `series.json` handling returns as soon as the marker is no longer fresh. The pinned binary, source checkout and build target live under `MOKURO_BRIDGE_BUNKO_DIR` and can be deleted as a folder.
