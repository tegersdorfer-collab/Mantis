"""
Alle Jev-Fragen und -Schwellen von Mantis an EINER Stelle.

Regel aus der TypeSafe-Doku, die sich im Benchmark bestätigt hat: Fragen und
Thresholds gehören zusammen in eine Datei, damit ein Mensch sie ohne Spelunking
reviewen kann. Die Fragetexte hier sind die aus bench/jev/run.py — die sind
gemessen (bench/jev/results/report.md). Wer sie ändert, misst nach.

Jeder Wrapper: Jev fragen → wenn nicht verfügbar ODER unter der Schwelle →
den übergebenen lokalen Fallback ausführen (das heutige Verhalten). Dadurch
kann kein Aufrufer durch Jev schlechter werden als vorher.

Bänder: `BANDS` je Entscheidung (jevkit). ACT = Jev gilt, sonst lokal.
Log: `JEV_LOG_PATH` (JSONL, nur Hash des States, nie der Text) — Grundlage
für `jevkit.calibrate.suggest_bands`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import jevkit
from jevkit import Band, Bands, band

import config
from core import decide
from core.decide import JevUnavailable, Noul

log = logging.getLogger(__name__)

Fallback = Callable[[], Awaitable[bool]]

SURE = 0.5             # Ja/Nein gilt als sicher ab |p-0.5|*2 >= 0.5, d.h. p <= 0.25 oder p >= 0.75
TOOL_CATEGORY_P = 0.6  # P(ja) ab der eine Tool-Kategorie als betroffen gilt
TOOL_ACTION_P = 0.7    # P(ja) ab der Tool-Calls erzwungen werden

# Drei Bänder statt einer Schwelle (jevkit.gate). ACT = Jev-Antwort gilt; CONFIRM/ESCALATE = lokaler
# Fallback. act=0.5 auf der Confidence entspricht dem bisherigen SURE (p <= 0.25 oder p >= 0.75).
_DEFAULT = Bands(act=SURE, escalate=0.25)
BANDS: dict[str, Bands] = {
    # Gemessen 21.09.2026 (bench/jev, 14 Fälle + live_path): alle Antworten mit conf >= 0.38 richtig,
    # jevkit.suggest_bands → act 0.38. 0.4 (= p >= 0.70) nimmt 13/14 statt 11/14 selbst, 0 Fehler.
    "addressed": Bands(act=0.4, escalate=0.2),
    "supported": _DEFAULT,
    "supersedes": _DEFAULT,
}


def decision_log() -> jevkit.DecisionLog | None:
    path = getattr(config, "JEV_LOG_PATH", "")
    return jevkit.DecisionLog(Path(path)) if path else None


def _log(name: str, ans: decide.Answer, b: Band, state) -> None:
    lg = decision_log()
    if lg is None:
        return
    try:
        # ans.raw ist die rohe jevkit-Answer (aus decide.decide()); nur wenn sie fehlt
        # (z.B. in Tests, die Answer von Hand bauen) auf Noul(p) zurückfallen.
        answer = ans.raw if ans.raw is not None else jevkit.NoulAnswer(ans.p)
        d = jevkit.Decision({name: answer}, ans.model or "unknown", {}, False, 0.0)
        lg.write(d, {name: b}, state)
    except Exception as e:  # Log darf nie eine Entscheidung verhindern
        log.debug("Jev-Log fehlgeschlagen: %r", e)

# ── Fragen ────────────────────────────────────────────────────────────────────

Q_ADDRESSED = Noul(
    # State-Feld heißt `untrusted_text` (jevkit.untrusted) — das Transkript ist Fremdtext
    # (Voice-Eingabe) und läuft deshalb zusammen mit dem Guard (siehe `addressed()`).
    "Das Transkript ist eine Anfrage oder ein Befehl an den persönlichen Sprachassistenten im Raum "
    "(auch ohne Namensnennung), nicht Selbstgespräch oder Gespräch mit einer anderen Person.",
    criteria={"true": "Der Sprecher will, dass der Assistent reagiert",
              "false": "Beiläufiges Gerede, Selbstgespräch, Gespräch mit jemand anderem"},
)

Q_CLAIM_SUPPORTED = Noul(
    "Die Behauptung wird im Text wörtlich oder eindeutig direkt gestützt — keine Vermutung, "
    "keine Verallgemeinerung, keine Verwechslung der Person.",
    criteria={"true": "Steht so im Text", "false": "Nicht belegt, überinterpretiert oder falsche Person"},
)

Q_SUPERSEDES = Noul(
    "Die neue Aussage macht die alte veraltet oder widerspricht ihr direkt "
    "(Umzug, Wechsel, geänderte Präferenz oder Status). Zwei Dinge, die gleichzeitig wahr sein können, "
    "sind KEIN Widerspruch.",
    criteria={"true": "Alte Aussage ist jetzt überholt", "false": "Beides kann zugleich gelten"},
)

Q_ACTION = Noul(
    "Die Nachricht verlangt eine AKTION des Assistenten, für die ein Tool nötig ist "
    "(etwas anlegen, ändern, abhaken, suchen oder gespeicherte Daten abrufen).",
    criteria={"true": "Tool nötig: anlegen, ändern, abhaken, Websuche, Daten des Nutzers abrufen",
              "false": "Reines Gespräch, Meinung, Erklärung aus Allgemeinwissen, Dank"},
)

# Tool-Kategorien (core/tools.py REGISTRY → Tool.category). Ein Noul pro Kategorie,
# alle in EINEM Call (Speculative Fan-out) — Jev braucht für 20 Fragen kaum länger als für eine.
TOOL_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "fitness":      "Training, Workouts, Gewichte, Körpermaße protokollieren oder abfragen",
    "nutrition":    "Essen, Mahlzeiten, Kalorien, Ernährung protokollieren oder abfragen",
    "productivity": "Aufgaben, Termine, Erinnerungen, Kalender anlegen, ändern oder abfragen",
    "health":       "Schlaf, Puls, HRV, Erholung, Gesundheitsdaten abfragen",
    "knowledge":    "Wissensfragen, Websuche, Wetter, Notizen im Wissenssystem (Brain) speichern oder suchen",
    "habits":       "Gewohnheiten abhaken, anlegen oder Streaks abfragen",
    "goals":        "Langfristige Ziele anlegen, ändern oder Fortschritt abfragen",
    "robot":        "Den Roboter fahren, drehen, stoppen, Sensoren lesen",
    "flipper":      "Infrarot-Geräte steuern: Schreibtischlampe, Ventilator",
    "email":        "E-Mails lesen, suchen, archivieren oder einen Entwurf schreiben",
    "filesystem":   "Dateien oder Ordner auf dem Mac lesen, schreiben, öffnen; Apps öffnen",
    "geo":          "Wo liegt ein Ort, Koordinaten, Nachrichten-Briefing auf der Weltkarte",
    "gev":          "Den 3D-Globus (God's Eye View) steuern: hinfliegen, Layer, Stil",
    "journal":      "Einen Tagebuch-Eintrag schreiben",
    "memory":       "Etwas dauerhaft merken, an Gemerktes erinnern, eine Regel für den Assistenten setzen",
    "spotify":      "Musik abspielen, pausieren, weiter, was läuft gerade",
    "system":       "Den eigenen Code des Assistenten lesen oder ändern, einen neuen Skill erstellen oder löschen",
    "ui":           "Das Dashboard umbauen: Widgets zeigen, anordnen, schließen",
    "uiauto":       "Eine Mac-App per Fernsteuerung bedienen (klicken, tippen)",
    "utility":      "Etwas ausrechnen",
    "vision":       "Den Bildschirm anschauen und beschreiben",
    "skilltree":    "Den Skilltree / Fortschrittsbaum anzeigen",
}
# Kategorien, die nicht über die Nutzer-Nachricht geroutet werden (interne oder immer verfügbare Tools).
TOOL_CATEGORIES_OHNE_ROUTING: set[str] = {"general", "uiauto_internal"}


# ── Wrapper ───────────────────────────────────────────────────────────────────

async def _noul_or_fallback(name: str, state, question: Noul, fallback: Fallback, *,
                            guard: bool = False) -> bool:
    """`guard=True` legt jevkit.GUARD in denselben Fan-out (State ist dann Fremdtext, z.B.
    ein Voice-Transkript): schlägt der Guard an, gilt der State als potenzielle
    Prompt-Injection und es geht — ohne Band-Logik — direkt in den lokalen Fallback."""
    questions = {name: question}
    if guard:
        questions = jevkit.with_guard(questions)
    try:
        answers = await decide.decide(state, questions)
    except JevUnavailable:
        return await fallback()
    if guard and answers[jevkit.GUARD_ID].p >= 0.5:
        log.info("Jev %s: Guard hat angeschlagen (p=%.2f) → lokal", name, answers[jevkit.GUARD_ID].p)
        return await fallback()
    ans = answers[name]
    b = band(jevkit.NoulAnswer(ans.p), BANDS.get(name, _DEFAULT))
    await asyncio.to_thread(_log, name, ans, b, state)
    if b is not Band.ACT:
        log.debug("Jev %s: %s (p=%.2f) → lokal", name, b.value, ans.p)
        return await fallback()
    return bool(ans.value)


async def addressed(text: str, fallback: Fallback) -> bool:
    """Voice: Ist das Transkript an Mantis gerichtet? (Benchmark 14/14). Das Transkript ist
    Fremdtext (Mikrofon) — Guard auf `untrusted_text` fängt Prompt-Injection ab, bevor die
    Band-Logik überhaupt läuft."""
    return await _noul_or_fallback("addressed", jevkit.untrusted(text), Q_ADDRESSED, fallback,
                                   guard=True)


async def claim_supported(user_text: str, claim: str, fallback: Fallback) -> bool:
    """Memory-Verifier: Steht die extrahierte Behauptung wirklich im Text? Der Sprecher MUSS
    genannt werden — der Text sagt „ich", die Behauptung sagt „Timo" (ohne: 4/8, mit: 8/8)."""
    state = {"sprecher": "Timo (der Nutzer, spricht in der ersten Person)",
             "text": user_text[:2000], "behauptung": claim}
    return await _noul_or_fallback("supported", state, Q_CLAIM_SUPPORTED, fallback)


async def supersedes(old: str, new: str, fallback: Fallback) -> bool:
    """Memory-Konflikt: Überholt der neue Fakt den alten?"""
    return await _noul_or_fallback("supersedes", {"alte_aussage": old, "neue_aussage": new}, Q_SUPERSEDES, fallback)


async def tool_categories(text: str) -> tuple[set[str], bool | None] | None:
    """Tool-Auswahl: welche Kategorien betrifft die Nachricht, und ist es eine Aktion?
    None = Jev nicht verfügbar (Aufrufer bleibt beim Keyword-Pfad).
    aktion ist None, wenn Jev sich da nicht sicher ist."""
    questions: dict[str, Noul] = {
        f"cat:{cat}": Noul(f"Die Nachricht betrifft: {desc}.",
                           criteria={"true": "Ein Tool aus diesem Bereich wird gebraucht",
                                     "false": "Dieser Bereich ist nicht gemeint"})
        for cat, desc in TOOL_CATEGORY_DESCRIPTIONS.items()
    }
    questions["aktion"] = Q_ACTION
    try:
        answers = await decide.decide(text, questions)
    except JevUnavailable:
        return None
    cats = {cat for cat in TOOL_CATEGORY_DESCRIPTIONS if answers[f"cat:{cat}"].p >= TOOL_CATEGORY_P}
    a = answers["aktion"]
    aktion: bool | None = None
    if a.p >= TOOL_ACTION_P:
        aktion = True
    elif a.sure(SURE):
        aktion = False
    return cats, aktion
