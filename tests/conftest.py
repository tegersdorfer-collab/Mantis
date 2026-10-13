"""Test-weite Konfiguration und Fixtures.

Die DB-URL muss vor jedem Import von settings/config/core.db umgeleitet sein.
Die .env könnte außerdem echte Secrets oder aktivierte Dienste enthalten; Tests
dürfen davon nie abhängen oder echtes Netz anfassen.
"""
import sys
from pathlib import Path
from types import ModuleType

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import pytest

from db_test_config import configure_test_database

try:
    configure_test_database(dotenv_path=REPO_ROOT / ".env")
except ValueError as exc:
    pytest.exit(str(exc), returncode=2)

from core import decide


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "db: Test darf gezielt DB-Funktionen oder die Testdatenbank verwenden",
    )


@pytest.fixture(autouse=True)
def _jev_aus(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", False)
    monkeypatch.setattr(decide.config, "JEV_PROVIDER_CHAIN", "")
    monkeypatch.setenv("FORGE_SPUREN", "0")
    decide.reset_for_tests()


def _patch_imported_aliases(monkeypatch, original, replacement):
    """Ersetzt auch `from modul import funktion`-Aliase bereits geladener Module."""
    for module in tuple(sys.modules.values()):
        if not isinstance(module, ModuleType):
            continue
        for name, value in tuple(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, replacement)


@pytest.fixture(autouse=True)
def _db_nebenseiteneffekte_aus(request, monkeypatch):
    """Unterdrückt Fehler-/Event-Logging und LLM-Usage außerhalb markierter DB-Tests."""
    if request.node.get_closest_marker("db") is not None:
        return

    from core import db, llm_usage

    def noop(*args, **kwargs):
        return None

    for function in (db.log_error, db.log_event, llm_usage.record):
        _patch_imported_aliases(monkeypatch, function, noop)
