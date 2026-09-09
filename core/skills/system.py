"""
System-Tools — aus core/skills.py extrahiert (verhaltensgleich).
Registriert sich via @T.register beim Import (durch core/skills/__init__.py).
"""
import logging

from core import tools as T
from core.skill_context import CTX

log = logging.getLogger("core.skills")


@T.register("read_own_code",
    "Liest eine Datei aus Mantis' eigener Codebase. Nutze dies bevor du Code änderst "
    "um den aktuellen Stand zu verstehen.",
    {"path": {"type": "string", "description": "Relativer Pfad z.B. 'domains/health.py' oder 'web/index.html'"}},
    ["path"], "system")
async def _read_own_code(path: str):
    from domains.self_modify import read_file
    return read_file(path)

@T.register("list_own_files",
    "Listet Dateien in Mantis' Codebase. Nutze dies zur Orientierung.",
    {"directory": {"type": "string", "description": "Verzeichnis z.B. 'domains' oder 'web' (leer = alles)"}},
    [], "system")
async def _list_own_files(directory: str = ""):
    from domains.self_modify import list_files
    files = list_files(directory)
    return "\n".join(files) if files else "Keine Dateien gefunden."

@T.register("write_own_code",
    "Schreibt oder überschreibt eine Datei in Mantis' Codebase. "
    "Erstellt automatisch ein Git-Backup und triggert einen gesicherten Neustart. "
    "Bei Fehler wird automatisch zur alten Version zurückgerollt. "
    "Nutze dies für Bugfixes, neue Features, UI-Änderungen. "
    "IMMER zuerst read_own_code aufrufen um den aktuellen Stand zu lesen!",
    {
        "path": {"type": "string", "description": "Relativer Pfad z.B. 'domains/health.py'"},
        "content": {"type": "string", "description": "Vollständiger neuer Inhalt der Datei"},
        "description": {"type": "string", "description": "Kurze Beschreibung was geändert wurde und warum"},
    },
    ["path", "content", "description"], "system")
async def _write_own_code(path: str, content: str, description: str):
    from domains.self_modify import write_file
    result = write_file(path, content, description)
    if result["ok"]:
        return f"✅ {result['message']}\nMantis startet neu und prüft ob alles funktioniert. Bei Fehler: automatischer Rollback zu {result['backup_commit'][:8]}."
    return f"❌ {result['message']}"

@T.register("create_skill",
    "Speichert einen INAKTIVEN Python-Skill-Entwurf zur manuellen Prüfung, wenn ein Tool fehlt. "
    "Der Entwurf wird NICHT ausgeführt oder aktiviert, auch nicht nach einem Neustart. "
    "Automatische Ausführung ist ohne isolierte Sandbox deaktiviert. "
    "Schreibe eine async def <skill_name>(...)-Funktion mit "
    "@T.register(name, beschreibung, parameter_schema, required, kategorie). "
    "Statische Prüfungen sind nur Lint und keine Sicherheitsfreigabe. "
    "Die Funktion soll einen String zurückgeben.",
    {
        "skill_name": {"type": "string", "description": "snake_case Tool-Name, z.B. 'convert_currency'"},
        "description": {"type": "string", "description": "Kurze Beschreibung wofür das Skill gut ist"},
        "source_code": {"type": "string", "description": "Vollständiger Python-Code: Decorator @T.register(...) + 'async def <skill_name>(...): ...'"},
    },
    ["skill_name", "description", "source_code"], "system")
async def _create_skill(skill_name: str, description: str, source_code: str):
    from core.skill_factory import create_skill
    result = create_skill(skill_name, description, source_code)
    if result["ok"]:
        if CTX.channel:
            try:
                await CTX.channel.send(f"🛠️ Inaktiver Skill-Entwurf gespeichert: **{skill_name}**\n_{description}_")
            except Exception:
                pass
        try:
            from core import push
            import asyncio as _asyncio
            await _asyncio.to_thread(push.send_push, "🛠️ Skill-Entwurf (inaktiv)", f"{skill_name}: {description}", "/?view=settings")
        except Exception:
            pass
    return f"✅ {result['message']}" if result["ok"] else f"❌ {result['message']}"

