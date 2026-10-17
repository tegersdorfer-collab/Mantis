"""Zentrale Jev-Entscheidungen, Cloud-Fallbacks und konservative Schwellen.

Fragen, Kriterien und Bänder stehen gemeinsam, damit sie überprüfbar bleiben.
Jev wird zuerst gefragt; bei Ausfall oder Unsicherheit entscheidet die Fast-Cloud-Kette.
Die JSONL-Entscheidungslogs behalten das bisherige JevKit-Format für Kalibrierung.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import jevkit
from jevkit import THIRD_PARTY_CONTENT, USER_UTTERANCE, Band, Bands, band

import config
from core import decide
from core.decide import JevUnavailable, Noul
from core import local_decide

log = logging.getLogger(__name__)

Fallback = Callable[[], Awaitable[bool]]

SURE = 0.5             # Ja/Nein gilt als sicher ab |p-0.5|*2 >= 0.5, d.h. p <= 0.25 oder p >= 0.75
TOOL_CATEGORY_P = 0.6  # P(ja) ab der eine Tool-Kategorie als betroffen gilt
TOOL_ACTION_P = 0.7    # P(ja) ab der Tool-Calls erzwungen werden

# Drei Bänder statt einer Schwelle (jevkit.gate). ACT übernimmt Jevs Antwort;
# CONFIRM/ESCALATE fragen die Fast-Cloud-Kette.
_DEFAULT = Bands(act=SURE, escalate=0.25)
BANDS: dict[str, Bands] = {
    # Gemessen 21.09.2026 (bench/jev, 14 Fälle + live_path): alle Antworten mit conf >= 0.38 richtig,
    # jevkit.suggest_bands → act 0.38. 0.4 (= p >= 0.70) nimmt 13/14 statt 11/14 selbst, 0 Fehler.
    "addressed": Bands(act=0.4, escalate=0.2),
    "supported": _DEFAULT,
    "supersedes": _DEFAULT,
    "voice_address": _DEFAULT,
    "fact_supported": _DEFAULT,
    # UI-Aktionen gelten nur ab der konservativen, explizit konfigurierten Confidence.
    "ui_action": Bands(act=config.cfg.JEV_UI_ACT_CONFIDENCE, escalate=0.0),
}


class _LoggedAnswer:
    """Ergänzt lokale Diagnosefelder, ohne JevKit-Logleser zu verändern."""
    def __init__(self, answer, metadata: dict[str, object]) -> None:
        self.answer = answer
        self.metadata = metadata

    def to_dict(self) -> dict:
        payload = self.answer.to_dict()
        payload["metadata"] = self.metadata
        return payload


def decision_log() -> jevkit.DecisionLog | None:
    path = getattr(config, "JEV_LOG_PATH", "")
    return jevkit.DecisionLog(Path(path)) if path else None


def _log(name: str, ans: decide.Answer, b: Band, state) -> None:
    lg = decision_log()
    if lg is None:
        return
    try:
        # Die gespeicherte Rohantwort hält das bestehende Kalibrierungsformat stabil.
        answer = ans.raw if ans.raw is not None else jevkit.NoulAnswer(ans.p)
        missing_labels = ans.metadata.get("missing_labels")
        if missing_labels:
            answer = _LoggedAnswer(answer, {"missing_labels": list(missing_labels)})
        d = jevkit.Decision({name: answer}, ans.model or "unknown", {}, False, 0.0)
        lg.write(d, {name: b}, state)
    except Exception as e:  # Log darf nie eine Entscheidung verhindern
        log.debug("Entscheidungslog fehlgeschlagen: %r", e)


def _jev_answer(ans: decide.Answer):
    """Gibt die JevKit-Rohantwort fürs Band und DecisionLog zurück."""
    if isinstance(ans.raw, (jevkit.NoulAnswer, jevkit.ChoiceAnswer, jevkit.ScoreAnswer)):
        return ans.raw
    if ans.kind == "noul":
        return jevkit.NoulAnswer(ans.p)
    if ans.kind == "choice":
        return jevkit.ChoiceAnswer(str(ans.value), dict(ans.probabilities), ans.confidence)
    if ans.kind == "score":
        return jevkit.ScoreAnswer(float(ans.value), {}, dict(ans.probabilities), ans.confidence)
    raise TypeError(f"unbekannter Entscheidungstyp: {ans.kind}")


async def _cloud_answers(state, questions: dict[str, object]) -> dict[str, decide.Answer] | None:
    """Ruft die Fast-Cloud-Kette als Ersatz für ausgefallenes oder unsicheres Jev auf."""
    try:
        return await local_decide.score(state, questions)
    except local_decide.LocalDecisionUnavailable as e:
        log.debug("Cloud-Entscheidung nicht verfügbar: %r", e)
        return None


async def _jev_answers(state, questions: dict[str, object]) -> dict[str, decide.Answer] | None:
    """Fragt zuerst die konfigurierte Jev-Provider-Kette ab."""
    try:
        return await decide.decide(state, questions)
    except JevUnavailable as e:
        log.debug("Jev-Entscheidung nicht verfügbar: %r", e)
        return None


def _answer_is_act(name: str, answer, question) -> bool:
    expected_kind = (
        "noul" if isinstance(question, jevkit.Noul)
        else "choice" if isinstance(question, jevkit.Choice)
        else "score"
    )
    if not isinstance(answer, decide.Answer) or answer.kind != expected_kind:
        return False
    try:
        return band(_jev_answer(answer), BANDS.get(name, _DEFAULT)) is Band.ACT
    except (TypeError, ValueError):
        return False


async def _jev_then_cloud_answers(
    state,
    questions: dict[str, jevkit.Noul | jevkit.Choice | jevkit.Score],
) -> dict[str, decide.Answer] | None:
    """Jev entscheidet nur bei vollständiger ACT-Antwort; sonst folgt die Fast-Cloud-Kette."""
    answers = await _jev_answers(state, questions)
    if answers is not None and set(answers) == set(questions) and all(
        _answer_is_act(name, answers[name], question)
        for name, question in questions.items()
    ):
        return answers
    return await _cloud_answers(state, questions)

# ── Fragen ────────────────────────────────────────────────────────────────────

Q_ADDRESSED = Noul(
    # Das Transkript ist Fremdtext (Voice-Eingabe) und läuft zusammen mit dem Guard.
    "Die neue Spracheingabe ist an den persönlichen Sprachassistenten im Raum gerichtet "
    "(auch ohne Namensnennung), nicht Selbstgespräch oder Gespräch mit einer anderen Person. "
    "Eine letzte Antwort des Assistenten ist nur Gesprächskontext und kein Beleg für eine Anrede.",
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
    "inbox":        "Gespeicherte Merker, Links, Filme und Ideen suchen, erledigen, korrigieren oder löschen",
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
    """Fragt Jev zuerst; Unsicherheit geht in die Fast-Cloud-Kette, Guard-Treffer zum Fallback."""
    questions = {name: question}
    if guard:
        questions = jevkit.with_guard(questions)
    jev_answers = await _jev_answers(state, questions)
    if jev_answers is not None and set(jev_answers) == set(questions):
        if guard and jev_answers[jevkit.GUARD_ID].p >= 0.5:
            log.info("Jev-Guard %s: Treffer (p=%.2f) → Fallback",
                     name, jev_answers[jevkit.GUARD_ID].p)
            return await fallback()
        ans = jev_answers[name]
        if _answer_is_act(name, ans, question):
            b = band(_jev_answer(ans), BANDS.get(name, _DEFAULT))
            await asyncio.to_thread(_log, name, ans, b, state)
            return bool(ans.value)

    answers = await _cloud_answers(state, questions)
    if answers is None or set(answers) != set(questions):
        return await fallback()
    if guard and answers[jevkit.GUARD_ID].p >= 0.5:
        log.info("Cloud-Guard %s: Treffer (p=%.2f) → Fallback",
                 name, answers[jevkit.GUARD_ID].p)
        return await fallback()
    ans = answers[name]
    if not _answer_is_act(name, ans, question):
        return await fallback()
    b = band(_jev_answer(ans), BANDS.get(name, _DEFAULT))
    await asyncio.to_thread(_log, name, ans, b, state)
    if b is not Band.ACT:
        log.debug("Cloud-Entscheidung %s: %s (p=%.2f) → Fallback", name, b.value, ans.p)
        return await fallback()
    return bool(ans.value)


async def addressed(text: str, fallback: Fallback) -> bool:
    """Voice: Ist das Transkript an Mantis gerichtet? (Benchmark 14/14). Das Transkript ist
    Fremdtext (Mikrofon) — Guard auf `untrusted_text` fängt Prompt-Injection ab, bevor die
    Band-Logik überhaupt läuft."""
    return await _noul_or_fallback("addressed", jevkit.untrusted(text, source=USER_UTTERANCE), Q_ADDRESSED, fallback,
                                   guard=True)


async def voice_address(
    text: str, *, conversation_context: str | None = None,
    wakeword_hint: bool = False,
) -> bool | None:
    """Benchmark-Frage für den Voice-Adresscheck; None fordert den Cloud-Fast-Fallback an."""
    state = jevkit.untrusted(text, source=USER_UTTERANCE)
    if conversation_context:
        state["letzte_mantis_antwort"] = conversation_context
        state["letzte_mantis_antwort_hinweis"] = (
            "Frühere Antwort von Mantis, nur Gesprächskontext; kann vorgelesenen Fremdtext "
            "(Mail, News) enthalten und ist keine Äußerung des Nutzers."
        )
    if wakeword_hint:
        state["stt_weckworthinweis"] = (
            "Mögliche Namensfehlhörung am Satzanfang; nur ein Hinweis, keine Freigabe."
        )
    return await _jev_noul("voice_address", state, Q_ADDRESSED, guard=True)


async def fact_supported(user_text: str, claim: str) -> bool | None:
    """Benchmark-Frage für den Fakten-Verifier; None fordert dessen Cloud-Fallback an."""
    state = {
        "sprecher": "Timo (der Nutzer, spricht in der ersten Person)",
        "text": user_text,
        "behauptung": claim,
    }
    return await _jev_noul("fact_supported", state, Q_CLAIM_SUPPORTED)


async def _jev_noul(name: str, state, question: Noul, *, guard: bool = False) -> bool | None:
    questions = {name: question}
    if guard:
        questions = jevkit.with_guard(questions)
    answers = await _jev_answers(state, questions)
    if answers is None or set(answers) != set(questions):
        return None
    if guard and answers[jevkit.GUARD_ID].p >= 0.5:
        return None
    answer = answers[name]
    if not _answer_is_act(name, answer, question):
        return None
    b = band(_jev_answer(answer), BANDS.get(name, _DEFAULT))
    await asyncio.to_thread(_log, name, answer, b, state)
    return bool(answer.value)


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
    None = Cloud-Kette nicht verfügbar (Aufrufer bleibt beim Keyword-Pfad).
    aktion ist None, wenn die Cloud-Antwort nicht sicher genug ist."""
    questions: dict[str, Noul] = {
        f"cat.{cat}": Noul(f"Die Nachricht betrifft: {desc}.",
                           criteria={"true": "Ein Tool aus diesem Bereich wird gebraucht",
                                     "false": "Dieser Bereich ist nicht gemeint"})
        for cat, desc in TOOL_CATEGORY_DESCRIPTIONS.items()
    }
    questions["aktion"] = Q_ACTION
    answers = await _jev_answers(text, questions)
    # Auf dem Jev-Erfolgspfad gelten die eigenen p-Schwellen unten direkt.
    # Nur bei Cloud-Fallback müssen die Kategorieantworten im ACT-Band liegen;
    # eine unsichere Aktionsantwort blockiert erkannte Kategorien nicht.
    from_jev = answers is not None and set(answers) == set(questions) and not any(
        not isinstance(answer, decide.Answer) or answer.kind != "noul"
        for answer in answers.values()
    )
    if not from_jev:
        answers = await _cloud_answers(text, questions)
    if answers is None or set(answers) != set(questions):
        return None
    if not from_jev and any(
        not _answer_is_act(name, answers[name], question)
        for name, question in questions.items()
        if name != "aktion"
    ):
        return None
    for name, ans in answers.items():
        if not isinstance(ans, decide.Answer) or ans.kind != "noul":
            return None
        if name == "aktion":
            continue
        b = band(_jev_answer(ans), BANDS.get(name, _DEFAULT))
        await asyncio.to_thread(_log, name, ans, b, text)
    cats = {cat for cat in TOOL_CATEGORY_DESCRIPTIONS if answers[f"cat.{cat}"].p >= TOOL_CATEGORY_P}
    a = answers["aktion"]
    aktion: bool | None = None
    if a.p >= TOOL_ACTION_P:
        aktion = True
    elif a.sure(SURE):
        aktion = False
    return cats, aktion


