"""``python -m mokuro_bridge`` runs the bridge, the same as the console script.

Useful for an install where the script directory is not on PATH (a bare
``pip install`` into a venv that is not activated), and it is the one
invocation that stays stable across pipx, pip and a git checkout.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
