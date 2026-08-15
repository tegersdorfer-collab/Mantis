#!/usr/bin/env python3
"""
Erkundet das COROS-MCP: dumpt den Tool-Katalog und je eine Beispiel-Antwort.

Ausführen (nach scripts/coros_auth.py):
  cd /Users/timoegersdorfer/Mantis
  python3 scripts/coros_probe.py

Schreibt nach tests/fixtures/coros/. Die Feldnamen der COROS-Antworten sind
nirgends dokumentiert — diese Dateien sind die Grundlage für das Mapping in
Phase B. Reines Lese-Werkzeug, ändert nichts an Mantis.
"""
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from domains.coros import oauth
from domains.coros.client import CorosClient

OUT = ROOT / "tests" / "fixtures" / "coros"

# Nur lesende Tools. downloadActivityFitFiles bleibt bewusst draußen (Limit 50/Tag).
WANTED = [
    "queryUserInfo", "queryDevices",
    "queryDailyHealthData", "querySleepData", "querySleepHrv",
    "queryRestingHeartRate", "queryAvgHeartRate", "queryStressLevel",
    "queryRecoveryStatus", "queryTrainingLoadAssessment",
    "queryFitnessAssessmentOverview", "querySportRecords",
    "queryTrainingSchedule",
]

TODAY = date.today()
START = TODAY - timedelta(days=7)


def guess_args(schema: dict) -> dict:
    """Pflichtfelder aus dem inputSchema heuristisch füllen.

    Datumsartige Namen bekommen ISO-Daten der letzten Woche, Zahlen eine 1,
    Booleans False. Alles Geratene steht in der Fixture, ist also überprüfbar.
    """
    props = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []
    args = {}
    for name in required:
        spec = props.get(name, {})
        typ = spec.get("type", "string")
        low = name.lower()
        if typ in ("integer", "number"):
            # COROS nutzt an manchen Stellen YYYYMMDD als Zahl statt ISO-String.
            args[name] = int(TODAY.strftime("%Y%m%d")) if "date" in low or "day" in low else 1
        elif typ == "boolean":
            args[name] = False
        elif "start" in low or "from" in low or "begin" in low:
            args[name] = START.isoformat()
        elif "end" in low or "to" in low or "until" in low:
            args[name] = TODAY.isoformat()
        elif "date" in low or "day" in low:
            args[name] = TODAY.isoformat()
        else:
            args[name] = ""
    return args


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        oauth.access_token()
    except oauth.CorosNotAuthorized as e:
        print(f"❌ {e}")
        sys.exit(1)

    with CorosClient() as c:
        tools = c.list_tools()
        (OUT / "_tools.json").write_text(
            json.dumps(tools, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"📚 {len(tools)} Tools im Katalog → tests/fixtures/coros/_tools.json")

        by_name = {t["name"]: t for t in tools}
        unknown = [n for n in WANTED if n not in by_name]
        if unknown:
            print(f"⚠️  Nicht im Katalog (Doku veraltet?): {', '.join(unknown)}")

        ok = fail = 0
        for name in WANTED:
            tool = by_name.get(name)
            if not tool:
                continue
            args = guess_args(tool.get("inputSchema"))
            try:
                result = c.call_tool(name, args)
                record = {"tool": name, "arguments": args, "result": result}
                ok += 1
                print(f"  ✅ {name}  args={args}")
            except Exception as e:
                record = {"tool": name, "arguments": args, "error": f"{type(e).__name__}: {e}"}
                fail += 1
                print(f"  ❌ {name}  args={args}  → {e}")
            (OUT / f"{name}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            time.sleep(1)  # höflich bleiben

    print(f"\n{ok} erfolgreich, {fail} fehlgeschlagen. Dateien in {OUT}")
    if fail:
        print("Bei Fehlschlägen: das inputSchema in _tools.json ansehen und die "
              "Argumente in WANTED/guess_args nachziehen.")


if __name__ == "__main__":
    main()
