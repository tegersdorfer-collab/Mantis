"""Wacht darüber, dass keine echten Messwerte aus data/coros_samples/ im Repo landen.

Dieser Fehler ist dreimal passiert: zweimal in Fixtures, einmal in Docstrings und
Doku. Die Prüfung sucht den längsten Lauf aufeinanderfolgender Zeilen mit Ziffern,
der sowohl in einer getrackten Datei als auch in einem echten Dump vorkommt.
Einzelne Zufallstreffer sind unvermeidlich; ein reproduzierter Datensatz nicht.
"""
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SAMPLES_DIR = REPO_ROOT / "data" / "coros_samples"

CANDIDATES = [
    REPO_ROOT / "domains" / "coros" / "mapping.py",
    REPO_ROOT / "domains" / "coros" / "importer.py",
    *sorted((REPO_ROOT / "tests" / "fixtures" / "coros").glob("*.txt")),
    REPO_ROOT / "docs" / "superpowers" / "plans" / "2026-08-16-coros-mcp-phase-b-import.md",
    REPO_ROOT / "docs" / "superpowers" / "specs" / "2026-08-15-coros-mcp-health-sync-design.md",
]

_DIGIT = re.compile(r"\d")

# Ab dieser Lauflänge gilt ein Treffer als reproduzierter Datensatz, nicht mehr
# als Zufall. Zwei Zeilen kommen nachweislich zufällig vor (sleep_data.txt).
_MIN_VIOLATION_LEN = 3


def _digit_lines(text: str) -> list[str]:
    """Nur Zeilen mit mindestens einer Ziffer, in Originalreihenfolge — alles
    andere (Leerzeilen, Überschriften, reiner Fließtext dazwischen) rausgefiltert.
    Damit stören Formatierungsunterschiede (Docstring-Einrückung, ein anderes Feld
    dazwischen wie 'Sleep Summary:') den Lauf nicht, so wie es das echte Leck in
    mapping.py auch nicht getan hat."""
    return [ln.strip() for ln in text.splitlines() if _DIGIT.search(ln)]


def _longest_common_run(a: list[str], b: list[str]) -> tuple[int, list[str]]:
    """Länge und Inhalt des längsten gemeinsamen ZUSAMMENHÄNGENDEN Laufs (nicht
    Teilsequenz — die Zeilen müssen in beiden Listen direkt aufeinanderfolgen)."""
    if not a or not b:
        return 0, []
    prev = [0] * (len(b) + 1)
    best_len = 0
    best_end = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                v = prev[j - 1] + 1
                curr[j] = v
                if v > best_len:
                    best_len = v
                    best_end = i
        prev = curr
    return best_len, a[best_end - best_len:best_end]


def _load_dumps() -> dict[str, list[str]]:
    """Alle echten Dumps unwrappen: result.content[0].text ist selbst JSON-codiert
    (doppelt verschachtelt) — erst der äußere Parse, dann json.loads auf den
    Text-Wert, um die eigentliche Prosa mit echten Zeilenumbrüchen zu bekommen."""
    dumps: dict[str, list[str]] = {}
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            text = raw["result"]["content"][0]["text"]
            prose = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # kein Tool-Result in diesem Format (z.B. _tools.json)
        if isinstance(prose, str):
            dumps[path.name] = _digit_lines(prose)
    return dumps


def test_no_real_health_data_leaked_into_tracked_files():
    if not SAMPLES_DIR.is_dir():
        pytest.skip("data/coros_samples/ nicht vorhanden (gitignored) — "
                     "Test läuft nur auf Maschinen mit echten Dumps")

    dumps = _load_dumps()
    if not dumps:
        pytest.skip("keine verwertbaren Dumps unter data/coros_samples/ gefunden")

    violations = []
    for candidate in CANDIDATES:
        if not candidate.exists():
            continue
        cand_lines = _digit_lines(candidate.read_text(encoding="utf-8"))
        for dump_name, dump_lines in dumps.items():
            length, run = _longest_common_run(cand_lines, dump_lines)
            if length >= _MIN_VIOLATION_LEN:
                violations.append((candidate.relative_to(REPO_ROOT), dump_name, length, run))

    if violations:
        details = "\n".join(
            f"  {f} <-> data/coros_samples/{d}: {n} Zeilen identisch:\n"
            + "\n".join(f"      {line}" for line in run)
            for f, d, n, run in violations
        )
        pytest.fail(
            "Echte Gesundheitsdaten aus data/coros_samples/ im getrackten Repo "
            f"gefunden:\n{details}"
        )
