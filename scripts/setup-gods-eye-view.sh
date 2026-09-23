#!/usr/bin/env bash
set -euo pipefail

mantis_root="$(cd "$(dirname "$0")/.." && pwd)"
gev_dir="${MANTIS_GEV_DIR:-$mantis_root/data/gods-eye-view}"
gev_commit="a65d9d85f1faa06ae7df235d7fa8a29b026b7a5b"

node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (!((major === 24 && minor >= 14) || major === 26)) process.exit(1)' || {
  echo "God's Eye View benötigt Node.js 24.14+ oder 26.x." >&2
  exit 1
}

if [[ ! -d "$gev_dir/.git" ]]; then
  git clone --filter=blob:none https://github.com/bilawalsidhu/gods-eye-view.git "$gev_dir"
fi

if [[ "$(git -C "$gev_dir" rev-parse HEAD)" != "$gev_commit" ]]; then
  if [[ -n "$(git -C "$gev_dir" status --porcelain)" ]]; then
    echo "GEV-Verzeichnis enthält Änderungen; manuellen Stand prüfen: $gev_dir" >&2
    exit 1
  fi
  git -C "$gev_dir" fetch --depth 1 origin "$gev_commit"
  git -C "$gev_dir" checkout --detach "$gev_commit"
fi

cp "$mantis_root/integrations/gev/bridge.mjs" "$gev_dir/src/mantis-bridge.mjs"

python3 - "$gev_dir" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
entry = root / "src/main.js"
content = entry.read_text()
marker = "// mantis-bridge: lokal eingebundene Steuerung"
if marker not in content:
    content += """

// mantis-bridge: lokal eingebundene Steuerung
import { installBridge } from './mantis-bridge.mjs';
application.subscribe((state) => {
  if (state.status !== 'ready') return;
  installBridge({
    allowedOrigins: ['http://127.0.0.1:7779', 'http://localhost:7779'],
    runAction: (name, args) => window.__godsEyeView.voiceCommands.runner(name, args),
  });
});
"""
    entry.write_text(content)

config = root / "build/vite.js"
content = config.read_text()
old = "'X-Frame-Options': 'DENY',\n        'Content-Security-Policy': \"frame-ancestors 'none'\","
new = "'Content-Security-Policy': 'frame-ancestors http://127.0.0.1:7779 http://localhost:7779',"
if old in content:
    config.write_text(content.replace(old, new, 1))
elif new not in content:
    raise SystemExit("Upstream-Frame-Header haben sich geändert; Integration prüfen")
PY

npm --prefix "$gev_dir" ci
npm --prefix "$gev_dir" run doctor
echo "GEV installiert: $gev_dir"
echo "Start: cd '$gev_dir' && npm run dev -- --host 127.0.0.1 --port 4173"
