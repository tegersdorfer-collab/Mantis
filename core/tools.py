"""
Tool-Registry für den agentischen Kern.
Jedes Tool ist eine async-Funktion mit JSON-Schema. Das LLM ruft sie selbst auf.
"""
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from core import decisions
from core.status import BUS

log = logging.getLogger(__name__)

# Fehler-Selbstheilung Stufe 1: erkennt Tools die wiederholt hintereinander
# fehlschlagen und meldet das (nur Erkennung + Sichtbarkeit im Desktop-HUD
# via StatusBus — kein automatisches Auto-Fixing, das wäre ohne menschliche
# Aufsicht zu riskant). Zähler pro Tool, wird bei Erfolg zurückgesetzt.
_tool_failure_counts: dict[str, int] = {}
_FAILURE_THRESHOLD = 3


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                      # JSON-Schema (properties)
    handler: Callable[..., Awaitable[str]]
    required: list[str] = field(default_factory=list)
    category: str = "general"


REGISTRY: dict[str, Tool] = {}


def register(
    name: str,
    description: str,
    parameters: dict | None = None,
    required: list[str] | None = None,
    category: str = "general",
):
    """Decorator zum Registrieren eines Tools."""
    def deco(fn: Callable[..., Awaitable[str]]):
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            parameters=parameters or {},
            handler=fn,
            required=required or [],
            category=category,
        )
        return fn
    return deco


def ollama_schemas(only: list[str] | None = None) -> list[dict]:
    """Exportiert Tool-Schemas im Ollama/OpenAI Function-Calling-Format."""
    tools = []
    for t in REGISTRY.values():
        if only is not None and t.name not in only:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {
                    "type": "object",
                    "properties": t.parameters,
                    "required": t.required,
                },
            },
        })
    return tools


async def execute(name: str, args: dict) -> str:
    """Führt ein Tool aus und gibt die Beobachtung (String) zurück."""
    tool = REGISTRY.get(name)
    if not tool:
        return f"FEHLER: Tool '{name}' existiert nicht."
    try:
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        result = tool.handler(**args)
        if inspect.isawaitable(result):
            result = await result
        _tool_failure_counts[name] = 0
        return str(result)
    except TypeError as e:
        _note_failure(name, e)
        return f"FEHLER: ungültige Argumente für {name}: {e}"
    except Exception as e:
        log.warning(f"Tool {name} fehlgeschlagen: {e}")
        _note_failure(name, e)
        return f"FEHLER beim Ausführen von {name}: {e}"


def _note_failure(name: str, error: Exception) -> None:
    count = _tool_failure_counts.get(name, 0) + 1
    _tool_failure_counts[name] = count
    if count >= _FAILURE_THRESHOLD:
        _tool_failure_counts[name] = 0  # nicht bei jedem weiteren Fehlschlag erneut spammen
        try:
            BUS.emit(
                "tool_failure",
                f"Tool '{name}' ist {count}x hintereinander fehlgeschlagen: {error}",
                detail=name,
            )
        except Exception:
            pass


def tool_names() -> list[str]:
    return list(REGISTRY.keys())


def catalog() -> str:
    """Menschenlesbarer Katalog aller Tools (fürs Dashboard / Debug)."""
    lines = []
    for t in sorted(REGISTRY.values(), key=lambda x: (x.category, x.name)):
        lines.append(f"[{t.category}] {t.name} – {t.description}")
    return "\n".join(lines)


# ── Schnelle Tool-Auswahl (spart Prefill: Tool-Schemas sind teuer) ────────────

import re as _re

