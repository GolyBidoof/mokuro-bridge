#!/bin/bash
# macOS-only convenience wrapper: store MEGA credentials in the login Keychain
# (service 'mega.nz', protocol https) for mokuro-bridge / megatools.
#
# Cross-platform alternative (works on macOS, Windows and Linux):
#   python3 server.py --setup-mega
# which stores in the OS keychain/credential store or a 0600 credentials file.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "This will store your MEGA email + password in the login Keychain"
echo "under service 'mega.nz' (protocol https)."
echo ""

read -r -p "MEGA email: " MEGA_EMAIL
read -r -s -p "MEGA password: " MEGA_PASSWORD
echo ""

# -U updates if an entry already exists; -T whitelists the binaries that may
# read it (megatools path varies by install method — extra -T entries are ok).
security add-internet-password \
  -s mega.nz \
  -r htps \
  -a "$MEGA_EMAIL" \
  -w "$MEGA_PASSWORD" \
  -T /opt/homebrew/bin/megatools \
  -T /usr/local/bin/megatools \
  -T /usr/bin/security \
  -U

echo ""
echo "Stored. Verifying read access..."
if security find-internet-password -s mega.nz -r htps -w >/dev/null 2>&1; then
  echo "OK — password is readable from Keychain."
else
  echo "WARNING: could not read password back. You may need to unlock Keychain."
fi

echo ""
echo "Next: start the bridge and check health:"
echo "  $DIR/run.sh"
echo "  curl -s http://127.0.0.1:${MOKURO_BRIDGE_PORT:-62642}/health | python3 -m json.tool"
echo ""
echo "Prefer env vars? Export MEGA_EMAIL and MEGA_PASSWORD instead."
