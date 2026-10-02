#!/usr/bin/env python3
"""mokuro-bridge — compatibility wrapper for ``mokuro-bridge-ocr``.

The implementation moved into ``mokuro_bridge/ocr_folder.py`` so that an
installed wheel can ship the ``mokuro-bridge-ocr`` console script. This file
stays so the README's ``python3 ocr_folder.py "..."`` keeps working from a git
checkout, where it needs only the standard library.
"""

from __future__ import annotations

import sys

from mokuro_bridge.ocr_folder import main

if __name__ == "__main__":
    sys.exit(main())
