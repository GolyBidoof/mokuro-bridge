# mokuro-bunko install path audit

## Path

1. The engine panel or wizard calls `POST /config/engines/bunko/install`.
2. The endpoint refuses when no cloud destination exists, when `git` or `cargo` is missing, or when Bunko is already configured.
3. It selects the default remote destination, otherwise the first connected remote account, and starts a daemon thread.
4. `bunko_agent.setup(method, log, totp)` resolves the provider backend before any build, so an unconfigured account fails in a second instead of after compilation.
5. It resolves the newest commit of the Bunko `0.8/remote-storage` branch, builds the server-only binary from source with cargo, writes a config pointing at the bridge folder, creates the local admin, starts Bunko on a loopback port, signs in through the control API, and waits for the `.mokuro-bunko.json` marker.
6. The page polls `/config/engines`, shows elapsed time, the last log lines and linkified sign-in URLs, then reports done.

## Requirements confirmed on this machine

- `git`: present.
- `cargo`: present at `/opt/homebrew/bin/cargo`.
- `rustup`: absent, but not required by the build command.
- A configured MEGA, Drive, OneDrive or WebDAV destination.
- Network access to GitHub and the Rust registry for the first build.
- Several minutes of CPU on first install; later starts reuse the binary.

## Blocker-class findings

- Two-factor MEGA needed a TTY before this change. The web flow now accepts an optional TOTP code and passes it through `bunko_agent.setup(..., totp=...)`; without it, a 2FA account still fails at connect time with the Bunko error.
- Google Drive signs in through Bunko's own OAuth project. If the bridge uploaded the library with a different OAuth client, neither the reader nor Bunko lists those files. The automatic flow prints a browser URL or code, which the page now shows as a link.
- OneDrive personal accounts need a pasted redirect address, which the automatic setup cannot supply. Those users must finish sign-in in the Bunko admin UI.
- The install thread lives in the bridge process. Restarting the bridge during the first build aborts it; the next attempt rebuilds. There is no cancel button yet.
- Setup can spend up to its marker wait after sign-in, so the job may show "running" for a while after the build finishes. The log explains the wait.
- Bunko needs a destination folder it is allowed to write metadata into. Read-only WebDAV folders will fail at connect.

## UX changes made

- Engine cards replace the inline text and button, with status pill, description, full-width action and footnote.
- Install buttons disappear after the corresponding engine is installed.
- Bunko's button stays disabled until a cloud destination exists and explains why.
- Optional MEGA two-factor field and visible build log are part of the card.
