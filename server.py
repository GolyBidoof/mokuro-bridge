"""mokuro-bridge — compatibility entry point.

The CLI lives in ``mokuro_bridge/cli.py`` now, so that an installed wheel can
ship the ``mokuro-bridge`` console script. This file stays because existing
setups point at it: the README quickstart, ``run.sh`` and the launchd plist
all run ``python server.py``.

    python server.py --check-update     # same CLI as the console script

``uvicorn server:app`` keeps working too: the ASGI app is re-exported below.
New setups can use ``mokuro-bridge`` or ``python -m mokuro_bridge`` instead.
"""

from __future__ import annotations

# Imported first on purpose: cli.py wraps its own app import in a try/except
# that turns a missing fastapi/uvicorn into an actionable message naming the
# interpreter. Importing the app directly here would raise the bare
# ModuleNotFoundError that wrapper exists to replace.
from mokuro_bridge.cli import _main, main  # noqa: F401  (_main kept for compat)
from mokuro_bridge.api import app  # noqa: F401  (keeps `uvicorn server:app` working)

if __name__ == "__main__":
    main()
