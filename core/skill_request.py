"""Deterministischer Fast-Path für die Skill-Factory.

Warum das hier steht statt im Agenten (gemessen am laufenden System, 09.09.2026):
Der Dashboard-Agent (gemma4:e2b) ruft `create_skill` unter dem vollen System-Prompt
mit 89 Tools reproduzierbar (3/3) OHNE das Pflichtargument `skill_name` auf —
`FEHLER: ungültige Argumente`, keine Datei — und meldet danach trotzdem Erfolg.
Dasselbe Modell schafft denselben Tool-Call isoliert 3/3 korrekt. Es ist also
nicht das Modell und nicht das Schema, sondern die Menge an Kontext.

Genau dieselbe Lehre wie in `core/fast_commands.py`: Was zuverlässig sein muss,
darf nicht davon abhängen, dass ein kleines Modell unter Volllast das richtige
Argument mitschickt.

Anders als fast_commands.py kommt dieser Pfad aber nicht ohne LLM aus — Python-
Quellcode lässt sich nicht regelbasiert erzeugen. Die Arbeitsteilung ist deshalb:

  * **deterministisch** (hier, ohne LLM): Absicht erkennen, `skill_name` und die
    Beschreibung aus der Äußerung ziehen. Genau die Argumente, die der Agent
    verschluckt.
  * **ein einziger, enger LLM-Call**: nur der Quellcode, auf BG_CODE_MODEL, mit
    kurzem Prompt und ohne Tools — die Bedingungen, unter denen die Modelle im
    Benchmark sauber liefern. Scheitert der Lint, bekommt das Modell den Fehler
    genau einmal zurück; danach wird abgebrochen statt geraten.

Designprinzip wie in fast_commands.py: **Fehlalarme sind schlimmer als
Auslassungen.** Die Regeln unten sind bewusst eng — wird kein sauberer
snake_case-Name gefunden, greift der Fast-Path NICHT und der Agent übernimmt.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from core.skill_factory import (
    SkillValidationError,
    _ALLOWED_ROOTS,
    create_skill,
    validate_source,
)

log = logging.getLogger(__name__)

# Der Name muss exakt der Regel aus skill_factory.create_skill genügen —
# sonst erkennt der Fast-Path etwas, das die Factory anschließend ablehnt.
_NAME_RE = r"[a-z][a-z0-9_]{2,39}"

_BAU_VERBEN = {"bau", "baue", "erstell", "erstelle", "schreib", "schreibe",
               "leg", "lege", "generier", "generiere", "programmier", "programmiere"}

# "namens X" / "mit dem namen X" / "nenn ihn X" / "skill X" — in dieser Reihenfolge,
# die spezifischeren Muster zuerst.
_NAME_MUSTER = (
    re.compile(rf"\bnamens\s+[`'\"]?({_NAME_RE})[`'\"]?", re.IGNORECASE),
    re.compile(rf"\bmit\s+dem\s+namen\s+[`'\"]?({_NAME_RE})[`'\"]?", re.IGNORECASE),
    re.compile(rf"\bnenn(?:e)?\s+(?:ihn|es|das)\s+[`'\"]?({_NAME_RE})[`'\"]?", re.IGNORECASE),
    re.compile(rf"\bskills?\s+[`'\"]({_NAME_RE})[`'\"]", re.IGNORECASE),
)


@dataclass
class SkillRequest:
    skill_name: str
    spec: str        # die Original-Äußerung; sie ist die Aufgabenstellung
    label: str = "skill-factory"


def match(text: str) -> SkillRequest | None:
    """Erkennt eine Skill-Bau-Anweisung — sonst None (dann übernimmt der Agent).

    Bewusst eng: ohne Bau-Verb, ohne das Wort "skill" oder ohne sauber
    extrahierbaren snake_case-Namen wird nicht gegriffen.
    """
    if not text or "?" in text:
        return None  # Fragen sind keine Befehle (wie in fast_commands.py)

    worte = set(re.findall(r"\w+", text.lower()))
    if not (worte & _BAU_VERBEN):
        return None
    if not ({"skill", "skills"} & worte):
        return None

    for muster in _NAME_MUSTER:
        m = muster.search(text)
        if m:
            name = m.group(1).lower()
            # Ein Treffer wie "skill an" wäre formal snake_case, aber offensichtlich
            # kein Name. Reine Füllwörter deshalb ausschließen.
            if name in {"skill", "skills", "namens", "einen", "eine", "neuen", "neue"}:
                return None
            return SkillRequest(skill_name=name, spec=text.strip())
    return None


def _prompt(req: SkillRequest, fehler: str | None = None) -> str:
    """Baut den Generierungs-Prompt.

    Zwei Regeln stehen hier aus gemessenen Gründen so und nicht anders
    (Livetest 09.09.2026):

    * **Importe werden aktiv abgeraten.** Das Modell griff sonst zur üblichen
      Boilerplate `from __future__ import annotations` — die steht nicht auf
      der Whitelist in skill_factory und ließ den Lint zweimal auflaufen.
    * **Die verbotenen Namen werden NICHT aufgezählt.** Eine frühere Fassung
      listete sie ("eval, exec, __import__, ..."); genau diese Liste tauchte
      dann im erzeugten Code wieder auf und ließ den Lint an `__import__`
      scheitern. Wer nichts importiert und nichts dynamisch ausführt, trifft
      die Sperre ohnehin nie — die Regel steht deshalb positiv formuliert da.
    """
    erlaubt = ", ".join(sorted(_ALLOWED_ROOTS))
    p = (
        "Schreibe den Python-Quellcode für ein Mantis-Skill.\n\n"
        f"AUFGABE: {req.spec}\n\n"
        "HARTE VORGABEN:\n"
        f"1. Genau EINE `async def {req.skill_name}(...)`-Funktion, sonst keine "
        "weiteren Funktionen oder Klassen.\n"
        f"2. Sie ist mit `@T.register(\"{req.skill_name}\", <beschreibung>, "
        "<parameter_schema>, <required_liste>, <kategorie>)` dekoriert.\n"
        "3. Die Funktion gibt einen String zurück.\n"
        "4. SCHREIBE KEINE IMPORT-ZEILE. `T` ist bereits vorhanden, und die "
        "allermeisten Skills brauchen nichts weiter. Insbesondere KEIN "
        "`from __future__ import ...` — das ist hier verboten.\n"
        f"5. Nur falls du wirklich ein Modul brauchst, ist genau das erlaubt: {erlaubt}.\n"
        "6. Kein dynamisches Ausführen oder Nachladen von Code, kein Dateizugriff.\n\n"
        "Antworte NUR mit dem Code, ohne Erklärung."
    )
    if fehler:
        p += (
            "\n\nDein letzter Versuch wurde vom Lint abgelehnt:\n"
            f"{fehler}\n"
            "Gib den korrigierten Code zurück. Der einfachste Weg an dem Fehler "
            "vorbei ist meistens, die Import-Zeile ersatzlos zu streichen."
        )
    return p


_CODEZAUN = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
# `from __future__ import annotations` ist die Boilerplate, zu der die Modelle
# trotz gegenteiliger Ansage am häufigsten greifen (3 von 10 Läufen, 09.09.2026).
# `__future__` steht nicht auf der Import-Whitelist, der Lint lehnt also ab.
# Die Zeile hat für ein Skill keinerlei Wirkung — sie deterministisch zu streichen
# ist billiger und verlässlicher, als dafür einen zweiten LLM-Call zu verbrennen.
_FUTURE_IMPORT = re.compile(r"^\s*from\s+__future__\s+import\s+.*$\n?", re.MULTILINE)


def _quellcode(antwort: str) -> str:
    m = _CODEZAUN.search(antwort or "")
    roh = (m.group(1) if m else (antwort or ""))
    return _FUTURE_IMPORT.sub("", roh).strip()


async def erzeuge(req: SkillRequest, llm) -> str:
    """Erzeugt den Skill und gibt die Nutzer-Antwort zurück.

    `llm` ist das Background-LLM (RoutedLLMProvider). Der Prompt enthält
    `async def` und trifft damit die _CODE_KEYWORDS in llm/routed.py — der Call
    landet also auf BG_CODE_MODEL und erbt dessen Sampling-Defaults.
    """
    fehler: str | None = None
    for versuch in (1, 2):
        antwort = await llm.chat(
            [{"role": "user", "content": _prompt(req, fehler)}],
            max_tokens=1200,
        )
        quelle = _quellcode(antwort)
        try:
            validate_source(quelle, req.skill_name)
        except SkillValidationError as e:
            fehler = str(e)
            log.info("Skill-Fast-Path: Lint-Fehler in Versuch %d — %s", versuch, fehler)
            continue

        ergebnis = create_skill(req.skill_name, req.spec, quelle)
        if ergebnis["ok"]:
            log.info("⚡ Skill-Fast-Path: '%s' als Entwurf gespeichert", req.skill_name)
            return f"✅ {ergebnis['message']}"
        # Die Factory lehnt aus eigenen Gründen ab (Tageslimit, Name belegt) —
        # das ist keine Lint-Frage, ein zweiter Versuch würde daran nichts ändern.
        log.info("Skill-Fast-Path abgelehnt: %s", ergebnis["message"])
        return f"❌ {ergebnis['message']}"

    return (
        f"❌ Konnte für '{req.skill_name}' keinen gültigen Skill-Code erzeugen. "
        f"Letzter Lint-Fehler: {fehler}"
    )
