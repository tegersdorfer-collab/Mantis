"""Tests für den Sicherheits-Validator der Skill-Factory (core/skill_factory.py).

validate_source ist ein Linter, keine Sandbox. Diese Tests prüfen einzelne
Strukturregeln; die Ausführungsgrenze testen die Factory-Isolationstests.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.skill_factory import validate_source, SkillValidationError


def _wrap(body: str, name: str = "mein_skill") -> str:
    """Baut eine minimal gültige Skill-Quelle mit gegebenem Funktionskörper."""
    return (
        f"@T.register('{name}', 'desc', {{}}, [], 'general')\n"
        f"async def {name}():\n"
        f"    {body}\n"
    )


# ── Gültige Skills ────────────────────────────────────────────────────────────

def test_valid_skill_passes():
    validate_source(_wrap("return 'ok'"), "mein_skill")


def test_valid_skill_with_allowed_import_passes():
    src = "import json\n\n" + _wrap("return json.dumps({'a': 1})")
    validate_source(src, "mein_skill")


def test_allowed_from_import_passes():
    src = "from datetime import datetime\n\n" + _wrap("return str(datetime.now())")
    validate_source(src, "mein_skill")


# ── Verbotene Imports ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("mod", ["os", "sys", "subprocess", "socket", "shutil",
                                 "importlib", "pickle", "threading", "ctypes"])
def test_banned_import_rejected(mod):
    src = f"import {mod}\n\n" + _wrap("return '1'")
    with pytest.raises(SkillValidationError):
        validate_source(src, "mein_skill")


def test_unknown_import_rejected():
    src = "import numpy\n\n" + _wrap("return '1'")  # nicht auf der Whitelist
    with pytest.raises(SkillValidationError):
        validate_source(src, "mein_skill")


def test_banned_from_import_rejected():
    src = "from os import system\n\n" + _wrap("return '1'")
    with pytest.raises(SkillValidationError):
        validate_source(src, "mein_skill")


# ── Verbotene Namen/Aufrufe ───────────────────────────────────────────────────

@pytest.mark.parametrize("call", ["eval('1')", "exec('x=1')", "__import__('os')",
                                  "open('/etc/passwd')", "compile('1','','eval')"])
def test_banned_builtin_rejected(call):
    with pytest.raises(SkillValidationError):
        validate_source(_wrap(f"return {call}"), "mein_skill")


# ── Struktur-Regeln ───────────────────────────────────────────────────────────

def test_wrong_function_name_rejected():
    with pytest.raises(SkillValidationError):
        validate_source(_wrap("return '1'", name="anderer_name"), "erwarteter_name")


def test_missing_decorator_rejected():
    src = "async def mein_skill():\n    return '1'\n"
    with pytest.raises(SkillValidationError):
        validate_source(src, "mein_skill")


def test_extra_class_rejected():
    src = "class Foo:\n    pass\n\n" + _wrap("return '1'")
    with pytest.raises(SkillValidationError):
        validate_source(src, "mein_skill")


def test_second_function_rejected():
    src = _wrap("return '1'") + "\ndef helper():\n    return 2\n"
    with pytest.raises(SkillValidationError):
        validate_source(src, "mein_skill")


def test_sync_function_rejected():
    src = ("@T.register('mein_skill', 'd', {}, [], 'general')\n"
           "def mein_skill():\n    return '1'\n")  # nicht async
    with pytest.raises(SkillValidationError):
        validate_source(src, "mein_skill")


def test_syntax_error_rejected():
    with pytest.raises(SkillValidationError):
        validate_source("async def mein_skill(:\n    pass", "mein_skill")
