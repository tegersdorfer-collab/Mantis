"""UI-Automatik-Skill — Mantis bedient macOS-Apps über die Accessibility-API.

Zwei Ebenen:
- Low-Level-Tools ui_inspect/ui_click/ui_type/ui_key (Kategorie 'uiauto_internal')
  — NICHT für den Hauptagenten, sondern für den internen qwen-Loop.
- computer_task(goal, app) — das einzige Tool für den (kleinen) Hauptagenten.
  Es prüft das Bedienungshilfen-Recht und startet dann einen isolierten ReAct-
  Loop auf dem STÄRKEREN Modell (qwen3.5:9b), der die Low-Level-Tools nutzt.

Sicherheit: ui_click prüft VOR dem Klick die roten Linien (tools/uiauto/safety.py)
— destruktive/riskante Elemente und Passwortfelder werden verweigert, egal was
das Modell will.
"""

import asyncio
import json
import logging

from core import tools as T
from core import uiauto_controller as controller
from tools.uiauto import engine, safety

log = logging.getLogger("core.skills")

UI_TOOLS = ["ui_inspect", "ui_click", "ui_type", "ui_key"]
UI_MAX_STEPS = 12

_SETUP_MSG = (
    "🔒 Mantis darf noch keine Apps steuern. Bitte den Python-Interpreter "
    "(/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14) unter "
    "Systemeinstellungen → Datenschutz & Sicherheit → Bedienungshilfen aktivieren "
    "und Mantis neu starten. Danach kann ich Fenster inspizieren und bedienen."
)

_UI_SYSTEM = """Du steuerst eine macOS-App über Accessibility-Tools. Arbeite präzise und knapp.

VORGEHEN:
1. ui_inspect(app) → du bekommst eine nummerierte Liste bedienbarer Elemente (ref, Rolle, Titel).
2. Wähle das passende Element und handle: ui_click(ref) / ui_type(text) / ui_key("cmd+k").
3. Nach jeder Aktion erneut ui_inspect, um den neuen Zustand zu sehen.
4. Wiederhole, bis das Ziel erreicht ist. Dann antworte mit einem kurzen Ergebnis-Satz.

REGELN:
- Rate NIE Referenzen — immer erst frisch inspizieren.
- Manche Klicks werden aus Sicherheitsgründen verweigert (Löschen/Senden/Kaufen/Passwortfelder).
  Das ist gewollt; versuche keinen Umweg, sondern melde es.
- Kein Smalltalk. Handle."""

_WRITER_SYSTEM = """Du lieferst ausschließlich den Text für das aktuell fokussierte UI-Eingabefeld.
Antworte nur mit dem Feldtext, ohne Erklärung, Anführungszeichen, Markdown oder Tool-Aufrufe."""
_WRITER_MAX_TOKENS = 160


# ── Low-Level-Tools (für den internen qwen-Loop) ──────────────────────────────

@T.register(
    "ui_inspect",
    "Listet die bedienbaren Elemente der Vordergrund- oder benannten App (ref, Rolle, Titel).",
    {"app": {"type": "string", "description": "optionaler App-Name (sonst Vordergrund-App)"}},
    [],
    "uiauto_internal",
)
async def _ui_inspect(app: str = ""):
    try:
        els = engine.snapshot(app or None)
    except engine.UIAutoError as e:
        return f"❌ {e}"
    if not els:
        return "ℹ️ Keine bedienbaren Elemente gefunden (evtl. Electron-App ohne Accessibility)."
    lines = [f"ref {e['ref']} [{e['role']}] {e['title']}"
             + (f" = {e['value']}" if e['value'] else "")
             + ("" if e['enabled'] else " (deaktiviert)")
             for e in els]
    return "\n".join(lines)


@T.register(
    "ui_click",
    "Klickt/drückt das Element mit der angegebenen ref aus dem letzten ui_inspect.",
    {"ref": {"type": "integer", "description": "Element-Referenz aus ui_inspect"}},
    ["ref"],
    "uiauto_internal",
)
async def _ui_click(ref: int):
    el = engine.element(int(ref))
    if el is None:
        return f"❌ Ungültige Referenz {ref}. Erst ui_inspect aufrufen."
    red, reason = safety.is_redline(el)
    if red:
        return (f"⛔ Rote Linie ({reason}) — Element ‚{el['title'] or el['role']}' NICHT geklickt. "
                f"Solche Aktionen führe ich nicht ohne ausdrückliche Freigabe aus.")
    try:
        engine.act(int(ref))
    except engine.UIAutoError as e:
        return f"❌ {e}"
    return f"✓ geklickt: {el['title'] or el['role']}"


