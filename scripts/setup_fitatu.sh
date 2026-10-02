#!/usr/bin/env bash
# Set up the unofficial Fitatu MCP server locally.
#
# This is third-party code that will hold your real Fitatu credentials and log
# into your live account, so the clone is pinned to a specific commit rather
# than tracking main. Review the diff before moving the pin.
set -euo pipefail

REPO="https://github.com/AndekQR/fitatu-mcp-unofficial"
DEST="$(cd "$(dirname "$0")/.." && pwd)/vendor/fitatu-mcp"
PIN_FILE="$(cd "$(dirname "$0")/.." && pwd)/vendor/PINNED_COMMIT"

if ! node --version | grep -qE 'v(2[2-9]|[3-9][0-9])'; then
  echo "ERROR: this server needs Node >= 22.18.0; found $(node --version)" >&2
  exit 1
fi

if [ ! -d "$DEST/.git" ]; then
  echo "Cloning $REPO -> $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone --quiet "$REPO" "$DEST"
fi

cd "$DEST"
if [ -f "$PIN_FILE" ]; then
  PIN="$(cat "$PIN_FILE")"
  echo "Checking out pinned commit $PIN"
  git fetch --quiet origin
  git checkout --quiet "$PIN"
else
  git rev-parse HEAD > "$PIN_FILE"
  echo "Pinned to $(cat "$PIN_FILE")"
fi

echo "Installing dependencies and building"
npm install --silent
npm run build

if [ ! -f "$DEST/.env" ]; then
  umask 177  # creates the file as 0600
  cat > "$DEST/.env" <<'ENVEOF'
# Fitatu account credentials. This file is gitignored. Keep it mode 600.
FITATU_EMAIL=
FITATU_PASSWORD=
ENVEOF
  echo
  echo "Created $DEST/.env (mode 600). Fill in FITATU_EMAIL and FITATU_PASSWORD."
fi

chmod 600 "$DEST/.env"
echo
echo "Built. Entry point: $DEST/dist/index.js"
