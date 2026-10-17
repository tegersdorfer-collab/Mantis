#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/web/gym_og/frontend"

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  npm ci --ignore-scripts --prefix "$FRONTEND_DIR"
fi

cd "$FRONTEND_DIR"
# mantis: This build flag disables the bundled catalogue, passkey/account flows, push, and media sync.
VITE_MANTIS_OG=1 VITE_DEMO=0 VITE_MOBILE=0 npx vite build --base=/og/
