#!/bin/bash
# Start mokuro-bridge. Picks a Python interpreter, then runs server.py.
#
# Preference order for the interpreter:
#   1. $MOKURO_BRIDGE_PYTHON (if set and executable)
#   2. ./.venv/bin/python     (project virtualenv, if present)
#   3. $PYTHON or python3     (whatever is on PATH)
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${MOKURO_BRIDGE_PYTHON:-}" && -x "${MOKURO_BRIDGE_PYTHON}" ]]; then
  PY="${MOKURO_BRIDGE_PYTHON}"
elif [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

exec "$PY" server.py "$@"
