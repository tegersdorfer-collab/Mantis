"""
General-Tools — aus core/skills.py extrahiert (verhaltensgleich).
Registriert sich via @T.register beim Import (durch core/skills/__init__.py).
"""
import logging

from core import tools as T
from core.skill_context import CTX

log = logging.getLogger("core.skills")


@T.register(
    "git_history_import",
    "Importiert Git-Commit-History eines Repos als Wissens-Notizen ins Second Brain.",
    [
        {"name": "repos", "type": "string",
         "description": "Kommagetrennte Pfade oder 'auto' für automatische Suche in ~/Documents, ~/Developer etc."},
        {"name": "limit", "type": "integer",
         "description": "Max. Commits pro Repo (default 100)"},
    ],
)
def _git_history_import(repos: str = "auto", limit: int = 100) -> str:
    import subprocess, os
    from pathlib import Path
    from domains import second_brain as _brain

    search_roots = [Path.home() / d for d in ["Documents", "Developer", "Projects", "repos", "code", "Mantis"]]

    if repos.strip().lower() == "auto":
        found = []
        for root in search_roots:
            if not root.exists():
                continue
            for p in root.iterdir():
                if p.is_dir() and (p / ".git").exists():
                    found.append(str(p))
        repo_paths = found[:10]
    else:
        repo_paths = [r.strip() for r in repos.split(",") if r.strip()]

    if not repo_paths:
        return "Keine Git-Repos gefunden."

    total_imported = 0
    summaries = []

    for repo in repo_paths:
        if not os.path.exists(os.path.join(repo, ".git")):
            continue
        try:
            result = subprocess.run(
                ["git", "-C", repo, "log",
                 f"--max-count={limit}",
                 "--pretty=format:%ad|%s",
                 "--date=short"],
                capture_output=True, text=True, timeout=10,
            )
            lines = [l for l in result.stdout.strip().split("\n") if "|" in l]
            if not lines:
                continue

            repo_name = os.path.basename(repo)
            entries_by_month: dict[str, list[str]] = {}
            for line in lines:
                parts = line.split("|", 1)
                if len(parts) < 2:
                    continue
                date_str, subject = parts[0], parts[1]
                month = date_str[:7]
                entries_by_month.setdefault(month, []).append(f"- {date_str}: {subject}")

            for month, commits in sorted(entries_by_month.items(), reverse=True):
                title = f"Git-Log {repo_name} {month}"
                existing = _brain.search_notes(title, limit=1)
                if not existing:
                    content = f"# Git-History: {repo_name} — {month}\n\n" + "\n".join(commits[:50])
                    _brain.add_note(title=title, content=content, category="resource",
                                    tags=["git", repo_name, "history"])
                    total_imported += 1

            summaries.append(f"{repo_name}: {len(lines)} Commits → {len(entries_by_month)} Monatskarten")
        except Exception as e:
            summaries.append(f"{repo_name}: Fehler ({e})")

    if total_imported:
        return f"✅ Git-History importiert: {total_imported} Monatskarten.\n" + "\n".join(summaries)
    return "Nichts Neues importiert (bereits vorhanden).\n" + "\n".join(summaries)

@T.register(
    "refresh_tools",
    "Scannt ob neue Skill-Dateien verfügbar sind und zeigt aktuelle Tool-Anzahl. Aufrufen wenn eine Fähigkeit zu fehlen scheint.",
    [],
)
def _refresh_tools() -> str:
    tools = T.all_tools() if hasattr(T, "all_tools") else []
    n = len(tools)
    return (
        f"{n} Tools aktuell verfügbar. "
        "Falls ein Tool fehlt: create_skill speichert einen inaktiven Entwurf zur manuellen Prüfung; keine automatische Aktivierung."
    )

@T.register(
    "delegate_task",
    "Delegiert eine komplexe Teilaufgabe an einen isolierten Unteragenten. Ideal für umfangreiche Analysen, Reports oder mehrstufige Operationen die den Hauptkontext nicht aufblähen sollen.",
    {
        "goal": {"type": "string", "description": "Was der Unteragent erreichen soll (natürlichsprachlich, konkret)"},
        "context": {"type": "string", "description": "Zusätzlicher Kontext aus dem aktuellen Gespräch (optional)"},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "Explizite Tool-Liste (optional, leer = alle erlaubten)"},
    },
    ["goal"],
    category="productivity",
)
async def _delegate_task(goal: str, context: str = "", tools: list | None = None) -> str:
    from tools.delegate import run_subagent
    return await run_subagent(
        goal=goal,
        context=context,
        allowed_tools=tools or None,
    )

@T.register(
    "list_skill_procedures",
    "Zeigt alle gespeicherten SKILL.md Prozeduren die Mantis über die Zeit gelernt hat.",
    [],
    category="general",
)
def _list_skill_procedures() -> str:
    from core.skill_md import list_all
    skills = list_all()
    if not skills:
        return "Noch keine Skill-Prozeduren gespeichert."
    lines = [f"**{s['name']}**: {s['description']} (Trigger: {', '.join(s['triggers'][:3])})"
             for s in skills]
    return f"{len(skills)} Prozedur-Skills:\n" + "\n".join(lines)

@T.register(
    "update_skill_procedure",
    "Aktualisiert eine bestehende SKILL.md Prozedur. Nutzen wenn Mantis eine bessere Vorgehensweise für eine bekannte Aufgabe gelernt hat.",
    {
        "name": {"type": "string", "description": "Skill-Name (snake_case)"},
        "new_body": {"type": "string", "description": "Neue Prozedur-Beschreibung"},
    },
    ["name", "new_body"],
    category="general",
)
def _update_skill_procedure(name: str, new_body: str) -> str:
    from core.skill_md import update_skill
    if update_skill(name, new_body):
        return f"✅ Skill-Prozedur '{name}' aktualisiert."
    return f"❌ Skill '{name}' nicht gefunden. Mit list_skill_procedures prüfen."

@T.register("speak",
    "Schickt eine Sprachnachricht an Timo statt Text. Nutzen wenn er explizit eine Sprachantwort "
    "möchte, oder bei Morgen-Briefings, Alerts und kurzen proaktiven Nachrichten.",
    {
        "text":  {"type": "string", "description": "Text der vorgelesen werden soll"},
        "voice": {"type": "string", "description": "Aktuell nur eine deutsche Stimme verfügbar (de_DE-thorsten-high), Parameter wird ignoriert"},
    },
    ["text"], "general")
async def _speak(text: str, voice: str = "de_DE-thorsten-high"):
    if not CTX.channel:
        return "Kein Kommunikationskanal verfügbar."
    from core.tts import is_available
    if not is_available():
        return "TTS nicht verfügbar — Piper-Modell fehlt in data/tts/piper/."
    send_voice = getattr(CTX.channel, "send_voice", None)
    if not send_voice:
        return "Kanal unterstützt keine Sprachnachrichten."
    ok = await send_voice(text, voice=voice)
    return "🔊 Sprachnachricht gesendet." if ok else "Fallback auf Text gesendet."