@T.register("delete_skill",
    "Entfernt ein zuvor von dir selbst erstelltes Skill (z.B. wenn es fehlerhaft ist oder "
    "nicht mehr gebraucht wird). Funktioniert nur für Skills, die über create_skill entstanden sind.",
    {"skill_name": {"type": "string", "description": "Name des zu entfernenden Skills"}},
    ["skill_name"], "system")
async def _delete_skill(skill_name: str):
    from core.skill_factory import delete_skill
    result = delete_skill(skill_name)
    return f"✅ {result['message']}" if result["ok"] else f"❌ {result['message']}"

@T.register("list_dynamic_skills",
    "Listet gespeicherte Skill-Entwürfe und alte Skill-Dateien; diese werden nicht automatisch aktiviert.",
    {}, [], "system")
async def _list_dynamic_skills():
    from core.skill_factory import list_dynamic_skills
    names = list_dynamic_skills()
    return "Inaktive Skill-Dateien (manuelle Prüfung erforderlich):\n" + "\n".join(names) if names else "Noch keine selbst erstellten Skills."

@T.register("api_costs",
    "Zeigt Token-Verbrauch und API-Kosten deiner LLM-Calls (Claude API kostet Geld, "
    "lokale Ollama-Modelle sind kostenlos). Nutze dies wenn Timo nach API-Kosten, "
    "Token-Verbrauch oder LLM-Ausgaben fragt.",
    {"days": {"type": "integer", "description": "Zeitraum in Tagen (Standard 30)"}},
    [], "system")
async def _api_costs(days: int = 30):
    import asyncio as _asyncio
    from core import llm_usage
    s = await _asyncio.to_thread(llm_usage.summary, max(1, min(int(days or 30), 365)))
    t = s["totals"]
    lines = [f"API-Kosten: heute ${t['today']:.2f} · 7 Tage ${t['week']:.2f} · 30 Tage ${t['month']:.2f}"]
    for m in s["by_model"][:8]:
        cost = f"${m['cost_usd']:.2f}" if m["cost_usd"] else "kostenlos (lokal)"
        lines.append(
            f"- {m['model']}: {m['calls']} Calls, "
            f"{(m['input_tokens'] or 0) // 1000}k in / {(m['output_tokens'] or 0) // 1000}k out → {cost}"
        )
    if len(lines) == 1:
        lines.append("Noch keine Usage-Daten erfasst.")
    return "\n".join(lines)


@T.register("claude_code_run",
    "Startet eine Claude Code Aufgabe im Hintergrund (z.B. 'Baue Feature X', "
    "'Schreibe Tests für Y', 'Refactor Z'). Mantis spawnt einen claude-Subprocess, "
    "du bekommst eine Push-Notification wenn er fertig ist.",
    {
        "task":    {"type": "string",  "description": "Was Claude Code tun soll"},
        "workdir": {"type": "string",  "description": "Arbeitsverzeichnis (Standard: ~/Mantis)"},
    },
    ["task"], "system")
async def _claude_code_run(task: str, workdir: str = "/Users/timoegersdorfer/Mantis"):
    import asyncio, time
    from core import push as _push

    start = time.time()

    async def _run():
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--print", task,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            elapsed = int(time.time() - start)
            result = stdout.decode()[:500] if stdout else ""
            err = stderr.decode()[:200] if stderr else ""
            summary = f"claude --print fertig nach {elapsed}s"
            if result:
                summary += f"\n{result}"
            if err and proc.returncode != 0:
                summary += f"\nFehler: {err}"
            try:
                _push.send_push("🤖 Claude Code fertig", summary[:120], "/")
            except Exception:
                pass
            if CTX.channel:
                try:
                    await CTX.channel.send(f"✅ Claude Code Aufgabe fertig ({elapsed}s):\n_{task[:80]}_")
                except Exception:
                    pass
        except asyncio.TimeoutError:
            if CTX.channel:
                try:
                    await CTX.channel.send(f"⏱ Claude Code Timeout nach 5 Minuten: _{task[:60]}_")
                except Exception:
                    pass
        except FileNotFoundError:
            if CTX.channel:
                try:
                    await CTX.channel.send("❌ `claude` CLI nicht gefunden. Ist Claude Code installiert?")
                except Exception:
                    pass

    asyncio.create_task(_run())
    return f"Claude Code gestartet für: '{task[:80]}'\nArbeitsverzeichnis: {workdir}\nDu bekommst eine Notification wenn er fertig ist."
