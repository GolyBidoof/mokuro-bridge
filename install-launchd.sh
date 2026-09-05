#!/bin/bash
# Install / load the launchd agent that keeps mokuro-bridge running (macOS).
#
# Usage: ./install-launchd.sh [path-to-python]
#   The interpreter defaults to MOKURO_BRIDGE_PYTHON, then ./.venv/bin/python,
#   then the Python that has fastapi+uvicorn installed (fallback: python3).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.mokuro-bridge"
PLIST_SRC="$DIR/com.mokuro-bridge.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -n "${1:-}" && -x "$1" ]]; then
  PYTHON3="$1"
elif [[ -n "${MOKURO_BRIDGE_PYTHON:-}" && -x "${MOKURO_BRIDGE_PYTHON}" ]]; then
  PYTHON3="$MOKURO_BRIDGE_PYTHON"
elif [[ -x "$DIR/.venv/bin/python" ]]; then
  PYTHON3="$DIR/.venv/bin/python"
else
  # Pick a python3 that has the server deps importable.
  PYTHON3="$(which python3)"
  if ! "$PYTHON3" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
    echo "error: $PYTHON3 lacks fastapi/uvicorn." >&2
    echo "       Pass an interpreter: ./install-launchd.sh /path/to/python3" >&2
    exit 1
  fi
fi

SERVER="$DIR/server.py"
PORT="${MOKURO_BRIDGE_PORT:-62642}"   # 62642 spells "MANGA" on a phone keypad

# Rewrite plist with absolute paths for this machine
mkdir -p "$HOME/Library/LaunchAgents"
sed \
  -e "s|__PYTHON3__|${PYTHON3}|g" \
  -e "s|__SERVER__|${SERVER}|g" \
  -e "s|__WORKDIR__|${DIR}|g" \
  -e "s|__HOME__|${HOME}|g" \
  -e "s|__PORT__|${PORT}|g" \
  "$PLIST_SRC" > "$PLIST_DST"

# Unload any previous install of this label
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

# Legacy: unload + remove the pre-rename agent (com.bw-mokuro-bridge), if any
if launchctl print "gui/$(id -u)/com.bw-mokuro-bridge" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/com.bw-mokuro-bridge" 2>/dev/null || true
fi
rm -f "$HOME/Library/LaunchAgents/com.bw-mokuro-bridge.plist"

launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "mokuro-bridge started via launchd ($PYTHON3). Health check:"
sleep 1
curl -s "http://127.0.0.1:${PORT}/health" | python3 -m json.tool || echo "(still starting — try again in a few seconds)"
echo ""
echo "Logs: tail -f ~/Library/Logs/mokuro-bridge.log"
echo "Stop: launchctl bootout gui/\$(id -u)/$LABEL"
