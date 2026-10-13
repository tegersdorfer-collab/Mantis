#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if [[ -z "${DATABASE_URL:-}" ]]; then
  DATABASE_URL="$(python3 - "$REPO_ROOT" <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
from db_test_config import production_database_url

print(production_database_url(os.environ, Path(sys.argv[1]) / ".env"))
PY
)"
fi
TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql://localhost:5432/mantis_test}"
export DATABASE_URL TEST_DATABASE_URL

python3 - "$REPO_ROOT" "$DATABASE_URL" "$TEST_DATABASE_URL" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
from db_test_config import ensure_test_database_isolated, normalize_database_target

source_url, test_url = sys.argv[2:]
try:
    source_target = normalize_database_target(source_url)
    test_target = normalize_database_target(test_url)
    ensure_test_database_isolated(test_url, (source_url,))
except ValueError as exc:
    raise SystemExit(str(exc)) from None

if test_target[2] != "mantis_test":
    raise SystemExit("Die Testdatenbank muss 'mantis_test' heißen.")
if source_target[:2] != test_target[:2]:
    raise SystemExit("Produktions- und Testdatenbank müssen auf demselben Host und Port liegen.")
PY

if [[ "$(psql "$DATABASE_URL" --no-psqlrc --tuples-only --no-align \
  --set=ON_ERROR_STOP=1 --command="SELECT 1 FROM pg_database WHERE datname = 'mantis_test'")" == "1" ]]; then
  echo "mantis_test existiert bereits; unverändert gelassen."
  exit 0
fi

createdb --maintenance-db="$DATABASE_URL" mantis_test
pg_dump --schema-only --no-owner --no-privileges "$DATABASE_URL" \
  | psql "$TEST_DATABASE_URL" --no-psqlrc --set=ON_ERROR_STOP=1

echo "mantis_test angelegt und Schema aus der Produktionsdatenbank kopiert."
