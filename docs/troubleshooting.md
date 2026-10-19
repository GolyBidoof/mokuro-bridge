# Troubleshooting

The failures that come up in practice, and what to do about them.

**Check `/health` first.** It reports the engine (`mokuro_installed`, `mokuro_version`, `mokuro_fork_api`), the fetch-proxy ports it bound (`fetchProxyPorts`, `fetchUpstreams`), the upload targets by alias and the current default (`upload_method_default`), the OCR queue (`ocr_queue_depth`), whether the bridge is busy (`busy`, `busy_stage`, `busy_detail`), the live upload when one is running (`upload`) and the update state, so most of these questions answer themselves without guessing. The dashboard at `http://127.0.0.1:62642/` renders the same facts in one page.

## Install

**`ModuleNotFoundError: No module named 'fastapi'`.** pip installed into a different Python than the one running the server. The server already tries to say so: it names the interpreter that failed and prints the matching `-m pip install` line. Compare `python3 -c "import sys; print(sys.executable)"` against `python3 -m pip -V`, and prefer `python3 -m pip` over a bare `pip`. Activate the virtualenv in every new terminal.

**`No space left on device` while pip builds a wheel.** This is usually `$TMPDIR`, not your disk. Many distros mount `/tmp` as a small tmpfs, so `df -h /` reports a different filesystem and the machine genuinely has free space. Building `unidic-lite`, which ships an sdist and no wheel, unpacks about 45 MB of dictionary into it. Point pip somewhere roomier:

```bash
mkdir -p ~/.cache/pip-tmp && TMPDIR=~/.cache/pip-tmp pip install -r requirements-ocr.txt
```

**Python version.** 3.10 or newer is required, and pip enforces it through `requires-python`. Older interpreters fail at install time rather than at runtime.

## OCR