# Aktions-/Bedarfssignale → relevante Kategorien
_CATEGORY_KEYWORDS = {
    "productivity": ["aufgabe", "task", "todo", "to-do", "erinner", "reminder",
                     "termin", "kalender", "meeting", "deadline", "fällig",
                     "lösch", "verschieb", "verlege", "ändere den termin", "streich"],
    "habits":       ["gewohnheit", "habit", "abhaken", "abgehakt", "hake", "hak ", "abhak",
                     "streak", "routine", "geschafft", "erledigt für heute"],
    "fitness":      ["training", "workout", "trainiert", "gelaufen", "lauf", "gym",
                     "sport", "übung", "sätze", "kraft", "joggen", "km"],
    "nutrition":    ["gegessen", "gegessen", "getrunken", "mahlzeit", "essen", "esse", "aß",
                     "kalorien", "protein", "snack", "frühstück", "mittag", "abendessen",
                     "ernährung", "kcal", "toast", "brötchen", "kaffee", "hatte zum",
                     "zum frühstück", "zum mittag", "zum abend", "carbs", "kohlenhydrate"],
    "journal":      ["tagebuch", "journal", "stimmung", "mood", "gefühlt", "fühle", "notier"],
    "goals":        ["ziel", "goal", "fortschritt", "vorhaben", "meilenstein"],
    "knowledge":    ["wetter", "news", "nachricht", "suche", "google", "aktuell",
                     "wer ist", "was ist", "preis", "kostet", "wie viel",
                     "brain", "second brain", "notiz", "notiere", "speichere", "inbox",
                     "wissen", "ressource", "daily note", "tageslog"],
    "memory":       ["merk dir", "merke", "behalte", "vergiss nicht", "erinnere dich"],
    "health":       ["schlaf", "schritte", "hrv", "puls", "gewicht", "gesundheit"],
    "system":       ["api-kosten", "api kosten", "api-ausgaben", "token", "llm-kosten",
                     "was kostest du", "deine kosten", "claude-kosten"],
    "filesystem":   ["datei", "ordner", "verzeichnis", "öffne", "öffnen", "dokument",
                     "app öffnen", "app starten", "starte die app", "zeig mir den ordner",
                     "lies die datei", "pdf", "downloads", "schreibtisch", "desktop"],
    "vision":       ["bildschirm", "screenshot", "bildschirmfoto", "siehst du",
                     "was siehst du", "schau dir meinen bildschirm", "guck dir an",
                     "bildschirminhalt"],
    "robot":        ["roboter", "droide", "droid", "x5", "greifer", "greif",
                     "autonom", "patrouill", "fahr los", "fahr vor", "fahr zurück"],
    "flipper":      ["lampe", "licht", "schreibtischlampe", "flipper", "infrarot",
                     "ir ", "fernbedienung", "mach das licht", "schalt das licht",
                     "ventilator", "lüfter", "luefter", "fan"],
    "spotify":      ["musik", "spotify", "song", "playlist", "lautstärke", "lauter",
                     "leiser", "abspielen", "pausier", "was läuft", "nächstes lied",
                     "spiel mal", "spiel was", "spiel etwas", "spiel mir",
                     "spiel den", "spiel die", "spiel das"],
    "uiauto":       ["bediene", "steuere die app", "klick auf", "klicke auf", "navigiere",
                     "in der app", "im fenster", "button", "knopf", "menü", "ui-automatik",
                     "bedien die", "mach in der app"],
    "geo":          ["wo ist", "wo liegt", "wo befindet", "koordinaten", "globus",
                     "weltkarte", "auf der karte", "schlagzeilen", "was ist in der welt",
                     "weltlage", "news-globus", "welche news", "was gibts an news"],
    "gev":          ["god's eye", "gods eye", "eye view", "gev", "3d-globus",
                     "im globus", "auf dem globus", "flieg nach", "fliege nach", "flieg zu", "flugzeuge auf der karte",
                     "satelliten auf der karte", "schiffe auf der karte", "globus steuern"],
    "email":        ["email", "e-mail", "mail", "mails", "posteingang", "ungelesen",
                     "postfach", "gmail", "schreib eine mail", "schick eine mail",
                     "neue mails", "gibt es neue"],
}

# Generelle Aktionsverben → es soll etwas GETAN werden
_ACTION_WORDS = ["erstell", "leg ", "lege ", "anlegen", "hak", "trag", "trage", "setz",
                 "lösch", "aktualisier", "update", "plan", "notier", "füg", "add",
                 "starte", "beende", "merk", "erinner", "mach mir", "trag ein"]


def is_action(text: str) -> bool:
    """Gibt True zurück wenn die Nachricht ein Aktionswort enthält (Tool-Call erzwingen)."""
    t = text.lower()
    return any(w in t for w in _ACTION_WORDS)


# Tools, die auch ohne jede erkannte Kategorie verfügbar bleiben (reines Gespräch /
# unbekannte Anfrage). Eine Quelle, damit select_tools/select_tools_async nicht
# auseinanderlaufen können.
_FALLBACK_TOOLS = ("create_skill", "list_dynamic_skills", "delete_skill", "calculate")


