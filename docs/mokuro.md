# The mokuro fork

mokuro-bridge drives whichever `mokuro` is installed in its own Python environment. Two engines exist:

- **upstream**, kha-white's PyPI package, installed with `pip install mokuro`
- **the fork**, [GolyBidoof/mokuro](https://github.com/GolyBidoof/mokuro), a performance fork of it, installed from its `v0.4.0` release

The fork declares the same distribution name, `mokuro`, which is the whole reason the swap is clean. pip sees an upgrade of one distribution: it removes the old copy, installs the fork and replaces the `mokuro` console script. A differently named package would coexist instead, and the two would share files.

## Why the fork is faster

The stock engine recognizes text one crop at a time. The fork adds a batched path: `MangaPageOcr.detect_and_extract` returns every text region on a page, then one `recognize_text` call runs over all the crops of a chunk, with `MokuroGenerator.ocr_batch_size` controlling the chunk size. mokuro-bridge detects that API at runtime and uses it when present, so no configuration is needed beyond installing it. Everything the bridge already does (chunked OCR with an idle flush, per-page progress) stays the same; the fork just spends less time per chunk.

Capability, not the version string, is how the bridge tells them apart. An editable install of the fork left `importlib.metadata.version("mokuro")` reporting a stale number on one machine, so the bridge reads the install provenance in `direct_url.json` and the module path instead. `/health` reports the result as `mokuro_flavor`, one of `upstream`, `fork`, `absent` or `unknown`.

## Install it

The one command that does the whole swap, including the uninstall and a verification import in a fresh interpreter:

```bash
mokuro-bridge --install-mokuro
```

It prints what is installed now, prints every command before running it, replaces the distribution, then reports the before and after flavor and version. Exit codes: 0 when the fork is installed and imports, 1 when the install or the verification failed, 2 when it refused.

It refuses rather than guesses in two cases, both exit 2:

- an externally managed interpreter (PEP 668), which is a system Python that pip must not install into. Create a virtualenv, or install the bridge with pipx or uv, and run the command there.
- a mokuro-bridge install whose method it cannot tell apart. It prints the target and runs nothing.

When the fork is already installed it reports that and runs nothing.

The commands it runs, per detected environment, are these. The uninstall comes first in each because the two engines share the distribution name.

For a plain pip venv or a git checkout (`./run.sh`), the interpreter that runs the bridge:

```bash
python3 -m pip uninstall -y mokuro
python3 -m pip install --upgrade "mokuro @ https://github.com/GolyBidoof/mokuro/releases/download/v0.4.0/mokuro-0.4.0-py3-none-any.whl"
```

For a `pipx install mokuro-bridge`:

```bash
pipx runpip mokuro-bridge uninstall -y mokuro
pipx runpip mokuro-bridge install --upgrade "mokuro @ https://github.com/GolyBidoof/mokuro/releases/download/v0.4.0/mokuro-0.4.0-py3-none-any.whl"
```

For a `uv tool install mokuro-bridge`, one command, because a uv tool environment has no pip of its own and `--force` is what makes this a replacement:

```bash
uv tool install --force --with "mokuro @ https://github.com/GolyBidoof/mokuro/releases/download/v0.4.0/mokuro-0.4.0-py3-none-any.whl" mokuro-bridge
```

The install target is the wheel attached to the `v0.4.0` release, which installs faster than building from source. If that asset is ever removed, the fallback is the tag itself, `mokuro @ git+https://github.com/GolyBidoof/mokuro.git@v0.4.0`; `mokuro_bridge/mokuro_flavor.py` holds both constants, and `INSTALL_TARGET` is the one to change.

Alternatively, point the bridge at a checkout you already have without installing anything, by setting `MOKURO_REPO` to it. That works for either engine and is what `/health` calls `mokuro_custom_fork`.

## Go back to upstream

Uninstall the fork and install the PyPI package in the same environment. For a plain pip venv or a checkout:

```bash
python3 -m pip uninstall -y mokuro
python3 -m pip install mokuro
```

For pipx, use `pipx runpip mokuro-bridge uninstall -y mokuro` and `pipx runpip mokuro-bridge install mokuro`. For uv, `uv tool install --force mokuro-bridge` recreates the tool environment without the `--with` overlay. Restart the bridge afterwards and `/health` reports `mokuro_flavor: upstream`.

## The startup notice

When the service starts and the installed engine is `upstream` or `unknown`, it logs one line naming the fork and the command above. It reads local metadata only, so it never delays startup and never raises. It is silent when the fork is already installed and when no engine is installed at all. Set `MOKURO_BRIDGE_MOKURO_NOTICE=0` to turn it off. That switch is separate from `MOKURO_BRIDGE_UPDATE_CHECK`, which governs the mokuro-bridge release notice: they are different questions, and one can be wanted without the other.

## The honest caveat

pipx and uv isolate every tool in its own environment. "Install the fork" therefore means one install per environment, not one change to the machine. If the bridge runs from a pipx venv, installing the fork into your system Python or your shell's active virtualenv does nothing for the service, and `/health` will keep saying `upstream`. The install command targets the environment that runs mokuro-bridge, which is exactly why it detects the method first and refuses when it cannot.