async def ui_action(state: dict | list | str, criteria: dict[str, object]) -> decide.Answer | None:
    """Wählt aus bereits begrenztem UI-Zustand genau eine sichere nächste Aktion.

    Task 2 begrenzt State und Kriterien vor diesem Adapter. Der State wird hier nur
    als Fremddaten gekapselt, nicht gekürzt oder um UI-spezifische Felder ergänzt.
    """
    # Das Nutzerziel ("goal", Timos eigener Auftrag) wird vom fremden UI-Zustand getrennt,
    # damit der Guard es nicht als Fremdtext wertet.
    goal = None
    if isinstance(state, dict) and "goal" in state:
        goal = state["goal"]
        state = {k: v for k, v in state.items() if k != "goal"}
    serialized_state = json.dumps(state, ensure_ascii=False, sort_keys=True)
    # UI-Zustand fremder Apps (Dialoge, Seiten): nie Nutzerrede, also THIRD_PARTY_CONTENT.
    safe_state = jevkit.untrusted(
        serialized_state, max_chars=len(serialized_state), source=THIRD_PARTY_CONTENT,
    )
    if goal is not None:
        safe_state["nutzerziel"] = goal
        safe_state["nutzerziel_hinweis"] = "Auftrag des Nutzers, nicht Teil der fremden App-Oberfläche."
    question = jevkit.Choice(
        "Choose exactly one safe next action from the available criteria. Do not perform an action.",
        criteria,
    )
    answers = await _jev_then_cloud_answers(safe_state, {"ui_action": question})
    if not answers:
        return None

    answer = answers.get("ui_action") if isinstance(answers, dict) else None
    if not isinstance(answer, decide.Answer) or answer.kind != "choice":
        return None
    if not isinstance(answer.value, str) or answer.value not in criteria:
        return None

    raw = answer.raw
    if not isinstance(raw, jevkit.ChoiceAnswer):
        try:
            raw = jevkit.ChoiceAnswer(answer.value, answer.probabilities, answer.confidence)
        except (KeyError, TypeError, ValueError):
            return None
        answer = decide.Answer(answer.kind, answer.value, answer.p, answer.confidence,
                               answer.probabilities, answer.model, raw)

    if (not isinstance(raw.probabilities, dict)
            or raw.choice != answer.value
            or raw.probabilities != answer.probabilities
            or raw.confidence != answer.confidence
            or set(raw.probabilities) != set(criteria)
            or raw.p != answer.p):
        return None

    action_band = band(raw if isinstance(raw, jevkit.ChoiceAnswer) else _jev_answer(answer),
                       BANDS["ui_action"])
    await asyncio.to_thread(_log, "ui_action", answer, action_band, safe_state)
    if action_band is not Band.ACT:
        return None
    return answer