**OCR is unavailable but the server is fine.** That is the expected state of a base install: `/health` reports `mokuro_installed: false`. Install either engine from the [README](../README.md#install).

**Is the fast path active?** `/health` answers with `mokuro_fork_api`. `false` is not a fault: it means the loaded engine has no batched API, so the bridge is using the slower single-crop path. Only [the fork](https://github.com/GolyBidoof/mokuro) has it. `mokuro_repo` and `mokuro_custom_fork` show whether `MOKURO_REPO` is in play.

**Which engine is installed?** `/health` reports `mokuro_flavor` as `upstream`, `fork`, `absent` or `unknown`, read from install provenance rather than the version string. If it says upstream or unknown, the service logs one line at startup naming `mokuro-bridge --install-mokuro`, the one command that replaces it with the faster fork and verifies the import; `MOKURO_BRIDGE_MOKURO_NOTICE=0` silences that line. The exact per-environment commands and the way back to upstream are in [mokuro.md](mokuro.md).

**`AttributeError` reading a generated `.mokuro` with no engine installed.** A v0.7.0 bug, fixed in v0.7.1. Upgrade.

**PyTorch will not install on your Python.** Its wheels often trail new releases. Install the engine on an interpreter that has them, or wait for the wheel.

## Runtime

**`OSError: Too many open files`.** The file-descriptor limit could not be raised far enough for the fetch proxy, which holds a descriptor on each side of the bridge, about 600 at peak for 48 ports. Lower `MOKURO_BRIDGE_FETCH_PORTS`, set it to `0` to turn the proxy off, or raise the limit for whatever starts the bridge.

**The fetch proxy is off.** `UVICORN_RELOAD=1` disables it, because the reloader spawns a second process that would contend for the same ports. `MOKURO_BRIDGE_FETCH_PORTS=0` disables it deliberately.

**403 on a folder of pages.** The local-ingest routes are confined to your home directory and the platform temp locations, which on Windows means `C:\Users\<you>\...`, so a library on `D:\` is refused until you add it:

```bash
MOKURO_BRIDGE_INGEST_ROOTS='D:\manga' mokuro-bridge
```

Comma-separated, absolute paths only. A relative entry is ignored rather than resolved against the working directory.

**A client is not using the extra ports.** It has to read `fetchProxyPorts` from `/health` and fetch through them. If it cannot reach the bridge at all, check `CORS_ORIGINS`: it is a wildcard by default, and narrowing it can lock out the storefront the client runs on.

**A volume landed in an unexpected folder.** This is almost always the sticky upload default, not a bug: once any client names a destination explicitly, that choice becomes the default for every later request, including from other clients, until another explicit choice replaces it. `GET /upload-methods` and `/health` report `upload_method_default`. To see where a local volume went, read `current_folder` for the `local` method; it reflects the remembered `local_dir` when one was set. See [destinations.md](destinations.md).

**A volume folder has a random suffix such as `_a1b2c3`.** That is the collision rule working: the bridge keeps the clean title unless a directory with that name already exists, in which case it adds a six-character suffix so the two volumes cannot share working state. Remove or finish the older volume and the next run takes the clean name.

**Finalize answers 409.** Another finalize already owns that session, or a page ingest is still holding a reservation. `GET /session/{id}/status` shows `finalizing` and `pages_ingesting`; wait for them to clear rather than retrying immediately.

**MEGA upload fails with `ENOENT` after a successful wizard run.** A v0.6.0 keychain bug, where a stale entry for an older address shadowed the working one. Upgrade.

## Dashboard and accounts

**The dashboard does not load, or loads with empty panels.** The page is a small JavaScript app: it reads `/health` and `/config` after loading. Check those two endpoints first (`curl -s http://127.0.0.1:62642/health`, then `/config`). A `500` saying `dashboard page missing` means the packaged page is not installed, so reinstall the package or restore `mokuro_bridge/web/dashboard.html`. The page is served from disk on every request with no cache, so a normal reload picks up a new build without restarting the service. If JavaScript is disabled, `/health` and `mokuro-bridge --status` show the same state without the page.

**The browser did not open on first run.** The auto-open only happens when no remote account is configured, the run has a real terminal (stdin and stdout are a TTY) and it was not disabled. A service started by launchd, systemd or another manager has no terminal, so it never opens a browser; `--no-browser` and `MOKURO_BRIDGE_OPEN_BROWSER=0` also stop it. Open the address the banner prints and follow the setup guide by hand. The startup banner is deliberately short; add `--verbose` or set `MOKURO_BRIDGE_VERBOSE=1` to print the paths, engine and fetch-proxy lines as well.

**The setup guide does not appear, or it keeps appearing.** The guide opens by itself only while no remote account is configured and only once: `Skip for now` or closing it stores `mb-wizard-seen` in the page's `localStorage`. Reopen it from the footer's Setup guide link, or add `?wizard=1` to the page URL to force it once. Clearing the page's stored site data brings the automatic first-run guide back.

**The Reading engine fact says `upstream build`.** That is the slower stock mokuro package, not a fault. Install the batched GolyBidoof fork with `mokuro-bridge --install-mokuro`, which replaces the engine in the environment that runs the bridge and verifies the import; Developer details shows the same command when the engine is upstream or missing. `/health` reports `mokuro_flavor` and `mokuro_fork_api` if you want to confirm the change. A `Library index` line for mokuro-bunko is optional and appears only when that backend reports a version, so its absence means nothing is wrong.

**A change on the page answers `403 configuration changes are accepted only from this machine`.** Every configuration write is gated to a loopback `Origin`, because the bridge is unauthenticated and any page in a browser can reach it. Open the dashboard at `http://127.0.0.1:62642/` or `http://localhost:62642/`, not through a LAN address or another hostname. curl and the CLI are accepted because they send no `Origin`.

**A destination says `Sign-in expired`.** The account has stored credential material but the bridge cannot use it right now, so uploads to it would fail. Open its card and press Reconnect, which runs the same sign-in flow again with the same alias and keeps the folder, note and default. For Google Drive and OneDrive this is usually a revoked or expired token; for MEGA, sign in again with the account password. The state is a local check, so `needs_sign_in` can also mean a required optional library is missing; `/config` lists what is missing under `providers[].missing`.

**Add a destination is disabled, or a provider says it is unavailable.** The page checks for what that provider needs and names it: `megatools` for MEGA, `google-auth-oauthlib` and `google-api-python-client` for Drive, `msal` and `requests` for OneDrive, and `requests` for WebDAV. Install the matching package (`pip install google-api-python-client google-auth-oauthlib` for Drive, `pip install msal` for OneDrive, `pip install requests` for WebDAV), install megatools for MEGA, then reload the page. The page links to the Google Cloud console and the Azure portal for creating those clients. A missing OneDrive app ID does not disable anything: the OneDrive form collects it.

**A WebDAV destination says the username and password were rejected.** Most servers treat your login password and a WebDAV app password as different things. In Nextcloud open Settings, then Security, then App passwords and create one; ownCloud has the same page. On a Synology NAS create an app password in the user's settings, and Box requires one from Account Settings. A server that expects a domain prefix (some NAS and hosting setups) needs the full spelling of the account name.

**WebDAV says the address answered with a web page, or that no WebDAV folder was found.** That address is the page you browse, not the collection. Pasting the server's own address is fine: the bridge tries `remote.php/dav/files/<user>/`, `/dav/` and `/webdav/` behind it. If nothing answers, look for the WebDAV address in the server's settings (Nextcloud shows it in the Files app's settings; Synology has a WebDAV Server package) and paste that. A file share link is not a WebDAV address.

**WebDAV on a NAS fails with a certificate error.** A self-signed certificate is normal on a home server. Open the WebDAV form's Advanced settings and tick Accept a self-signed certificate; the choice is stored with the account and only affects that account, and the Details dialog then says the certificate is not verified.

**The WebDAV form cannot reach the server at all.** The bridge runs on this computer, so `localhost` and `127.0.0.1` mean the bridge's machine, not your laptop. Use the NAS or server's network name or address, check that the WebDAV port is open (Synology: 5005 for HTTP, 5006 for HTTPS), and try the address in a browser first. If the address there has no scheme, the form adds `https://` for a public hostname and `http://` for something that looks local.

**The Google sign-in window says the link is no longer valid, or the sign-in did not finish.** Drive and OneDrive sign-ins are held in memory for 10 minutes, and restarting the bridge cancels one in progress. Press Add destination and start again. If Drive answers that the OAuth client is unrecognised or missing, set `DRIVE_CLIENT_ID` and `DRIVE_CLIENT_SECRET`, or point `DRIVE_CLIENT_SECRET_FILE` at a downloaded `client_secrets.json`; a client pasted into the page is used for that sign-in only and is not saved.

**The OneDrive code is rejected or the flow expired.** A sign-in flow expires after 10 minutes and is held in memory. Start the sign-in again from the destination card. A stale code is normal and changes nothing on the account.

**OneDrive asks for an app ID.** That is the free step Microsoft requires of every app that signs users in; a self-hosted install cannot ship one for you. Open the OneDrive form and follow its five steps, or in short: open the Azure portal, App registrations, New registration, choose the account types that include personal Microsoft accounts, set Authentication, Allow public client flows to Yes, add the delegated Microsoft Graph permission Files.ReadWrite, then copy the Application (client) ID from Overview. Paste it into the form; it is saved for every OneDrive account and no restart is needed, and the same ID can be reused if you already made one for mokuro-reader. No redirect URI and no client secret are involved.

**You have a personal Microsoft account and cannot create an app registration at all.** Azure app registrations need an Entra tenant, and a personal Microsoft account cannot make one, so the portal refuses before any app ID exists. Use the rclone destination instead: rclone ships its own Microsoft app registration, so a personal account is enough. Install rclone, run `rclone config`, choose Microsoft OneDrive, leave the client ID and secret blank, sign in in the browser, then add an rclone destination in the page and pick that remote. Nothing in Azure is needed.

**OneDrive sign-in fails with AADSTS65001.** The app registration is missing the delegated `Files.ReadWrite` permission. Add it under API permissions, then start the sign-in again.

**OneDrive sign-in fails with AADSTS700016.** Microsoft does not recognize the app ID for the account you signed in with. Copy the Application (client) ID (not the Object ID or Directory ID), and set the app's supported account types to include personal Microsoft accounts as well as work or school ones.

**OneDrive sign-in fails with AADSTS50076.** Microsoft wants an extra verification step for that account. Finish the prompt in the browser, or sign in once at https://microsoft.com/devicelogin with the code the bridge shows.

**An rclone destination says the remote is not configured.** The remote name in the account must match a section in rclone's own configuration. Run `rclone listremotes` and `rclone config file` in a terminal: the section names it prints (with the trailing colon) are what the form offers. A remote that works in a terminal but not here usually belongs to another user account or an `RCLONE_CONFIG` the service does not see; point `RCLONE_CONFIG` at it in the service environment.

**rclone is not detected by the page.** The page finds rclone on `PATH`, or at the path in `RCLONE_BINARY`. A service started by launchd, systemd or a desktop launcher often has a smaller `PATH` than your shell: set `RCLONE_BINARY=/usr/local/bin/rclone` (or wherever `which rclone` points) in the service environment and restart.

**An alias is rejected, or Rename answers that it is taken.** Aliases are 1 to 32 characters from lowercase letters, digits and hyphens, must start with a letter or digit, cannot be `local`, and cannot be another provider's bare name (`mega`, `drive`, `onedrive`). They are unique across all accounts, so an alias owned by a different provider is refused. After a rename, update any script that used the old alias: the alias is the id scripts pass, while historical `provider:name` spellings keep resolving for existing accounts.

**A preference row is disabled and says `Pinned by ...`.** That environment variable is set for the bridge, and the environment wins over the settings file. The page disables the row and the save endpoint skips that key, so a save can never overwrite the stored value. Unset the variable in the service's environment (launchd plist, systemd unit or shell), restart, and then the stored value applies.

**Saving preferences says `Restart the bridge to apply them`.** The three preferences are read when the process starts. The page shows the restart command with a copy button; on macOS launchd the command is `launchctl kickstart -k gui/$(id -u)/com.mokuro-bridge`. Anything else the bridge understands is set from the environment and documented in `.env.example`; a hand-edited settings file can carry those keys as well.

**Saving the output folder is rejected.** The value must be an absolute path. An existing file is refused with `is a file, not a folder`, and a path whose parent folder does not exist is refused with `cannot be created because its parent folder does not exist`. A new folder is allowed when its parent exists and is writable; the bridge creates it at the next start. Extra read roots must be absolute too, because the bridge ignores relative entries.

**The page says update checks are off or failed.** `MOKURO_BRIDGE_UPDATE_CHECK=0` turns the background check off, and a failed check is reported with its reason. Neither affects uploads; `mokuro-bridge --check-update` runs the check by hand and still works when the background check is disabled.

## Updating

When a newer release exists the service logs one line about it at startup, and `mokuro-bridge --check-update` reports it without changing anything: exit 0 when up to date, 1 when a newer release exists, and 2 when the check could not be completed. Nothing is applied automatically, because a restart mid-OCR loses work. `MOKURO_BRIDGE_UPDATE_CHECK=0` turns off the background check and its notice; running `--check-update` by hand still works.

`mokuro-bridge --update` applies the upgrade. It reads the install method from the paths the tool left behind and runs exactly one of these: `pipx upgrade mokuro-bridge`, `uv tool upgrade mokuro-bridge`, `python -m pip install --upgrade mokuro-bridge`, or for a git checkout `git pull --ff-only` followed by `pip install -r requirements.txt`. It prints each command before running it, and when it cannot tell how the copy was installed it prints the candidate commands and runs none of them. The service never restarts itself; after the upgrade, restart it with the command `--update` prints, which on macOS launchd is:

```bash
launchctl kickstart -k gui/$(id -u)/com.mokuro-bridge
```

**A volume uploaded by a script never appears in reader.mokuro.app.** The reader needs the per-series `series.json` beside the volume files, and it only writes that file itself for a series the device already knows locally. Check the series folder at the destination for `series.json`: if it is absent, the upload did not come from this bridge (or came from a release before v0.9.0, which did not write one). Re-finalize the volume to the same destination and the bridge creates it, or merges the volume into an existing one. A finalize that reports `partial_upload` with a `series.json` error in the message saved the volume files but not the index, which is why the reader shows nothing; the reason is in the frame, and the most common ones are a destination the bridge could not read (it refuses to overwrite a manifest it cannot read) and a provider that rejected the upload.

**Has the series.json format changed upstream?** Run `mokuro-bridge --check-series-reference`: exit 0 means the pinned bytes still match and neither repository has moved, 1 means upstream moved, 2 means the check could not run. Nothing breaks until the format itself changes, and the service never checks on its own. See [series-json.md](series-json.md).

## Still stuck

Read [architecture.md](architecture.md) for how the pieces fit, [endpoints.md](endpoints.md) for the exact route contract, and [CHANGELOG.md](../CHANGELOG.md) for whether it was already fixed. Open an [issue](https://github.com/GolyBidoof/mokuro-bridge/issues) with the `/health` output and the client's error.
