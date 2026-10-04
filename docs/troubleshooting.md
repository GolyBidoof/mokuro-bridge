# Troubleshooting

The failures that come up in practice, and what to do about them.

**Check `/health` first.** It reports the engine (`mokuro_installed`), whether the
batch path is active (`mokuro_fork_api`), the fetch-proxy ports it bound
(`fetchProxyPorts`) and the update state, so most of these questions answer
themselves without guessing.

## Install

**`ModuleNotFoundError: No module named 'fastapi'`.** pip installed into a different
Python than the one running the server. The server already tries to say so: it names
the interpreter that failed and prints the matching `-m pip install` line. Compare
`python3 -c "import sys; print(sys.executable)"` against `python3 -m pip -V`, and
prefer `python3 -m pip` over a bare `pip`. Activate the virtualenv in every new
terminal.

**`No space left on device` while pip builds a wheel.** This is usually `$TMPDIR`, not
your disk. Many distros mount `/tmp` as a small tmpfs, so `df -h /` reports a
different filesystem and the machine genuinely has free space. Building
`unidic-lite`, which ships an sdist and no wheel, unpacks about 45 MB of dictionary
into it. Point pip somewhere roomier:

```bash
mkdir -p ~/.cache/pip-tmp && TMPDIR=~/.cache/pip-tmp pip install -r requirements-ocr.txt
```

**Python version.** 3.10 or newer is required, and pip enforces it through
`requires-python`. Older interpreters fail at install time rather than at runtime.

## OCR

**OCR is unavailable but the server is fine.** That is the expected state of a base
install: `/health` reports `mokuro_installed: false`. Install either engine from the
[README](../README.md#the-ocr-engine-any-mokuro-works).

**Is the fast path active?** `/health` answers with `mokuro_fork_api`. `false` is not
a fault: it means the loaded engine has no batched API, so the bridge is using the
slower single-crop path. Only [the fork](https://github.com/GolyBidoof/mokuro) has it.
`mokuro_repo` and `mokuro_custom_fork` show whether `MOKURO_REPO` is in play.

**`AttributeError` reading a generated `.mokuro` with no engine installed.** A v0.7.0
bug, fixed in v0.7.1. Upgrade.

**PyTorch will not install on your Python.** Its wheels often trail new releases.
Install the engine on an interpreter that has them, or wait for the wheel.

## Runtime

**`OSError: Too many open files`.** The file-descriptor limit could not be raised far
enough for the fetch proxy, which holds a descriptor on each side of the bridge, about
600 at peak for 48 ports. Lower `MOKURO_BRIDGE_FETCH_PORTS`, set it to `0` to turn the
proxy off, or raise the limit for whatever starts the bridge.

**The fetch proxy is off.** `UVICORN_RELOAD=1` disables it, because the reloader spawns
a second process that would contend for the same ports. `MOKURO_BRIDGE_FETCH_PORTS=0`
disables it deliberately.

**403 on a folder of pages.** The local-ingest routes are confined to your home
directory and the platform temp locations, which on Windows means `C:\Users\<you>\...`,
so a library on `D:\` is refused until you add it:

```bash
MOKURO_BRIDGE_INGEST_ROOTS='D:\manga' mokuro-bridge
```

Comma-separated, absolute paths only. A relative entry is ignored rather than resolved
against the working directory.

**A client is not using the extra ports.** It has to read `fetchProxyPorts` from
`/health` and fetch through them. If it cannot reach the bridge at all, check
`CORS_ORIGINS`: it is a wildcard by default, and narrowing it can lock out the
storefront the client runs on.

**MEGA upload fails with `ENOENT` after a successful wizard run.** A v0.6.0 keychain
bug, where a stale entry for an older address shadowed the working one. Upgrade.

## Updating

`mokuro-bridge --check-update` exits 0 when up to date, 1 when a newer release exists,
and 2 when the check could not be completed. Nothing is applied automatically, because
a restart mid-OCR loses work. `MOKURO_BRIDGE_UPDATE_CHECK=0` turns off the background
check; running `--check-update` by hand still works.

## Still stuck

Read [architecture.md](architecture.md) for how the pieces fit, check
[CHANGELOG.md](../CHANGELOG.md) for whether it was already fixed, and open an
[issue](https://github.com/GolyBidoof/mokuro-bridge/issues) with the `/health` output
and the client's error.
