"""Schutztests für die PostgreSQL-Isolation der Testsuite."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from db_test_config import (
    DEFAULT_TEST_DATABASE_URL,
    PRODUCTION_DATABASE_URLS,
    TEST_DATABASE_URL,
    configure_test_database,
    ensure_test_database_isolated,
)
from core.db import log_error as imported_log_error
from core.db import log_event as imported_log_event
from core.llm_usage import record as imported_usage_record


def test_pytest_process_uses_isolated_database():
    from settings import MantisSettings

    effective_url = os.environ["DATABASE_URL"]

    assert effective_url == TEST_DATABASE_URL
    assert MantisSettings().DATABASE_URL == TEST_DATABASE_URL
    ensure_test_database_isolated(effective_url, PRODUCTION_DATABASE_URLS)


def test_pydantic_settings_environment_overrides_dotenv(tmp_path, monkeypatch):
    from settings import MantisSettings

    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "DATABASE_URL=postgresql://localhost:5432/mantis\n",
        encoding="utf-8",
    )
    test_url = "postgresql://localhost:5432/mantis_test"
    monkeypatch.setenv("DATABASE_URL", test_url)

    assert MantisSettings(_env_file=dotenv_file).DATABASE_URL == test_url


def test_guard_rejects_production_database_name_even_on_another_host():
    with pytest.raises(ValueError, match="Test-DB-Schutz"):
        ensure_test_database_isolated(
            "postgresql://db.example:5432/mantis",
            (),
        )


def test_pytest_aborts_clearly_when_test_url_is_the_production_url():
    production_url = "postgresql://localhost:5432/mantis"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = production_url
    environment["TEST_DATABASE_URL"] = production_url

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_db_isolation.py"],
        cwd=Path(__file__).resolve().parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "Test-DB-Schutz" in result.stdout + result.stderr


def test_guard_normalizes_host_and_default_port_when_comparing_urls(monkeypatch):
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.delenv("PGHOSTADDR", raising=False)

    with pytest.raises(ValueError, match="Test-DB-Schutz"):
        ensure_test_database_isolated(
            "postgresql://127.0.0.1:5432/mantis_stage",
            ("postgres://LOCALHOST/mantis_stage",),
        )


def test_guard_uses_libpq_host_and_port_defaults(monkeypatch):
    monkeypatch.setenv("PGHOST", "db.example")
    monkeypatch.setenv("PGPORT", "5544")
    monkeypatch.delenv("PGHOSTADDR", raising=False)

    with pytest.raises(ValueError, match="Test-DB-Schutz"):
        ensure_test_database_isolated(
            "postgresql:///mantis_stage",
            ("postgresql://db.example:5544/mantis_stage",),
        )


def test_bootstrap_compares_test_url_with_dotenv_database_url(tmp_path):
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "DATABASE_URL=postgresql://db.example:5432/mantis_stage\n",
        encoding="utf-8",
    )
    environment = {
        "TEST_DATABASE_URL": "postgres://db.example/mantis_stage",
    }

    with pytest.raises(ValueError, match="Test-DB-Schutz"):
        configure_test_database(environment, dotenv_file)


def test_default_test_database_is_explicitly_test_named():
    assert DEFAULT_TEST_DATABASE_URL.endswith("/mantis_test")
    environment = {"DATABASE_URL": "postgresql://localhost:5432/mantis"}

    test_url, _ = configure_test_database(environment)

    assert test_url == DEFAULT_TEST_DATABASE_URL
    assert environment["DATABASE_URL"] == DEFAULT_TEST_DATABASE_URL


def test_unmarked_tests_suppress_imported_database_side_effects(monkeypatch):
    from core import db

    writes = []
    monkeypatch.setattr(db, "execute", lambda *args, **kwargs: writes.append((args, kwargs)))

    imported_log_error("unit-test", RuntimeError("synthetic failure"))
    imported_log_event("unit-test", "synthetic event")
    imported_usage_record("openrouter", "openai/gpt-6-luna", 10, 5)

    assert writes == []
