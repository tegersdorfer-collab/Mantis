#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Dieser Dienst nutzt macOS launchd." >&2
  exit 1
fi

mantis_root="$(cd "$(dirname "$0")/.." && pwd)"
gev_dir="${MANTIS_GEV_DIR:-$mantis_root/data/gods-eye-view}"
node_binary="$(command -v node)"
plist="$HOME/Library/LaunchAgents/com.mantis.gods-eye-view.plist"

if [[ ! -f "$gev_dir/node_modules/vite/bin/vite.js" ]]; then
  echo "GEV ist nicht installiert. Zuerst scripts/setup-gods-eye-view.sh ausführen." >&2
  exit 1
fi

mkdir -p "$(dirname "$plist")" "$mantis_root/data"
python3 - "$plist" "$gev_dir" "$node_binary" "$mantis_root/data" <<'PY'
import plistlib
from pathlib import Path
import sys

plist, gev_dir, node, data_dir = map(Path, sys.argv[1:])
payload = {
    "Label": "com.mantis.gods-eye-view",
    "ProgramArguments": [str(node), str(gev_dir / "node_modules/vite/bin/vite.js"),
                         "--host", "127.0.0.1", "--port", "4173", "--strictPort"],
    "WorkingDirectory": str(gev_dir),
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(data_dir / "gev.stdout.log"),
    "StandardErrorPath": str(data_dir / "gev.stderr.log"),
}
plist.write_bytes(plistlib.dumps(payload))
PY

launchctl bootout "gui/$(id -u)/com.mantis.gods-eye-view" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "Lokaler GEV-Dienst eingerichtet: $plist"
