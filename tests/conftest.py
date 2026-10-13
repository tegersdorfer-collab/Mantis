"""Test-weite Konfiguration und Fixtures.

Die DB-URL muss vor jedem Import von settings/config/core.db umgeleitet sein.
Die .env könnte außerdem echte Secrets oder aktivierte Dienste enthalten; Tests
dürfen davon nie abhängen oder echtes Netz anfassen.
"""
import importlib
import sys
from pathlib import Path
from types import ModuleType

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import pytest

from db_test_config import configure_test_database


def _preloaded_database_urls() -> tuple[str, ...]:
    """Sammelt DB-URLs aus Modulen, die vor dem pytest-Bootstrap geladen wurden."""
    urls = []

    settings_module = sys.modules.get("settings")
    settings_cfg = getattr(settings_module, "cfg", None)
    settings_url = getattr(settings_cfg, "DATABASE_URL", None)
    if isinstance(settings_url, str) and settings_url not in urls:
        urls.append(settings_url)

    config_module = sys.modules.get("config")
    config_url = getattr(config_module, "DATABASE_URL", None)
    if isinstance(config_url, str) and config_url not in urls:
        urls.append(config_url)
    config_cfg = getattr(config_module, "cfg", None)
    config_cfg_url = getattr(config_cfg, "DATABASE_URL", None)
    if isinstance(config_cfg_url, str) and config_cfg_url not in urls:
        urls.append(config_cfg_url)

    db_module = sys.modules.get("core.db")
    db_config = getattr(db_module, "config", None)
    db_url = getattr(db_config, "DATABASE_URL", None)
    if isinstance(db_url, str) and db_url not in urls:
        urls.append(db_url)

    return tuple(urls)


def _repair_preloaded_database_modules(test_url: str, old_urls: tuple[str, ...]) -> None:
    """Stellt schon geladene Settings und DB-Module auf die Test-URL um."""
    settings_module = sys.modules.get("settings")
    config_module = sys.modules.get("config")
    db_module = sys.modules.get("core.db")

    if db_module is not None and getattr(db_module, "_pool", None) is not None:
        close_pool = getattr(db_module, "close_pool", None)
        if not callable(close_pool):
            raise ValueError("vorab geladener DB-Pool kann nicht geschlossen werden")
        try:
            close_pool()
        except Exception:
            raise ValueError("vorab geladener DB-Pool konnte nicht sicher geschlossen werden") from None
        if getattr(db_module, "_pool", None) is not None:
            raise ValueError("vorab geladener DB-Pool blieb nach dem Schließen aktiv")

    previous_cfgs = []
    if settings_module is not None:
        cfg = getattr(settings_module, "cfg", None)
        if cfg is not None:
            previous_cfgs.append(cfg)
    if config_module is not None:
        cfg = getattr(config_module, "cfg", None)
        if cfg is not None and all(cfg is not existing for existing in previous_cfgs):
            previous_cfgs.append(cfg)

    if settings_module is not None:
        try:
            settings_module = importlib.reload(settings_module)
        except Exception:
            raise ValueError("vorab geladene settings konnten nicht sicher neu geladen werden") from None
        settings_cfg = getattr(settings_module, "cfg", None)
        if settings_cfg is None or getattr(settings_cfg, "DATABASE_URL", None) != test_url:
            raise ValueError("settings.cfg verwendet nach dem Neuladen nicht die Test-URL")

    for cfg in previous_cfgs:
        try:
            setattr(cfg, "DATABASE_URL", test_url)
        except Exception:
            raise ValueError("vorab geladene Settings-Aliase konnten nicht umgestellt werden") from None

    if config_module is not None:
        if settings_module is not None:
            config_module.cfg = settings_module.cfg
        elif getattr(config_module, "cfg", None) is not None:
            try:
                config_module.cfg.DATABASE_URL = test_url
            except Exception:
                raise ValueError("config.cfg konnte nicht auf die Test-URL umgestellt werden") from None
        config_module.DATABASE_URL = test_url
        if getattr(config_module, "DATABASE_URL", None) != test_url:
            raise ValueError("config.DATABASE_URL verwendet nicht die Test-URL")

    if db_module is not None:
        db_config = getattr(db_module, "config", None)
        if db_config is None:
            raise ValueError("core.db hat keine überprüfbare Konfiguration")
        db_config.DATABASE_URL = test_url
        if getattr(db_config, "DATABASE_URL", None) != test_url:
            raise ValueError("core.db verwendet nicht die Test-URL")
        if getattr(db_module, "_pool", None) is not None:
            raise ValueError("core.db hat nach dem Bootstrap weiterhin einen Pool")

    # Bereits importierte `from config import DATABASE_URL`-Aliase ebenfalls umstellen.
    for module in tuple(sys.modules.values()):
        if not isinstance(module, ModuleType):
            continue
        if getattr(module, "DATABASE_URL", None) in old_urls:
            module.DATABASE_URL = test_url


_PRELOADED_DATABASE_URLS = _preloaded_database_urls()
try:
    _TEST_URL, _PRODUCTION_URLS = configure_test_database(
        dotenv_path=REPO_ROOT / ".env",
        additional_production_urls=_PRELOADED_DATABASE_URLS,
    )
    _repair_preloaded_database_modules(_TEST_URL, _PRELOADED_DATABASE_URLS)
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
