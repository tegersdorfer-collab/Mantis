"""Tests für den Skill-Factory-Fast-Path (core/skill_request.py).

Hintergrund: Der Dashboard-Agent (gemma4:e2b) rief `create_skill` unter dem
vollen System-Prompt reproduzierbar (3/3, gemessen am laufenden System am
09.09.2026) ohne das Pflichtargument `skill_name` auf und meldete danach
trotzdem Erfolg. Der Fast-Path zieht Name und Beschreibung deterministisch aus
der Äußerung; nur der Quellcode kommt aus einem engen LLM-Call.

Kein Netzwerk, kein Ollama: das LLM ist durchgehend gestubbt.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import skill_request as SR

GUELTIG = '''@T.register("celsius_nach_fahrenheit", "Celsius nach Fahrenheit.",
    {"celsius": {"type": "number", "description": "Grad Celsius"}}, ["celsius"], "utility")
async def celsius_nach_fahrenheit(celsius: float) -> str:
    return f"{celsius * 9 / 5 + 32:.1f} °F"
'''

FALSCHER_NAME = '''@T.register("anderer_name", "Falsch.", {}, [], "utility")
async def anderer_name() -> str:
    return "x"
'''

VERBOTENER_IMPORT = '''import os


@T.register("celsius_nach_fahrenheit", "Verboten.", {}, [], "utility")
async def celsius_nach_fahrenheit() -> str:
    return "x"
'''


class StubLLM:
    """Gibt der Reihe nach die vorgegebenen Antworten zurück."""

    def __init__(self, *antworten):
        self.antworten = list(antworten)
        self.prompts = []

    async def chat(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        return self.antworten.pop(0)


# ── Erkennung: Treffer ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,erwartet", [
    ("Bau mir bitte einen neuen Skill namens celsius_nach_fahrenheit, der umrechnet.",
     "celsius_nach_fahrenheit"),
    ("erstelle einen skill mit dem namen wasser_tracker der Wasser zaehlt", "wasser_tracker"),
    ("Schreib einen Skill, nenn ihn zins_rechner", "zins_rechner"),
    ("bau einen skill 'muskel_volumen' der das Volumen berechnet", "muskel_volumen"),
    ("Erstelle einen Skill namens TAGE_BIS der zaehlt", "tage_bis"),  # Groß→klein
])
def test_erkennt_bau_anweisung(text, erwartet):
    req = SR.match(text)
    assert req is not None and req.skill_name == erwartet


def test_spec_ist_die_original_aeusserung():
    text = "Bau einen Skill namens zins_rechner der Zinseszins ueber Jahre rechnet"
    assert SR.match(text).spec == text


# ── Erkennung: bewusste Auslassungen (Fehlalarm ist schlimmer) ────────────────

@pytest.mark.parametrize("text", [
    "Wie baue ich einen Skill?",                             # Frage
    "Bau mir einen Skill der Celsius umrechnet",             # kein Name
    "erstelle eine Notiz namens einkauf",                    # kein Skill
    "was kann der skill celsius_nach_fahrenheit",            # kein Bau-Verb
    "mach die lampe an",                                     # anderer Fast-Path
    "erstelle einen skill namens X",                         # Name nicht snake_case
    "erstelle einen skill namens ab",                        # zu kurz (<3)
    "erstelle einen skill namens 9_leben",                   # beginnt mit Ziffer
    "",
])
def test_greift_nicht(text):
    assert SR.match(text) is None


def test_fuellwort_wird_nicht_als_name_genommen():
    assert SR.match("bau mir einen skill 'skill' zusammen") is None


# ── Erzeugung ─────────────────────────────────────────────────────────────────

def test_erzeugt_skill_aus_gueltigem_code(tmp_path, monkeypatch):
    aufrufe = {}

    def fake_create(name, beschreibung, quelle):
        aufrufe.update(name=name, beschreibung=beschreibung, quelle=quelle)
        return {"ok": True, "message": f"Skill '{name}' gespeichert."}

    monkeypatch.setattr(SR, "create_skill", fake_create)
    req = SR.match("Bau einen Skill namens celsius_nach_fahrenheit der umrechnet")
    llm = StubLLM(f"```python\n{GUELTIG}```")

    antwort = asyncio.run(SR.erzeuge(req, llm))

    assert antwort.startswith("✅")
    # Der Kern des Bugs: skill_name wird IMMER mitgegeben.
    assert aufrufe["name"] == "celsius_nach_fahrenheit"
    assert "async def celsius_nach_fahrenheit" in aufrufe["quelle"]


def test_code_ohne_codezaun_wird_akzeptiert(monkeypatch):
    monkeypatch.setattr(SR, "create_skill",
                        lambda n, b, q: {"ok": True, "message": "ok"})
    req = SR.match("Bau einen Skill namens celsius_nach_fahrenheit der umrechnet")

    assert asyncio.run(SR.erzeuge(req, StubLLM(GUELTIG))).startswith("✅")


def test_future_import_wird_deterministisch_entfernt():
    """Die häufigste Boilerplate-Falle: `from __future__ import annotations`.

    `__future__` steht nicht auf der Import-Whitelist der Factory. Ohne diese
    Normalisierung brauchten 3 von 10 Generierungen einen zweiten LLM-Call.
    """
    antwort = (
        "```python\n"
        "from __future__ import annotations\n\n"
        + GUELTIG +
        "```"
    )
    quelle = SR._quellcode(antwort)

    assert "__future__" not in quelle
    assert quelle.startswith("@T.register")
    SR.validate_source(quelle, "celsius_nach_fahrenheit")  # wirft nicht


def test_normale_imports_bleiben_erhalten():
    """Gestrichen wird NUR __future__ — erlaubte Importe dürfen nicht wegfallen."""
    antwort = "import math\n\n" + GUELTIG
    assert "import math" in SR._quellcode(antwort)


def test_retry_nach_lint_fehler(monkeypatch):
    """Der Retry-Pfad — im Livetest 8/8 nie ausgelöst, deshalb hier erzwungen."""
    monkeypatch.setattr(SR, "create_skill",
                        lambda n, b, q: {"ok": True, "message": "ok"})
    req = SR.match("Bau einen Skill namens celsius_nach_fahrenheit der umrechnet")
    llm = StubLLM(FALSCHER_NAME, GUELTIG)

    antwort = asyncio.run(SR.erzeuge(req, llm))

    assert antwort.startswith("✅")
    assert len(llm.prompts) == 2
    # Der zweite Prompt muss den Lint-Fehler enthalten, sonst rät das Modell blind.
    assert "abgelehnt" in llm.prompts[1]
    assert "celsius_nach_fahrenheit" in llm.prompts[1]


def test_gibt_auf_statt_zu_raten(monkeypatch):
    """Nach zwei Fehlversuchen wird abgebrochen — kein Endlos-Raten."""
    monkeypatch.setattr(SR, "create_skill",
                        lambda n, b, q: {"ok": True, "message": "darf nicht passieren"})
    req = SR.match("Bau einen Skill namens celsius_nach_fahrenheit der umrechnet")
    llm = StubLLM(VERBOTENER_IMPORT, FALSCHER_NAME)

    antwort = asyncio.run(SR.erzeuge(req, llm))

    assert antwort.startswith("❌")
    assert len(llm.prompts) == 2


def test_factory_ablehnung_wird_nicht_wiederholt(monkeypatch):
    """Tageslimit/Name belegt sind keine Lint-Fragen — kein zweiter LLM-Call."""
    monkeypatch.setattr(SR, "create_skill",
                        lambda n, b, q: {"ok": False, "message": "Tages-Limit erreicht."})
    req = SR.match("Bau einen Skill namens celsius_nach_fahrenheit der umrechnet")
    llm = StubLLM(f"```python\n{GUELTIG}```", f"```python\n{GUELTIG}```")

    antwort = asyncio.run(SR.erzeuge(req, llm))

    assert antwort.startswith("❌") and "Tages-Limit" in antwort
    assert len(llm.prompts) == 1


# ── Prompt-Inhalt ─────────────────────────────────────────────────────────────

def test_prompt_trifft_die_code_route():
    """llm/routed.py routet über _CODE_KEYWORDS — 'async def' muss drin stehen,
    sonst landet die Skill-Erzeugung nicht auf BG_CODE_MODEL."""
    from llm.routed import _detect_route

    req = SR.match("Bau einen Skill namens celsius_nach_fahrenheit der umrechnet")
    assert _detect_route(SR._prompt(req)) == "code"


def test_prompt_nennt_den_geforderten_funktionsnamen():
    req = SR.match("Bau einen Skill namens zins_rechner der rechnet")
    p = SR._prompt(req)
    assert "async def zins_rechner" in p
    assert "`T` ist bereits vorhanden" in p       # sonst importiert das Modell T
    assert "__future__" in p                      # die Boilerplate-Falle wird benannt


def test_prompt_zaehlt_verbotene_namen_nicht_auf():
    """Die Aufzählung war kontraproduktiv: '__import__' landete daraufhin im Code."""
    req = SR.match("Bau einen Skill namens zins_rechner der rechnet")
    p = SR._prompt(req)
    for verboten in ("eval", "exec", "__import__", "breakpoint"):
        assert verboten not in p
