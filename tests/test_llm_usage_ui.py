"""UI-Formatierung unterscheidet lokale, unbekannte und bezahlte Kosten."""
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_usage_cost_labels_keep_unknown_separate_from_local():
    script = r"""
const assert = require("node:assert/strict");
const usage = require(process.argv[1]);
const currency = value => "$" + Number(value).toFixed(2);
assert.equal(usage.formatCost(null, "openrouter", currency), "unbekannt");
assert.equal(usage.formatCost(undefined, "openrouter", currency), "unbekannt");
assert.equal(usage.formatCost(0, "ollama", currency), "lokal");
assert.equal(usage.formatCost(0, "openrouter", currency), "$0.00");
assert.equal(usage.formatCost(0.035, "openrouter", currency), "$0.04");
assert.equal(usage.totalsNote, "(ohne unbekannte)");
"""
    subprocess.run(
        ["node", "-e", script, str(ROOT / "web" / "usage_costs.js")],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=5,
    )