@T.register(
    "ui_type",
    "Tippt Text in das aktuell fokussierte Feld.",
    {"text": {"type": "string", "description": "einzugebender Text"}},
    ["text"],
    "uiauto_internal",
)
async def _ui_type(text: str):
    try:
        engine.type_text(text)
    except engine.UIAutoError as e:
        return f"❌ {e}"
    return f"✓ getippt: {text}"


@T.register(
    "ui_key",
    "Drückt eine Taste oder Tastenkombination, z.B. 'return' oder 'cmd+k'.",
    {"keys": {"type": "string", "description": "Taste/Kombi, z.B. 'cmd+k', 'return', 'escape'"}},
    ["keys"],
    "uiauto_internal",
)
async def _ui_key(keys: str):
    try:
        engine.press_key(keys)
    except engine.UIAutoError as e:
        return f"❌ {e}"
    return f"✓ Taste: {keys}"


# ── Haupt-Tool (für den gemma-Hauptagenten) ───────────────────────────────────

async def _write_ui_text(goal: str, state: dict[str, object]) -> str:
    """Erzeugt einmalig Feldtext ohne Zugriff auf UI- oder andere Tools."""
    from core.agent import Agent
    from core.backends.ollama import OllamaBackend
    from settings import cfg

    backend = OllamaBackend(model=cfg.BG_REASONING_MODEL)
    agent = Agent(backend=backend, max_steps=1)
    context = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    response, _trace = await agent.run(
        messages=[{
            "role": "user",
            "content": f"Ziel: {goal}\nUI-Kontext: {context}",
        }],
        system=_WRITER_SYSTEM,
        use_tools=False,
        allowed_tools=[],
        force_tools=False,
        temperature=0.2,
        max_tokens=_WRITER_MAX_TOKENS,
    )
    return response.strip()


def _default_text_writer(goal: str, state: dict[str, object]) -> str:
    """Synchroner Writer-Adapter für den im Worker laufenden Controller."""
    return asyncio.run(_write_ui_text(goal, state))

async def _run_ui_agent(goal: str, app: str) -> str:
    """Versucht Jev zuerst; startet bei Fallback den bisherigen qwen-ReAct-Loop."""
    result = await asyncio.to_thread(
        controller.run,
        goal,
        app,
        UI_MAX_STEPS,
        _default_text_writer,
    )
    if result.status in {"completed", "aborted"}:
        return result.text
    log.warning("Jev UI controller fallback: %s", result.reason or "unknown reason")

    from core.agent import Agent
    from core.backends.ollama import OllamaBackend
    from settings import cfg

    backend = OllamaBackend(model=cfg.BG_REASONING_MODEL)
    agent = Agent(backend=backend, max_steps=UI_MAX_STEPS)
    app_hint = f" Ziel-App: {app}." if app else ""
    try:
        resp, _trace = await agent.run(
            messages=[{"role": "user", "content": f"{goal}{app_hint}"}],
            system=_UI_SYSTEM,
            allowed_tools=UI_TOOLS,
            force_tools=True,
            temperature=0.3,
            max_tokens=1200,
        )
        return resp or "Fertig."
    except Exception as e:
        log.warning("computer_task-Loop fehlgeschlagen: %s", e)
        return f"❌ UI-Automatik-Fehler: {e}"


@T.register(
    "computer_task",
    "Bedient eine macOS-App über ihre Oberfläche, um ein Ziel zu erreichen (klicken, tippen, "
    "navigieren). Nutze dies, wenn Timo eine App bedienen/steuern will, für die es kein "
    "spezielles Tool gibt. Ein stärkeres Modell übernimmt die eigentliche Bedienung.",
    {
        "goal": {"type": "string", "description": "was in der App erreicht werden soll"},
        "app": {"type": "string", "description": "optionaler App-Name (z.B. 'Notizen')"},
    },
    ["goal"],
    "uiauto",
)
async def _computer_task(goal: str, app: str = ""):
    if not engine.is_trusted():
        return _SETUP_MSG
    log.info("🖥️ computer_task: %s (app=%s)", goal[:80], app or "vordergrund")
    return await _run_ui_agent(goal, app)
