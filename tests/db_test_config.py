"""Hilfsfunktionen zur sicheren PostgreSQL-Konfiguration der Testsuite."""

from __future__ import annotations

import ipaddress
import os
import getpass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from dotenv import dotenv_values

DEFAULT_PRODUCTION_DATABASE_URL = "postgresql://localhost:5432/mantis"
DEFAULT_TEST_DATABASE_URL = "postgresql://localhost:5432/mantis_test"

TEST_DATABASE_URL = DEFAULT_TEST_DATABASE_URL
PRODUCTION_DATABASE_URLS: tuple[str, ...] = ()


def _dotenv_database_url(dotenv_path: str | Path | None) -> str | None:
    if dotenv_path is None or not Path(dotenv_path).is_file():
        return None
    value = dotenv_values(dotenv_path).get("DATABASE_URL")
    return value.strip() if value and value.strip() else None


def production_database_url(
    environ: dict[str, str] | os._Environ[str] | None = None,
    dotenv_path: str | Path | None = None,
) -> str:
    """Liefert die Produktions-URL mit derselben Env-vor-.env-Priorität wie Pydantic."""
    environment = os.environ if environ is None else environ
    return (
        environment.get("DATABASE_URL", "").strip()
        or _dotenv_database_url(dotenv_path)
        or DEFAULT_PRODUCTION_DATABASE_URL
    )


def normalize_database_target(database_url: str) -> tuple[str, int, str]:
    """Normalisiert Host, Port und Datenbankname einer PostgreSQL-URL."""
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("leere PostgreSQL-URL")

    try:
        parsed = urlsplit(database_url.strip())
        if parsed.scheme.lower() not in {"postgres", "postgresql"}:
            raise ValueError("kein PostgreSQL-Schema")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("hostaddr") or query.get("service") or os.environ.get("PGHOSTADDR"):
            raise ValueError("nicht direkt vergleichbarer PostgreSQL-Endpunkt")

        host_values = query.get("host")
        if host_values:
            if len(host_values) != 1 or "," in host_values[0] or not host_values[0]:
                raise ValueError("mehrdeutiger PostgreSQL-Host")
            host = host_values[0]
        else:
            host = parsed.hostname or os.environ.get("PGHOST") or "localhost"
            if "," in host:
                raise ValueError("mehrdeutiger PostgreSQL-Host")

        port_values = query.get("port")
        if port_values:
            if len(port_values) != 1:
                raise ValueError("mehrdeutiger PostgreSQL-Port")
            port = int(port_values[0])
        else:
            parsed_port = parsed.port
            port = parsed_port if parsed_port is not None else int(os.environ.get("PGPORT") or 5432)

        path_database = unquote(parsed.path.lstrip("/"))
        database_values = query.get("dbname")
        if database_values is not None:
            if len(database_values) != 1:
                raise ValueError("mehrdeutiger PostgreSQL-Datenbankname")
            database = database_values[0]
            if not database:
                raise ValueError("leerer PostgreSQL-Datenbankname")
        else:
            database = path_database

        if not database:
            user_values = query.get("user")
            if user_values is not None and len(user_values) != 1:
                raise ValueError("mehrdeutiger PostgreSQL-Benutzername")
            query_user = user_values[0] if user_values else ""
            uri_user = unquote(parsed.username) if parsed.username else ""
            database = query_user or uri_user or os.environ.get("PGUSER") or getpass.getuser()
    except (TypeError, ValueError) as exc:
        raise ValueError("PostgreSQL-URL kann nicht sicher normalisiert werden") from exc

    if not host or not database or not 1 <= port <= 65535:
        raise ValueError("PostgreSQL-URL benötigt Host, Port und Datenbankname")

    try:
        address = ipaddress.ip_address(host)
        normalized_host = "localhost" if address.is_loopback else address.compressed
    except ValueError:
        normalized_host = host.rstrip(".").casefold()

    return normalized_host, port, database.casefold()


def ensure_test_database_isolated(
    test_database_url: str,
    production_database_urls: tuple[str, ...] | list[str],
) -> None:
    """Bricht ab, wenn die Test-URL auf die Produktionsdatenbank zeigt."""
    try:
        test_target = normalize_database_target(test_database_url)
    except ValueError:
        raise ValueError(
            "Test-DB-Schutz: TEST_DATABASE_URL kann nicht sicher geprüft werden."
        ) from None

    if test_target[2] == "mantis":
        raise ValueError(
            "Test-DB-Schutz: Die Testdatenbank darf nicht 'mantis' heißen."
        )

    for production_url in production_database_urls:
        try:
            production_target = normalize_database_target(production_url)
        except ValueError:
            raise ValueError(
                "Test-DB-Schutz: DATABASE_URL aus Umgebung oder .env kann nicht sicher geprüft werden."
            ) from None
        if test_target == production_target:
            raise ValueError(
                "Test-DB-Schutz: TEST_DATABASE_URL zeigt auf dieselbe Datenbank wie DATABASE_URL."
            )


def configure_test_database(
    environ: dict[str, str] | os._Environ[str] | None = None,
    dotenv_path: str | Path | None = None,
    additional_production_urls: tuple[str, ...] | list[str] = (),
) -> tuple[str, tuple[str, ...]]:
    """Prüft die Test-URL und setzt sie vor Settings-/DB-Imports als DATABASE_URL."""
    global PRODUCTION_DATABASE_URLS, TEST_DATABASE_URL

    environment = os.environ if environ is None else environ
    production_urls = []
    environment_url = environment.get("DATABASE_URL", "").strip()
    dotenv_url = _dotenv_database_url(dotenv_path)
    for candidate in (
        environment_url,
        dotenv_url,
        DEFAULT_PRODUCTION_DATABASE_URL,
        *additional_production_urls,
    ):
        if candidate and candidate not in production_urls:
            production_urls.append(candidate)

    test_url = environment.get("TEST_DATABASE_URL", "").strip() or DEFAULT_TEST_DATABASE_URL
    ensure_test_database_isolated(test_url, production_urls)

    environment["TEST_DATABASE_URL"] = test_url
    environment["DATABASE_URL"] = test_url

    if environ is None:
        TEST_DATABASE_URL = test_url
        PRODUCTION_DATABASE_URLS = tuple(production_urls)

    return test_url, tuple(production_urls)
