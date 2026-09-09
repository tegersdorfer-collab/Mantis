"""Generate review artifacts; never execute generated Python.

AST checks are lint, not a sandbox. Python imports and introspection can recover
host capabilities, and a plain subprocess would still inherit host permissions.
Until an OS-enforced sandbox is available, creation saves .py.pending files and
startup refuses to import even legacy .py skills. Review and integration must
happen outside this automatic workflow. No runtime opt-in bypass is provided.
"""
import ast
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from core import db
from core import tools as T

log = logging.getLogger(__name__)

MANTIS_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = MANTIS_DIR / "domains" / "dynamic_skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

MAX_SKILLS_PER_DAY = 5

# Erlaubte Top-Level-Module/Pakete für Imports in generierten Skills.
_ALLOWED_ROOTS = {
    "asyncio", "json", "re", "math", "statistics", "datetime", "time", "uuid",
    "random", "secrets", "string", "decimal", "collections", "itertools",
    "textwrap", "unicodedata", "zoneinfo", "calendar", "urllib",
    "httpx", "config", "core", "domains", "llm", "memory", "tools",
}
_BANNED_ROOTS = {
    "os", "sys", "subprocess", "socket", "ctypes", "shutil", "pty",
    "importlib", "builtins", "pickle", "marshal", "multiprocessing",
    "threading", "signal", "resource",
}
_BANNED_NAMES = {
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "open", "input", "breakpoint",
}


class SkillValidationError(Exception):
    pass


def _check_import_root(root: str) -> None:
    if root in _BANNED_ROOTS:
        raise SkillValidationError(f"Import nicht erlaubt: '{root}' (gesperrtes Modul).")
    if root not in _ALLOWED_ROOTS:
        raise SkillValidationError(
            f"Import nicht erlaubt: '{root}' ist nicht auf der Whitelist "
            f"({sorted(_ALLOWED_ROOTS)})."
        )


