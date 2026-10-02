#!/usr/bin/env bash
# Install the unofficial Fitatu MCP server into mcp/fitatu via npm.
#
# The server is NOT published to npm — only as a GitHub repo — so package.json
# depends on it by git URL, pinned to an exact reviewed commit. Upstream ships
# no prepared dist/ and has no `prepare` script, so npm cannot build it on
# install; the postinstall hook does that step.
#
# This is third-party code that will hold real Fitatu credentials and log into
# a live account. Review upstream's diff before moving the pin in package.json.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)/mcp/fitatu"

if ! node --version | grep -qE 'v(2[2-9]|[3-9][0-9])'; then
  echo "ERROR: this server needs Node >= 22.18.0; found $(node --version)" >&2
  exit 1
fi

cd "$DIR"
echo "Installing (and building) the pinned Fitatu MCP server"
npm install

if [ ! -f .env ]; then
  umask 177  # create as 0600
  cat > .env <<'ENVEOF'
# Fitatu account credentials. Gitignored; keep this file mode 600.
FITATU_EMAIL=
FITATU_PASSWORD=
ENVEOF
  echo
  echo "Created $DIR/.env (mode 600). Fill in FITATU_EMAIL and FITATU_PASSWORD."
fi
chmod 600 .env

echo
echo "Entry point: $DIR/node_modules/fitatu-mcp/dist/index.js"
