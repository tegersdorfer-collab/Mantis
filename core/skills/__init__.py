"""
Skill-Registrierung: verbindet alle Domänen-Funktionen mit dem Agent-Tool-Registry.
Der Agent (Qwen3) ruft diese Tools selbstständig auf.

Vor Nutzung muss bind(ctx) aufgerufen werden (im Orchestrator-Start).
Die Tools liegen nach Kategorie in core/skills/<category>.py; der Import unten
löst ihre @T.register-Registrierung aus.
"""
from core import tools as T
from core.skill_context import SkillContext, CTX, bind

# Reihenfolge = Kategorie-Erstauftritt im ursprünglichen skills.py
from . import (
    knowledge,
    skilltree,
    memory,
    productivity,
    health,
    habits,
    fitness,
    nutrition,
    journal,
    goals,
    utility,
    system,
    general,
    ui,
    filesystem,
    vision,
    robot,
    flipper,
    ventilator,
    spotify,
    uiauto,
    geo,
    gev,
    email,
)

# REGISTRY in die ursprüngliche Reihenfolge bringen (select_tools byte-identisch).
# Auto-generiert; neue Tools, die hier fehlen, bleiben am Ende erhalten.
_ORDER = [
    'web_search', 'get_weather', 'remember', 'recall', 'create_task', 'add_subtask',
    'complete_task', 'set_task_progress', 'list_tasks', 'set_reminder', 'create_calendar_event', 'get_calendar',
    'update_calendar_event', 'delete_calendar_event', 'reschedule_after_overrun', 'get_health', 'log_habit', 'create_habit',
    'list_habits', 'log_workout', 'log_workout_detailed', 'recent_workouts', 'log_meal', 'nutrition_today',
    'add_journal', 'create_goal', 'update_goal', 'list_goals', 'calculate', 'read_own_code',
    'list_own_files', 'write_own_code', 'create_skill', 'delete_skill', 'list_dynamic_skills', 'brain_save',
    'brain_search', 'brain_inbox', 'brain_daily_log', 'suggest_next_weight', 'progression_report', 'import_kindle_highlights',
    'speak', 'add_research_query', 'fetch_and_summarize_url', 'log_body_measurement', 'body_progress', 'save_quote',
    'add_thought_to_quote', 'add_directive', 'nutrition_advice', 'claude_code_run', 'git_history_import', 'refresh_tools',
    'delegate_task', 'list_skill_procedures', 'update_skill_procedure',
]

_ordered = {n: T.REGISTRY[n] for n in _ORDER if n in T.REGISTRY}
for _n, _v in T.REGISTRY.items():
    _ordered.setdefault(_n, _v)
T.REGISTRY.clear()
T.REGISTRY.update(_ordered)

__all__ = ["SkillContext", "CTX", "bind", "T"]