def select_tools(text: str) -> list[str]:
    """
    Wählt relevante Tools basierend auf der Nachricht.
    Leere Liste = reines Gespräch (schneller Pfad ohne Tool-Prefill).
    """
    t = text.lower()
    cats = set()
    for cat, kws in _CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            cats.add(cat)

    has_action = any(w in t for w in _ACTION_WORDS)
    is_question = "?" in text

    # Reines Gespräch ohne Bezug → fast path, aber create_skill/calculate bleiben
    # verfügbar – sonst hat der Agent ausgerechnet bei unerwarteten/neuen Anfragen
    # (die selten in eine Keyword-Kategorie fallen) gar keine Tools zur Wahl.
    if not cats and not has_action:
        return [n for n in _FALLBACK_TOOLS if n in REGISTRY]

    names = [name for name, tool in REGISTRY.items() if tool.category in cats]

    # Aktionswort ohne klare Kategorie → Produktivitäts-Tools als Default
    if has_action and not names:
        names = [n for n, tl in REGISTRY.items() if tl.category == "productivity"]

    # Bei Fragen nach externem Wissen Web-Suche/Wetter sicher dabei
    if is_question and "knowledge" in cats:
        for n in ("web_search", "get_weather"):
            if n not in names:
                names.append(n)

    # Embedding-basierter Fallback: wenn wenige Keyword-Treffer → semantisch ranken
    if len(names) < 4 and len(t) > 10:
        semantic = _semantic_rank(text, top_k=8)
        for n in semantic:
            if n not in names:
                names.append(n)

    names = names[:14]   # Prefill begrenzen

    # create_skill MUSS immer verfügbar sein, sobald überhaupt Tools im Spiel sind –
    # sonst fehlt es genau dann, wenn kein passendes Tool zur Kategorie passt.
    for n in ("create_skill", "list_dynamic_skills", "delete_skill"):
        if n in REGISTRY and n not in names:
            names.append(n)

    return names


async def select_tools_async(text: str) -> tuple[list[str], bool]:
    """
    select_tools + Jev. Der Keyword-Pfad bleibt der Boden (er ist byte-identisch zu
    heute), Jev legt Kategorien obendrauf, die die Keyword-Listen verpassen — das war
    der gemma-Gotcha: „spiel [Song]" wurde von select_tools gar nicht angeboten.
    Gibt (erlaubte Tool-Namen, force_tools) zurück.
    Jev-aktion=True erzwingt Tool-Calls; aktion=False/None ändert nichts, denn ein
    Aktionswort im Text muss weiter ziehen (Recall vor Precision bei kleinen Modellen).
    """
    names = select_tools(text)
    force = bool(names) and is_action(text)
    jev = await decisions.tool_categories(text)
    if jev is None:
        return names, force
    cats, aktion = jev
    extra = [n for n, t in REGISTRY.items() if t.category in cats and n not in names]
    if extra:
        # Deckel wie in select_tools: Prefill begrenzen, Skill-Factory-Tools bleiben immer drin
        factory = [n for n in names if n in ("create_skill", "list_dynamic_skills", "delete_skill")]
        core_names = [n for n in names if n not in factory]
        names = (core_names + extra)[:14] + factory
    # Nur erzwingen, wenn Jev wirklich etwas beigetragen hat (Kategorie) oder ein
    # nicht-Fallback-Tool erlaubt ist — sonst zwingt aktion=True den Agenten auf
    # z.B. "calculate", obwohl das der Anfrage gar nicht hilft (Fallback-Set-Falle).
    if aktion is True and (cats or any(n not in _FALLBACK_TOOLS for n in names)):
        force = True
    return names, force


# ── Embedding-basiertes Tool-Routing ──────────────────────────────────────────

_tool_desc_cache: dict[str, list[float]] | None = None
_tool_desc_ts: float = 0.0


def _semantic_rank(query: str, top_k: int = 6) -> list[str]:
    """
    Rankt Tools semantisch per TF-IDF-ähnlichem Keyword-Overlap gegen Tool-Descriptions.
    Kein LLM-Call nötig — reine Wort-Überlappung zwischen Query und Tool-Descriptions.
    Schnell genug für jeden Request.
    """
    query_words = set(_re.findall(r'\w+', query.lower())) - {
        "ich", "der", "die", "das", "ein", "eine", "und", "oder", "mit", "für",
        "von", "ist", "war", "hat", "wie", "was", "wann", "wo", "bitte", "kann",
        "du", "mir", "mich", "mein", "meine", "nicht", "auch", "noch", "schon",
    }
    if not query_words:
        return []

    scores: list[tuple[str, float]] = []
    for name, tool in REGISTRY.items():
        desc_words = set(_re.findall(r'\w+', tool.description.lower()))
        overlap = len(query_words & desc_words)
        if overlap > 0:
            scores.append((name, overlap / max(len(desc_words), 1) * overlap))

    scores.sort(key=lambda x: x[1], reverse=True)
    return [n for n, _ in scores[:top_k]]
