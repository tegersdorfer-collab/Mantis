#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/web/gym_og/frontend"

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  npm ci --ignore-scripts --prefix "$FRONTEND_DIR"
fi

cd "$FRONTEND_DIR"
# mantis: This build flag disables the bundled catalogue, passkey/account flows, push, and media sync.
# mantis: The app is served at /, its API stays under /og/api (no clash with the old tracker's /api).
VITE_MANTIS_OG=1 VITE_MANTIS_API_BASE=/og VITE_DEMO=0 VITE_MOBILE=0 npx vite build --base=/