def validate_source(source: str, expected_name: str) -> None:
    """Lint source structure; acceptance never authorizes execution."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise SkillValidationError(f"Syntaxfehler: {e}")

    func_defs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    other_defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    if other_defs:
        raise SkillValidationError("Nur eine einzige async-Funktion ist erlaubt, keine weiteren Defs/Klassen.")
    if len(func_defs) != 1:
        raise SkillValidationError("Es muss genau eine 'async def'-Funktion definiert sein.")

    fn = func_defs[0]
    if fn.name != expected_name:
        raise SkillValidationError(f"Funktionsname muss exakt '{expected_name}' sein (war: '{fn.name}').")
    if not fn.decorator_list:
        raise SkillValidationError("Die Funktion muss mit @T.register(...) dekoriert sein.")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import_root(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            _check_import_root((node.module or "").split(".")[0])
        elif isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise SkillValidationError(f"Verbotener Name verwendet: '{node.id}'.")
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED_NAMES:
            raise SkillValidationError(f"Verbotenes Attribut verwendet: '.{node.attr}'.")


def _today_count() -> int:
    log_ = db.get_setting("skill_factory_log", []) or []
    cutoff = datetime.now() - timedelta(hours=24)
    recent = [t for t in log_ if datetime.fromisoformat(t) > cutoff]
    return len(recent)


def _record_attempt() -> None:
    log_ = db.get_setting("skill_factory_log", []) or []
    cutoff = datetime.now() - timedelta(hours=24)
    log_ = [t for t in log_ if datetime.fromisoformat(t) > cutoff]
    log_.append(datetime.now().isoformat())
    db.set_setting("skill_factory_log", log_)


_PROMPT_INJECTION_PATTERNS = re.compile(
    r"(ignore (previous|above|all)|system\s*prompt|you are now|jailbreak|"
    r"disregard|override|<\s*/?system|<\s*/?instruction|\[INST\]|###\s*system)",
    re.IGNORECASE,
)


def _sanitize_text(text: str, field: str, max_len: int = 200) -> str:
    """Kürzt und prüft auf Prompt-Injection-Muster."""
    text = text.strip()[:max_len]
    if _PROMPT_INJECTION_PATTERNS.search(text):
        raise SkillValidationError(f"'{field}' enthält unerlaubte Anweisungs-Muster (Prompt-Injection).")
    return text


def create_skill(skill_name: str, description: str, source_code: str) -> dict:
    """
    Erstellt einen inaktiven Code-Entwurf zur manuellen Prüfung.
    Gibt {'ok': bool, 'message': str} zurück.
    """
    if _today_count() >= MAX_SKILLS_PER_DAY:
        return {"ok": False, "message": (
            f"Tages-Limit erreicht ({MAX_SKILLS_PER_DAY} neue Skills/24h). "
            "Erstmal bestehende Skills nutzen/reparieren statt neue zu bauen."
        )}

    if not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", skill_name):
        return {"ok": False, "message": "skill_name muss snake_case sein, 3-40 Zeichen, nur a-z/0-9/_."}

    try:
        description = _sanitize_text(description, "description")
    except SkillValidationError as e:
        return {"ok": False, "message": str(e)}
    if skill_name in T.REGISTRY:
        return {"ok": False, "message": f"Tool '{skill_name}' existiert bereits."}

    file_path = SKILLS_DIR / f"{skill_name}.py.pending"
    if file_path.exists() or (SKILLS_DIR / f"{skill_name}.py").exists():
        return {"ok": False, "message": f"Datei '{file_path.name}' existiert bereits."}

    _record_attempt()

    try:
        validate_source(source_code, skill_name)
    except SkillValidationError as e:
        return {"ok": False, "message": f"Validierung fehlgeschlagen: {e}"}

    # Comment each metadata line: user text must never become executable source.
    header = "# INACTIVE: generated draft; manual review required.\n"
    header += "".join(f"# {line}\n" for line in description.splitlines())
    header += f"# Created: {datetime.now().isoformat(timespec='seconds')}\n"
    header += "from core import tools as T\n\n"
    full_source = header + source_code.strip() + "\n"

    try:
        # Exclusive creation also prevents following a pre-existing symlink.
        with file_path.open("x", encoding="utf-8") as artifact:
            artifact.write(full_source)
    except Exception as e:
        return {"ok": False, "message": f"Schreiben fehlgeschlagen: {e}"}

    log.info("Skill-Entwurf zur Prüfung gespeichert (inaktiv): %s", skill_name)
    return {
        "ok": True,
        "active": False,
        "status": "pending_review",
        "path": str(file_path),
        "message": (
            f"Skill '{skill_name}' als inaktiver Entwurf gespeichert: {file_path.name}. "
            "Manuelle Prüfung und Integration erforderlich; automatische Ausführung "
            "ist ohne isolierte Sandbox deaktiviert."
        ),
    }


def delete_skill(skill_name: str) -> dict:
    """Remove a draft or legacy generated file, without executing its contents."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", skill_name):
        return {"ok": False, "message": "Ungültiger Skill-Name."}
    paths = [SKILLS_DIR / f"{skill_name}.py.pending", SKILLS_DIR / f"{skill_name}.py"]
    found = False
    for path in paths:
        if path.exists() or path.is_symlink():
            path.unlink()
            found = True
    if not found:
        return {"ok": False, "message": f"'{skill_name}' ist kein dynamisch erstelltes Skill."}
    T.REGISTRY.pop(skill_name, None)
    return {"ok": True, "message": f"Skill '{skill_name}' entfernt."}


def list_dynamic_skills() -> list[str]:
    """List inactive drafts and legacy files; presence does not mean activation."""
    names = {p.stem for p in SKILLS_DIR.glob("*.py") if p.stem != "__init__"}
    names.update(p.name.removesuffix(".py.pending") for p in SKILLS_DIR.glob("*.py.pending"))
    return sorted(names)


def load_all_on_startup() -> int:
    """Fail closed: never import generated Python, including legacy artifacts."""
    names = list_dynamic_skills()
    if names:
        log.warning(
            "%d dynamische Skill-Dateien bleiben inaktiv: isolierte Sandbox fehlt; "
            "manuelle Prüfung und Integration erforderlich.", len(names)
        )
    return 0
