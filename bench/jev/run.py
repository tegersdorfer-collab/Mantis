"""Jev (TypeSafe via OpenRouter) gegen die lokalen Mantis-Modelle auf den Fällen in cases.py.

Aufruf:  cd ~/Mantis && python -m bench.jev.run [--local gemma4:e2b,qwen3.5:9b] [--no-local]
Ergebnis: bench/jev/results/raw.jsonl + report.md

Jev bekommt pro Fall EINEN Call mit der/den passenden Frage(n). Lokal bekommt jedes
Modell den Prompt-Stil, den Mantis heute an der jeweiligen Stelle verwendet
("Antworte NUR mit ..."), Antwort per startswith/Exact-Match ausgewertet — genau so,
wie der Produktivcode es tut.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import ollama

from bench.jev import cases as C

HERE = Path(__file__).parent
RESULTS = HERE / "results"
OR_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"


def _load_env_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        for line in (HERE.parent.parent / ".env").read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("OPENROUTER_API_KEY fehlt")
    return key


# ── Jev ───────────────────────────────────────────────────────────────────────

def jev_call(key: str, state, questions: dict) -> tuple[dict, float]:
    body = {"model": JEV_MODEL, "state": state, "questions": questions}
    req = urllib.request.Request(OR_URL, data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t = time.perf_counter()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r), time.perf_counter() - t
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode(errors="replace")[:300]
            if e.code in (429, 529, 500, 502, 503, 520, 522, 524) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {body_txt}") from e
    raise RuntimeError("unreachable")


def jev_questions(kind: str, state) -> tuple[object, dict]:
    """Baut (state, questions) für einen Fall-Typ. Fragen-IDs sind nur für unseren Code."""
    if kind == "intent":
        return state, {"q": {"type": "choice",
                             "instructions": "Welcher Bereich des persönlichen Assistenten ist für diese Nutzer-Nachricht zuständig?",
                             "criteria": C.INTENT_CRITERIA}}
    if kind == "address":
        return {"transkript": state}, {"q": {"type": "noul",
            "instructions": "Das Transkript ist eine Anfrage oder ein Befehl an den persönlichen Sprachassistenten im Raum "
                            "(auch ohne Namensnennung), nicht Selbstgespräch oder Gespräch mit einer anderen Person.",
            "criteria": {"true": "Der Sprecher will, dass der Assistent reagiert",
                         "false": "Beiläufiges Gerede, Selbstgespräch, Gespräch mit jemand anderem"}}}
    if kind == "gate":
        return state, {"q": {"type": "noul",
            "instructions": "Die Nachricht verlangt eine AKTION des Assistenten, für die ein Tool nötig ist "
                            "(etwas anlegen, ändern, abhaken, suchen oder gespeicherte Daten abrufen).",
            "criteria": {"true": "Tool nötig: anlegen, ändern, abhaken, Websuche, Daten des Nutzers abrufen",
                         "false": "Reines Gespräch, Meinung, Erklärung aus Allgemeinwissen, Dank"}}}
    if kind == "proactive":
        return {"gedanke_des_assistenten": state}, {"q": {"type": "noul",
            "instructions": "Der Assistent soll diesen Gedanken JETZT ungefragt an den Nutzer schicken. "
                            "Ja nur, wenn ALLES gilt: konkreter unmittelbarer Mehrwert; neue Information oder offene Aktion; "
                            "kein Coaching, keine Verhaltensanalyse, kein Moralappell; kündigt nichts an, was nicht in der "
                            "Nachricht selbst passiert; behauptet keine Aktion, die der Assistent nicht ausführen darf.",
            "criteria": {"true": "Ein guter Freund würde genau das jetzt schreiben",
                         "false": "Generisch, austauschbar, belehrend, Ankündigung, erfundene Aktion — im Zweifel nein"}}}
    if kind == "inbox":
        return {"notiz": state}, {"q": {"type": "choice",
            "instructions": "In welche Kategorie des Wissenssystems gehört diese Notiz?",
            "criteria": C.INBOX_CRITERIA}}
    if kind == "task":
        return {"aufgabe": state}, {"q": {"type": "choice",
            "instructions": "Kann der KI-Assistent diese Aufgabe selbst erledigen (Recherche, Texte, Analyse, Kalender, Notizen) "
                            "oder erfordert sie zwingend die physische Anwesenheit des Nutzers?",
            "criteria": {"mantis": "Der Assistent erledigt es: Recherche, Analyse, Texte, Pläne, Einträge, Auswertungen",
                         "user":   "Nur der Nutzer kann es: hingehen, abholen, kaufen, anrufen, treffen, Gerät bedienen"}}}
    if kind == "conflict":
        return {"alte_aussage": state["old"], "neue_aussage": state["new"]}, {"q": {"type": "noul",
            "instructions": "Die neue Aussage macht die alte veraltet oder widerspricht ihr direkt "
                            "(Umzug, Wechsel, geänderte Präferenz oder Status). Zwei Dinge, die gleichzeitig wahr sein können, "
                            "sind KEIN Widerspruch.",
            "criteria": {"true": "Alte Aussage ist jetzt überholt", "false": "Beides kann zugleich gelten"}}}
    if kind == "verify":
        return {"sprecher": "Timo (der Nutzer, spricht in der ersten Person)", "text": state["text"],
                "behauptung": state["claim"]}, {"q": {"type": "noul",
            "instructions": "Die Behauptung wird im Text wörtlich oder eindeutig direkt gestützt — keine Vermutung, "
                            "keine Verallgemeinerung, keine Verwechslung der Person.",
            "criteria": {"true": "Steht so im Text", "false": "Nicht belegt, überinterpretiert oder falsche Person"}}}
    raise ValueError(kind)


def jev_decide(kind: str, answer: dict):
    """Antwort → (label, konfidenz-ähnlicher Wert in [0,1], rohdaten)."""
    if answer["type"] == "choice":
        return answer["choice"], answer["confidence"], answer["probabilities"]
    if answer["type"] == "noul":
        p = answer["noul"]
        return p >= 0.5, abs(p - 0.5) * 2, {"noul": p}
    raise ValueError(answer["type"])


# ── Lokal (Mantis-Prompt-Stil) ────────────────────────────────────────────────

def local_prompt(kind: str, state) -> tuple[str, callable]:
    """Prompt + Parser, wie Mantis an der jeweiligen Stelle heute arbeitet."""
    def ja_nein(out: str) -> bool:
        return out.strip().upper().startswith(("JA", "YES"))

    if kind == "intent":
        labels = " | ".join(C.INTENT_CRITERIA)
        p = ("Du bist der Routing-Layer eines Assistenten. Ordne die Nutzer-Nachricht GENAU EINER "
             f"Kategorie zu.\nKategorien: {labels}\n'chat' = reiner Smalltalk/keine der anderen. "
             f"Antworte NUR mit dem Kategorie-Wort, sonst nichts.\n\nNachricht: \"{state}\"\nKategorie:")
        return p, lambda o: o.strip().lower().split()[0].strip(".,") if o.strip() else ""
    if kind == "address":
        p = ("Ist dieser Satz eine Anfrage oder ein Befehl an einen persönlichen KI-Assistenten namens Mantis "
             "(nicht nur Small Talk mit jemand anderem im Raum), auch wenn der Name 'Mantis' nicht genannt wird?"
             f"\n\n\"{state}\"\n\nAntworte NUR mit JA oder NEIN.")
        return p, ja_nein
    if kind == "gate":
        p = ("Braucht diese Nachricht eine AKTION (etwas anlegen/ändern/abrufen, Tool nötig) oder ist es "
             f"reines GESPRAECH? Antworte NUR mit AKTION oder GESPRAECH.\n\nNachricht: \"{state}\"\nAntwort:")
        return p, lambda o: o.strip().upper().startswith("AKTION")
    if kind == "proactive":
        p = (f"Du bist Mantis. Du hast gerade diesen Gedanken generiert:\n\n\"{state}\"\n\n"
             "Entscheide: Soll ich das JETZT an Timo schicken?\n\n"
             "Strenge Kriterien für JA (ALLE müssen erfüllt sein):\n- Konkreter, unmittelbarer Mehrwert für Timo\n"
             "- Neue Information die er noch nicht kennt oder eine offene Aktion\n- Kein Coaching, keine Verhaltensanalyse, kein Urteil\n"
             "- Würde ein guter Freund das auch schicken?\n\nKriterien für NEIN (eines reicht):\n- Generisch oder austauschbar\n"
             "- Keine neuen Informationen\n- Klingt nach Coaching oder Moralappell\n- Timo könnte es als nervig oder aufdringlich empfinden\n"
             "- Kündigt etwas an was nicht in dieser Nachricht passiert (\"ich sende dir gleich...\")\n"
             "- Behauptet etwas getan zu haben was nicht wirklich passiert ist\n- Im Zweifel: NEIN\n\nAntworte NUR mit: JA oder NEIN")
        return p, ja_nein
    if kind == "inbox":
        p = ("Du bist Mantis' Wissens-Sortierer. Ordne diese Notiz einer Kategorie zu.\n\n"
             f"Titel: {state['title']}\nInhalt: {state['content'][:400]}\n\nKategorien:\n"
             + "".join(f"- {k}: {v}\n" for k, v in C.INBOX_CRITERIA.items())
             + "\nAntworte NUR mit einem dieser Wörter: " + " | ".join(C.INBOX_CRITERIA))
        return p, lambda o: o.strip().lower().split()[0].strip(".,") if o.strip() else ""
    if kind == "task":
        notes = f"Notiz: {state['notes']}" if state.get("notes") else ""
        p = (f"Du bist Mantis. Neue Aufgabe:\n\nTitel: {state['title']}\n{notes}\n\n"
             "Entscheide: Soll ich (Mantis) diese Aufgabe übernehmen oder Timo selbst?\n\n"
             "Standard: MANTIS – ich übernehme alles was ich auch nur ansatzweise erledigen kann.\nIm Zweifel immer MANTIS.\n\n"
             "Nur USER wenn die Aufgabe ZWINGEND physische Präsenz erfordert:\n- Einkaufen, Pakete abholen, irgendwo hinfahren\n"
             "- Jemanden persönlich anrufen oder treffen\n- Physische Objekte bedienen (Gerät kaufen, reparieren lassen)\n\n"
             "Alles andere gehört zu MANTIS:\n- Recherche, Analyse, Texte, Pläne, Entwürfe\n- Daten auswerten\n"
             "- Events, Erinnerungen, Notizen anlegen\n\nAntworte NUR mit: MANTIS oder USER")
        return p, lambda o: "mantis" if o.strip().upper().startswith("MANTIS") else "user"
    if kind == "conflict":
        p = (f"Alte Aussage über Timo: \"{state['old']}\"\nNeue Aussage über Timo: \"{state['new']}\"\n\n"
             "Macht die NEUE Aussage die ALTE veraltet oder widerspricht ihr direkt "
             "(z.B. Umzug, Jobwechsel, geänderte Präferenz, geänderter Status)? "
             "Zwei Dinge, die gleichzeitig wahr sein können, sind KEIN Widerspruch. Antworte NUR mit JA oder NEIN.")
        return p, ja_nein
    if kind == "verify":
        p = (f"Antworte NUR mit JA oder NEIN, nichts anderes.\n\nText: \"{state['text'][:500]}\"\n"
             f"Behauptung: \"{state['claim']}\"\n\nWird die Behauptung im Text wörtlich oder eindeutig direkt erwähnt?")
        return p, ja_nein
    raise ValueError(kind)


def _ram_available_mb() -> float:
    """frei + inaktiv laut vm_stat (macOS). Konservativ: purgeable/komprimiert zählt nicht."""
    import subprocess, re
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))
    pages = {k.strip(): int(v.strip(" .")) for k, v in
             (l.split(":") for l in out.splitlines()[1:] if ":" in l)}
    return (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)) * page / 1048576


async def unload_all(client: ollama.AsyncClient) -> None:
    """Alle residenten Ollama-Modelle rauswerfen — auf 16 GB darf nur eins zur Zeit leben."""
    try:
        for m in (await client.ps()).models:
            await client.generate(model=m.model, prompt="", keep_alive=0)
    except Exception as e:
        print(f"  ! unload: {e}")


async def model_size_mb(client: ollama.AsyncClient, model: str) -> float:
    for m in (await client.list()).models:
        if m.model == model:
            return m.size / 1048576
    return 0.0


async def local_call(client: ollama.AsyncClient, model: str, prompt: str) -> tuple[str, float]:
    t = time.perf_counter()
    # keep_alive kurz: das Modell lebt nur solange der Benchmark es braucht, dann ist es weg
    resp = await client.chat(model=model, messages=[{"role": "user", "content": prompt}],
                             options={"temperature": 0.0, "num_predict": 8}, keep_alive="60s", think=False)
    return (resp.message.content or ""), time.perf_counter() - t


# ── Fälle einsammeln ──────────────────────────────────────────────────────────

def all_cases() -> list[dict]:
    out = []
    for kind, rows in (("intent", C.INTENT), ("address", C.ADDRESS), ("gate", C.GATE), ("proactive", C.PROACTIVE),
                       ("inbox", C.INBOX), ("task", C.TASK_CLASSIFY), ("conflict", C.CONFLICT), ("verify", C.VERIFY)):
        for row in rows:
            cid, state, expected = row[0], row[1], row[2]
            out.append({"id": cid, "kind": kind, "state": state, "expected": expected,
                        "note": row[3] if len(row) > 3 else ""})
    return out


# ── Hauptlauf ─────────────────────────────────────────────────────────────────

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default="gemma4:e2b,qwen3.5:9b")
    ap.add_argument("--no-local", action="store_true")
    ap.add_argument("--no-jev", action="store_true")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    raw = RESULTS / "raw.jsonl"
    raw.write_text("")
    cases = all_cases()
    rows: list[dict] = []

    def emit(r: dict) -> None:
        rows.append(r)
        with raw.open("a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if not args.no_jev:
        key = _load_env_key()
        print(f"Jev: {len(cases)} Fälle …", flush=True)
        for c in cases:
            state, qs = jev_questions(c["kind"], c["state"])
            try:
                resp, lat = jev_call(key, state, qs)
                label, conf, rawans = jev_decide(c["kind"], resp["answers"]["q"])
                emit({**c, "backend": "jev", "label": label, "correct": label == c["expected"],
                      "confidence": conf, "raw": rawans, "latency": lat,
                      "tokens": resp["usage"]["input_tokens"], "cost": resp["usage"].get("cost")})
            except Exception as e:
                emit({**c, "backend": "jev", "error": str(e)[:200], "correct": False, "latency": None})
                print(f"  ! {c['id']}: {e}", flush=True)
        print("Jev fertig.", flush=True)

    if not args.no_local:
        client = ollama.AsyncClient(host=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
        models = [m for m in args.local.split(",") if m]
        # der Verifier läuft heute auf qwen2.5:0.5b — der gehört als Ist-Zustand mit rein
        for m in models + ["qwen2.5:0.5b"]:
            kinds_for_model = ("verify",) if m == "qwen2.5:0.5b" else None
            subset = [c for c in cases if kinds_for_model is None or c["kind"] in kinds_for_model]
            await unload_all(client)
            need = await model_size_mb(client, m) + 2048
            have = _ram_available_mb()
            if have < need:
                print(f"  ! {m} übersprungen: {have:.0f} MB frei, brauche ~{need:.0f} MB", flush=True)
                continue
            print(f"lokal {m}: {len(subset)} Fälle … ({have:.0f} MB frei, Modell ~{need-2048:.0f} MB)", flush=True)
            # Warmup, damit Ladezeit nicht als Latenz zählt
            try:
                await local_call(client, m, "ok")
            except Exception as e:
                print(f"  ! {m} nicht ladbar: {e}")
                continue
            for c in subset:
                prompt, parse = local_prompt(c["kind"], c["state"])
                try:
                    out, lat = await local_call(client, m, prompt)
                    label = parse(out)
                    emit({**c, "backend": m, "label": label, "correct": label == c["expected"],
                          "raw": out.strip()[:40], "latency": lat})
                except Exception as e:
                    emit({**c, "backend": m, "error": str(e)[:200], "correct": False, "latency": None})
        await unload_all(client)
        print("lokal fertig, alle Modelle entladen.", flush=True)

    write_report(rows)


def write_report(rows: list[dict]) -> None:
    backends = sorted({r["backend"] for r in rows}, key=lambda b: (b != "jev", b))
    kinds = ["intent", "address", "gate", "proactive", "inbox", "task", "conflict", "verify"]
    L = ["# Jev vs. lokal — Mantis-Entscheidungen", "",
         f"{len({r['id'] for r in rows})} handgelabelte Fälle (bench/jev/cases.py). Lokal = Mantis' heutige Prompts + Parser.", "",
         "## Trefferquote je Entscheidungstyp", "",
         "| Typ | n | " + " | ".join(backends) + " |", "|---|---|" + "---|" * len(backends)]
    for k in kinds:
        n = len({r["id"] for r in rows if r["kind"] == k})
        if not n:
            continue
        cells = []
        for b in backends:
            rs = [r for r in rows if r["kind"] == k and r["backend"] == b]
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs)}" if rs else "—")
        L.append(f"| {k} | {n} | " + " | ".join(cells) + " |")
    cells = []
    for b in backends:
        rs = [r for r in rows if r["backend"] == b]
        cells.append(f"**{sum(r['correct'] for r in rs)}/{len(rs)} ({100*sum(r['correct'] for r in rs)/len(rs):.0f}%)**")
    L.append("| **gesamt** | | " + " | ".join(cells) + " |")

    L += ["", "## Latenz (Sekunden, warm)", "", "| Backend | Median | p90 | max |", "|---|---|---|---|"]
    for b in backends:
        lats = [r["latency"] for r in rows if r["backend"] == b and r.get("latency")]
        if lats:
            lats.sort()
            L.append(f"| {b} | {statistics.median(lats):.2f} | {lats[int(0.9*len(lats))-1]:.2f} | {lats[-1]:.2f} |")

    jev = [r for r in rows if r["backend"] == "jev" and "confidence" in r]
    if jev:
        cost = sum(r.get("cost") or 0 for r in jev)
        toks = sum(r.get("tokens") or 0 for r in jev)
        L += ["", f"Jev-Kosten für alle {len(jev)} Calls: ${cost:.5f} ({toks} Input-Tokens).", "",
              "## Kalibrierung (Jev)", "",
              "Confidence = Choice-Confidence bzw. |noul−0.5|·2 bei Ja/Nein. Gut kalibriert heißt: niedrige Buckets sind auch öfter falsch.", "",
              "| Confidence | n | richtig | Quote |", "|---|---|---|---|"]
        for lo, hi in ((0.0, 0.5), (0.5, 0.8), (0.8, 0.95), (0.95, 1.01)):
            rs = [r for r in jev if lo <= r["confidence"] < hi]
            if rs:
                L.append(f"| {lo:.2f}–{min(hi,1):.2f} | {len(rs)} | {sum(r['correct'] for r in rs)} | {100*sum(r['correct'] for r in rs)/len(rs):.0f}% |")
        nouls = [r for r in jev if "noul" in r["raw"]]
        if nouls:
            brier = statistics.mean((r["raw"]["noul"] - (1.0 if r["expected"] else 0.0)) ** 2 for r in nouls)
            L.append(f"\nBrier-Score der {len(nouls)} Ja/Nein-Fragen: {brier:.3f} (0 = perfekt, 0.25 = Münzwurf).")

    L += ["", "## Fehler im Detail", ""]
    for b in backends:
        wrong = [r for r in rows if r["backend"] == b and not r["correct"]]
        L.append(f"### {b} — {len(wrong)} falsch")
        for r in wrong:
            extra = f" conf={r['confidence']:.2f}" if "confidence" in r else ""
            got = r.get("label", r.get("error", "?"))
            L.append(f"- `{r['id']}` [{r['kind']}] erwartet **{r['expected']}**, bekam **{got}**{extra} — "
                     f"{json.dumps(r['state'], ensure_ascii=False)[:90]}" + (f" _{r['note']}_" if r.get("note") else ""))
        L.append("")
    (RESULTS / "report.md").write_text("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    asyncio.run(main())
