"""Nimble-v2 (und Base ohne LoRA) auf den 92 Mantis-Fällen aus bench/jev.

Setup (außerhalb des Repos, ~/models/nimble): llama.cpp-Build, Q4_K_M-Base-GGUF, konvertierter
LoRA, nimble_client.py/nimble_prompt.py, run_server.sh. Server starten, dann:
    cd ~/models/nimble && python3.14 ~/Mantis/bench/jev/nimble/bench_nimble.py
Ergebnis: ~/models/nimble/results/nimble.jsonl + Zusammenfassung auf stdout.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path.home() / "models/nimble"   # nimble_client + Ergebnisse
MANTIS = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(MANTIS))

from bench.jev.run import all_cases, jev_questions  # noqa: E402
from nimble_client import score  # noqa: E402


def to_schema(questions: dict) -> dict:
    """Jev-Frage → flaches Nimble-Feld (boolean bzw. enum mit Beschreibungen)."""
    q = questions["q"]
    if q["type"] == "noul":
        crit = q.get("criteria") or {}
        desc = q["instructions"]
        if crit:
            desc += f"\nJa/true: {crit.get('true', '')}\nNein/false: {crit.get('false', '')}"
        return {"q": {"type": "boolean", "description": desc}}
    return {"q": {"type": "enum", "description": q["instructions"],
                  "choices": list(q["criteria"]),
                  "choice_descriptions": {k: str(v) for k, v in q["criteria"].items()}}}


def evaluate(case: dict, out: dict, qtype: str) -> tuple[object, float, dict]:
    probs = out["q"]["probabilities"]
    if qtype == "noul":
        p = probs["true"]
        return p >= 0.5, abs(p - 0.5) * 2, {"noul": p}
    k = len(probs)
    pmax = max(probs.values())
    return out["q"]["value"], max(0.0, (k * pmax - 1) / (k - 1)), probs


def main() -> None:
    res = HERE / "results"
    res.mkdir(exist_ok=True)
    rows = []
    with open(res / "nimble.jsonl", "w") as f:
        for use_lora in (True, False):
            system = "nimble-v2" if use_lora else "qwen3.5-9b-base"
            for case in all_cases():
                state, questions = jev_questions(case["kind"], case["state"])
                context = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
                t = time.perf_counter()
                try:
                    out = score(context, to_schema(questions), use_lora=use_lora)
                    label, conf, raw = evaluate(case, out, questions["q"]["type"])
                    err = None
                except Exception as e:  # Einzelfall darf den Lauf nicht beenden
                    label, conf, raw, err = None, 0.0, {}, repr(e)[:200]
                row = {**case, "backend": system, "label": label, "correct": label == case["expected"],
                       "confidence": conf, "raw": raw, "latency": time.perf_counter() - t, "error": err}
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(system, case["id"], "ok" if row["correct"] else f"FALSCH ({label})", flush=True)

    for system in ("nimble-v2", "qwen3.5-9b-base"):
        rs = [r for r in rows if r["backend"] == system]
        noul = [r for r in rs if "noul" in r["raw"]]
        brier = statistics.mean((r["raw"]["noul"] - (1.0 if r["expected"] else 0.0)) ** 2 for r in noul)
        sure = [r for r in rs if r["confidence"] >= 0.5]
        print(f"\n## {system}: {sum(r['correct'] for r in rs)}/{len(rs)}  Brier {brier:.3f}  "
              f"conf>=0.5 {sum(r['correct'] for r in sure)}/{len(sure)}  "
              f"Median {statistics.median(r['latency'] for r in rs):.2f}s  Fehler {sum(bool(r['error']) for r in rs)}")
        kinds = sorted({r["kind"] for r in rs}, key=[c["kind"] for c in all_cases()].index)
        print("  " + "  ".join(f"{k} {sum(r['correct'] for r in rs if r['kind']==k)}/{sum(r['kind']==k for r in rs)}" for k in kinds))
        for lo, hi in ((0, .5), (.5, .8), (.8, .95), (.95, 1.01)):
            b = [r for r in rs if lo <= r["confidence"] < hi]
            print(f"  conf {lo}-{hi}: {sum(r['correct'] for r in b)}/{len(b)}")


if __name__ == "__main__":
    main()
