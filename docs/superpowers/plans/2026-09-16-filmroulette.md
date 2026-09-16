# Filmroulette Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eine eigenständige Filmroulette-Webapp neben Mantis bauen, die Filme über IMDb/TMDB verwaltet, deutsche Abo-Kategorien automatisch bestimmt, Bewertungen von 1 bis 100 speichert und einen zweistufigen Entscheidungs-Flow für ungesehene Filme anbietet.

**Architecture:** Ein eigenes Python-Projekt unter /Users/timoegersdorfer/filmroulette liefert eine FastAPI-App aus und persistiert ausschließlich in SQLite. Katalogzugriffe sind über ein Provider-Interface von der Domänenlogik getrennt; das Frontend ist Vanilla-JS mit SVG/Canvas-Glücksrad.

**Tech Stack:** Python 3.14, FastAPI, Uvicorn, httpx, SQLite (sqlite3), Vanilla-JS, HTML/CSS, pytest.

**Spec:** docs/superpowers/specs/2026-09-16-filmroulette-design.md

## Global Constraints

- Die App ist vollständig eigenständig und importiert kein Mantis-Modul.
- Der Markt ist fest DE; die Kategoriepriorität lautet Prime Video > Netflix > Disney+ > Andere.
- Nur Flatrate-/Abo-Verfügbarkeit zählt für die ersten drei Kategorien; Leihe und Kauf werden unter Andere geführt.
- IMDb-TT-IDs sind eindeutig; eine erneute Aufnahme erzeugt keinen Duplikatdatensatz.
- Bewertungen sind optionale Ganzzahlen von 1 bis 100 und nur bei watched=true erlaubt.
- Gewinnerentscheidungen und watched_at werden atomar in SQLite gespeichert.
- Die App bindet standardmäßig an Loopback und akzeptiert für Tailnet-Betrieb nur eine explizite Tailscale-Adresse.
- Tests ersetzen externe Katalogdienste vollständig; kein Test greift auf TMDB, IMDb oder echte Anbieter zu.
- Keine Secrets, Tokens oder .env-Dateien werden committed.

---

### Task 1: Eigenständiges Projekt, Konfiguration und sichere Tailscale-Bindung

**Files:**
- Create: /Users/timoegersdorfer/filmroulette/pyproject.toml
- Create: /Users/timoegersdorfer/filmroulette/app/__init__.py
- Create: /Users/timoegersdorfer/filmroulette/app/config.py
- Create: /Users/timoegersdorfer/filmroulette/app/main.py
- Create: /Users/timoegersdorfer/filmroulette/tests/test_config.py
- Create: /Users/timoegersdorfer/filmroulette/.gitignore

**Interfaces:**
- Produces AppSettings(host: str, port: int, db_path: Path, tmdb_api_token: str | None) and is_allowed_bind_host(host: str) -> bool.
- Produces create_app(db=None, catalog=None, db_path: Path | None = None) -> FastAPI, initially with a health endpoint.
- Later tasks consume AppSettings for startup and create_app for API tests.

- [ ] Step 1: Write the failing tests

~~~
from pathlib import Path

import pytest

from app.config import AppSettings, is_allowed_bind_host


def test_allows_loopback_and_tailscale_addresses():
    assert is_allowed_bind_host("127.0.0.1")
    assert is_allowed_bind_host("100.101.102.103")
    assert is_allowed_bind_host("fd7a:115c:a1e0::1234")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "8.8.8.8"])
def test_rejects_public_or_lan_bind_addresses(host):
    assert not is_allowed_bind_host(host)


def test_settings_use_safe_defaults(monkeypatch):
    monkeypatch.delenv("FILMROULETTE_HOST", raising=False)
    monkeypatch.delenv("FILMROULETTE_PORT", raising=False)
    monkeypatch.delenv("FILMROULETTE_DB", raising=False)
    settings = AppSettings.from_env()
    assert settings.host == "127.0.0.1"
    assert settings.port == 7780
    assert settings.db_path == Path("data/filmroulette.sqlite3")
~~~

- [ ] Step 2: Run tests to verify they fail

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_config.py -q

Expected: FAIL because the standalone project and configuration module do not exist.

- [ ] Step 3: Write the minimal implementation

Implement host parsing with ipaddress.ip_address: allow loopback and IPv4 addresses in 100.64.0.0/10 plus IPv6 addresses in fd7a:115c:a1e0::/48; reject wildcard and all other addresses. Read environment variables without logging token values.

- [ ] Step 4: Run tests to verify they pass

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_config.py -q

Expected: all configuration tests pass.

- [ ] Step 5: Add the project scaffold and commit

Create package metadata and .gitignore entries for .env, data/, caches and virtual environments, then run:

~~~
cd /Users/timoegersdorfer/filmroulette
git init
git add pyproject.toml app/__init__.py app/config.py tests/test_config.py .gitignore
git commit -m "chore: standalone filmroulette scaffold"
~~~

---

### Task 2: SQLite-Schema und Film-Repository

**Files:**
- Create: /Users/timoegersdorfer/filmroulette/app/db.py
- Create: /Users/timoegersdorfer/filmroulette/app/models.py
- Create: /Users/timoegersdorfer/filmroulette/tests/test_db.py

**Interfaces:**
- Consumes AppSettings.db_path from Task 1.
- Produces Database(path: Path) with initialize(), transaction(), get_movie(movie_id), get_movie_by_imdb_id(imdb_id), insert_movie(movie), list_movies(filters), set_watched(movie_id, watched, watched_at) and set_rating(movie_id, rating).
- Produces MovieRecord and MovieFilters dataclasses used by Tasks 3–5.

- [ ] Step 1: Write failing repository tests

~~~
from datetime import datetime, timezone

import pytest

from app.db import Database
from app.models import MovieRecord


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "movies.sqlite3")
    database.initialize()
    return database


def test_imdb_id_is_unique(db):
    movie = MovieRecord.new(imdb_id="tt1234567", title="Example", imdb_url="https://www.imdb.com/title/tt1234567/")
    db.insert_movie(movie)
    with pytest.raises(ValueError, match="bereits"):
        db.insert_movie(movie)


def test_rating_requires_watched_movie(db):
    movie_id = db.insert_movie(MovieRecord.new(imdb_id="tt1234568", title="Unseen", imdb_url="https://www.imdb.com/title/tt1234568/"))
    with pytest.raises(ValueError, match="gesehen"):
        db.set_rating(movie_id, 80)


def test_watched_transition_stores_time_and_rating(db):
    movie_id = db.insert_movie(MovieRecord.new(imdb_id="tt1234569", title="Seen", imdb_url="https://www.imdb.com/title/tt1234569/"))
    now = datetime(2026, 9, 16, 20, 30, tzinfo=timezone.utc)
    db.set_watched(movie_id, True, now)
    db.set_rating(movie_id, 91)
    row = db.get_movie(movie_id)
    assert row.watched is True
    assert row.watched_at == now
    assert row.rating == 91
~~~

- [ ] Step 2: Run tests to verify they fail

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_db.py -q

Expected: FAIL because the database, schema and model types do not exist.

- [ ] Step 3: Write the minimal implementation

Create the approved movies table with unique IMDb constraint, category/status checks and indexes. Use one SQLite connection per operation with foreign keys, sqlite3.Row, explicit transactions and UTC ISO-8601 timestamps. Translate unique-constraint errors to the repository ValueError and enforce the watched/rating invariant in the same transaction as the update.

- [ ] Step 4: Add list filtering and sorting tests

~~~
def test_list_movies_filters_category_and_sorting(db):
    db.insert_movie(MovieRecord.new(imdb_id="tt0000001", title="Zulu", release_year=2000, primary_category="other", imdb_url="https://www.imdb.com/title/tt0000001/"))
    db.insert_movie(MovieRecord.new(imdb_id="tt0000002", title="Alpha", release_year=2020, primary_category="prime", imdb_url="https://www.imdb.com/title/tt0000002/"))
    rows = db.list_movies(category="prime", sort="title", direction="asc")
    assert [row.title for row in rows] == ["Alpha"]
~~~

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_db.py -q

Expected: the new test fails before filter/sort support and passes after the repository implements an allowlisted SQL order map.

- [ ] Step 5: Run the complete repository test file

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_db.py -q

Expected: all database tests pass with a temporary SQLite file and no external service.

- [ ] Step 6: Commit the database layer

~~~
cd /Users/timoegersdorfer/filmroulette
git add app/db.py app/models.py tests/test_db.py
git commit -m "feat: add sqlite movie repository"
~~~

---

### Task 3: IMDb-Auflösung, TMDB-Adapter und Kategorieentscheidung

**Files:**
- Modify: /Users/timoegersdorfer/filmroulette/app/models.py
- Create: /Users/timoegersdorfer/filmroulette/app/catalog.py
- Create: /Users/timoegersdorfer/filmroulette/tests/test_catalog.py
- Create: /Users/timoegersdorfer/filmroulette/tests/fixtures/tmdb_movie.json
- Create: /Users/timoegersdorfer/filmroulette/tests/fixtures/tmdb_watch_providers.json

**Interfaces:**
- Consumes Database and MovieRecord from Task 2.
- Produces parse_imdb_id(value: str) -> str, classify_availability(availability) -> str and build_provider_link(provider, title) -> str.
- Produces CatalogProvider with search_titles(query), resolve_imdb_id(imdb_id), fetch_movie(tmdb_id) and fetch_availability(tmdb_id, region="DE").
- Produces TMDBCatalog, which reads TMDB_API_TOKEN from settings and never exposes it in exceptions or returned data.

- [ ] Step 1: Write failing identity and categorization tests

~~~
import pytest

from app.catalog import classify_availability, parse_imdb_id


def test_parse_imdb_id_accepts_url_and_raw_id():
    assert parse_imdb_id("tt1234567") == "tt1234567"
    assert parse_imdb_id("https://www.imdb.com/title/tt1234567/?ref_=fn_all_ttl_1") == "tt1234567"


@pytest.mark.parametrize("value", ["", "The Matrix", "https://example.com/title/tt1234567"])
def test_parse_imdb_id_rejects_ambiguous_values(value):
    with pytest.raises(ValueError):
        parse_imdb_id(value)


def test_classification_obeys_prime_netflix_disney_priority():
    availability = {
        "flatrate": [
            {"provider_name": "Netflix"},
            {"provider_name": "Disney Plus"},
            {"provider_name": "Amazon Prime Video"},
        ]
    }
    assert classify_availability(availability) == "prime"


def test_purchase_only_is_other():
    assert classify_availability({"buy": [{"provider_name": "Netflix"}]}) == "other"
~~~

- [ ] Step 2: Run tests to verify they fail

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_catalog.py -q

Expected: FAIL because the identity parser and categorization function do not exist.

- [ ] Step 3: Implement pure catalog rules first

Normalize provider names and IDs in one mapping. Inspect only flatrate for category selection; keep rent and buy in the stored availability payload. Implement the fixed order prime, netflix, disney, other. Build provider links from a direct URL when available, otherwise from a URL-encoded provider search URL; IMDb remains the fallback for Disney and Other.

- [ ] Step 4: Add a deterministic fake-provider metadata test

~~~
from app.catalog import FakeCatalog


def test_fake_catalog_returns_complete_candidate_shape():
    candidate = FakeCatalog().resolve_imdb_id("tt1234567")
    assert candidate.imdb_id == "tt1234567"
    assert candidate.title
    assert candidate.imdb_url.endswith("/tt1234567/")
    assert candidate.availability_status == "verified"
~~~

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_catalog.py -q

Expected: FAIL until the adapter model and fake fixture are complete, then PASS.

- [ ] Step 5: Test the real HTTP boundary with an in-memory transport

Use httpx.MockTransport to return checked-in movie and watch-provider fixtures for exact TMDB paths. Assert the adapter sends the bearer token and watch_region=DE, parses responses into internal types and redacts the token from forced HTTP errors. No test may perform a live network request.

- [ ] Step 6: Commit the catalog boundary

~~~
cd /Users/timoegersdorfer/filmroulette
git add app/models.py app/catalog.py tests/test_catalog.py tests/fixtures
git commit -m "feat: resolve imdb movies and classify providers"
~~~

---

### Task 4: Film-Service und REST-Endpunkte

**Files:**
- Create: /Users/timoegersdorfer/filmroulette/app/movies.py
- Modify: /Users/timoegersdorfer/filmroulette/app/main.py
- Create: /Users/timoegersdorfer/filmroulette/tests/test_movies_api.py

**Interfaces:**
- Consumes Database, CatalogProvider, MovieRecord and AppSettings.
- Produces MovieService.add_by_title, MovieService.add_by_imdb_url, MovieService.refresh, MovieService.list, MovieService.mark_watched and MovieService.rate.
- Exposes GET /api/movies, GET /api/movies/search, POST /api/movies, POST /api/movies/{id}/refresh, PATCH /api/movies/{id}/watched and PATCH /api/movies/{id}/rating.

- [ ] Step 1: Write failing API tests

~~~
from fastapi.testclient import TestClient

from app.catalog import FakeCatalog
from app.db import Database
from app.main import create_app


def test_add_by_imdb_link_deduplicates_and_returns_category(tmp_path):
    db = Database(tmp_path / "movies.sqlite3")
    db.initialize()
    client = TestClient(create_app(db=db, catalog=FakeCatalog()))

    payload = {"imdb_url": "https://www.imdb.com/title/tt1234567/"}
    first = client.post("/api/movies", json=payload)
    second = client.post("/api/movies", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["movie"]["imdb_id"] == "tt1234567"
    assert first.json()["movie"]["primary_category"] == "prime"


def test_rating_is_rejected_until_watched(tmp_path):
    db = Database(tmp_path / "movies.sqlite3")
    db.initialize()
    client = TestClient(create_app(db=db, catalog=FakeCatalog()))
    movie_id = client.post("/api/movies", json={"imdb_url": "tt1234567"}).json()["movie"]["id"]

    response = client.patch(f"/api/movies/{movie_id}/rating", json={"rating": 80})

    assert response.status_code == 400
    assert "gesehen" in response.json()["detail"]
~~~

- [ ] Step 2: Run tests to verify they fail

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_movies_api.py -q

Expected: FAIL because the movie service and routes do not exist.

- [ ] Step 3: Implement the service and route contracts

Resolve title searches to candidate DTOs; require a selected tmdb_id or imdb_id for persistence so an ambiguous title never creates a guessed record. On create, resolve metadata, classify availability, persist JSON payloads and return 201. On duplicate, return the existing row with 200. Validate request bodies with Pydantic models and keep sort fields in an allowlist.

- [ ] Step 4: Add watched/rating and refresh tests

~~~
def test_watched_then_rating_persists_local_timestamp(tmp_path):
    db = Database(tmp_path / "movies.sqlite3")
    db.initialize()
    client = TestClient(create_app(db=db, catalog=FakeCatalog()))
    movie_id = client.post("/api/movies", json={"imdb_url": "tt1234567"}).json()["movie"]["id"]

    watched = client.patch(f"/api/movies/{movie_id}/watched", json={"watched": True})
    rated = client.patch(f"/api/movies/{movie_id}/rating", json={"rating": 94})

    assert watched.status_code == 200
    assert rated.status_code == 200
    assert rated.json()["movie"]["rating"] == 94
    assert rated.json()["movie"]["watched_at"]
~~~

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_movies_api.py -q

Expected: the test fails before state endpoints exist and passes after implementation.

- [ ] Step 5: Add list/filter/refresh error tests

Cover query, category, watched status, sorting, duplicate, invalid rating values, unknown movie IDs and an external catalog failure that preserves existing confirmed category data while returning an error status.

- [ ] Step 6: Commit the movie API

~~~
cd /Users/timoegersdorfer/filmroulette
git add app/movies.py app/main.py tests/test_movies_api.py
git commit -m "feat: add movie management api"
~~~

---

### Task 5: Serverseitige Roulette-Logik

**Files:**
- Create: /Users/timoegersdorfer/filmroulette/app/roulette.py
- Modify: /Users/timoegersdorfer/filmroulette/app/main.py
- Create: /Users/timoegersdorfer/filmroulette/tests/test_roulette.py

**Interfaces:**
- Consumes Database and MovieRecord from Tasks 2 and 4.
- Produces RouletteService.shortlist(count=5), RouletteService.eliminate(candidate_ids) and RouletteService.decide(movie_id, candidate_ids).
- Exposes POST /api/roulette/shortlist, POST /api/roulette/eliminate and POST /api/roulette/decide.

- [ ] Step 1: Write failing algorithm tests

~~~
import random

import pytest

from app.db import Database
from app.models import MovieRecord
from app.roulette import RouletteService


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "movies.sqlite3")
    db.initialize()
    for number in range(6):
        db.insert_movie(MovieRecord.new(
            imdb_id=f"tt{number + 2000000}",
            title=f"Movie {number}",
            imdb_url=f"https://www.imdb.com/title/tt{number + 2000000}/",
        ))
    return RouletteService(db, rng=random.Random(7))


def test_shortlist_has_at_most_five_unique_unseen_movies(service):
    result = service.shortlist(count=5)
    assert len(result) == 5
    assert len({movie.id for movie in result}) == 5
    assert all(not movie.watched for movie in result)


def test_three_eliminations_leave_two_and_fourth_spin_decides(service):
    candidate_ids = [1, 2, 3, 4, 5]
    remaining = candidate_ids[:]
    for _ in range(3):
        eliminated, remaining = service.eliminate(remaining)
        assert eliminated not in remaining
    assert len(remaining) == 2
    winner = service.decide(remaining[0], remaining)
    assert winner.watched is True
    assert winner.watched_at is not None
~~~

- [ ] Step 2: Run tests to verify they fail

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_roulette.py -q

Expected: FAIL because roulette service and endpoints do not exist.

- [ ] Step 3: Implement the minimal server-side algorithm

Use SQLite ORDER BY RANDOM() for a shortlist of up to five unseen rows, then validate every supplied candidate against the database. eliminate selects one active candidate with a local injectable RNG, returns it and the remaining IDs. decide performs one transaction with UPDATE movies SET watched=1, watched_at=? WHERE id=? AND watched=0 and returns 409 if zero rows were updated.

- [ ] Step 4: Add edge-case and race-safety tests

Test zero unseen movies, one to four unseen movies, empty candidate lists, duplicate candidate IDs, non-candidate winners, already-seen winners and repeated decision requests. Use a fixed RNG in tests so each branch is deterministic.

- [ ] Step 5: Add API contract tests

Assert JSON includes the selected movie, current remaining IDs, action link and final watched_at. Assert the second decision request returns 409 and leaves the original timestamp unchanged.

- [ ] Step 6: Commit the roulette layer

~~~
cd /Users/timoegersdorfer/filmroulette
git add app/roulette.py app/main.py tests/test_roulette.py
git commit -m "feat: add two-stage movie roulette"
~~~

---

### Task 6: Weboberfläche für Übersicht, Bewertung und Glücksrad

**Files:**
- Create: /Users/timoegersdorfer/filmroulette/app/static/index.html
- Create: /Users/timoegersdorfer/filmroulette/app/static/app.js
- Create: /Users/timoegersdorfer/filmroulette/app/static/styles.css
- Modify: /Users/timoegersdorfer/filmroulette/app/main.py
- Create: /Users/timoegersdorfer/filmroulette/tests/test_static_app.py

**Interfaces:**
- Consumes REST contracts from Tasks 4 and 5.
- Produces a responsive single-page UI with list, filters, add flow, rating editor and roulette flow.
- UI actions call only /api; no external API key reaches the browser.

- [ ] Step 1: Write the failing static-app contract test

~~~
from fastapi.testclient import TestClient

from app.main import create_app


def test_root_serves_filmroulette_shell(tmp_path):
    client = TestClient(create_app(db_path=tmp_path / "movies.sqlite3"))
    response = client.get("/")
    assert response.status_code == 200
    assert "Filmroulette" in response.text
    assert "/static/app.js" in response.text
~~~

- [ ] Step 2: Run the test to verify it fails

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_static_app.py -q

Expected: FAIL because static files and root route do not exist.

- [ ] Step 3: Implement the responsive shell

Serve app/static with FastAPI and render a German UI with header metrics, four category tabs, add dialog for title search and IMDb URL, table/cards with filters and sort, inline 1–100 rating input only for seen films, manual seen toggle, refresh action, neutral poster fallback and visible unknown/error availability status.

- [ ] Step 4: Implement the roulette interaction

Draw the current pool in an SVG wheel. The first phase requests up to five server-selected unique films and animates each result. The second phase shows active candidates, calls elimination three times, then decision once for the fourth spin. Disable buttons during requests and show a result card with the returned action link.

- [ ] Step 5: Verify browser-facing JavaScript syntax and static contract

Run:

~~~
cd /Users/timoegersdorfer/filmroulette
python3.14 -m pytest tests/test_static_app.py -q
node --check app/static/app.js
~~~

Expected: both commands exit with code 0.

- [ ] Step 6: Commit the frontend

~~~
cd /Users/timoegersdorfer/filmroulette
git add app/static app/main.py tests/test_static_app.py
git commit -m "feat: add film list rating and roulette ui"
~~~

---

### Task 7: Betrieb, Dokumentation und vollständige Verifikation

**Files:**
- Create: /Users/timoegersdorfer/filmroulette/requirements.txt
- Create: /Users/timoegersdorfer/filmroulette/requirements-test.txt
- Create: /Users/timoegersdorfer/filmroulette/.env.example
- Create: /Users/timoegersdorfer/filmroulette/README.md
- Modify: /Users/timoegersdorfer/filmroulette/app/main.py
- Create: /Users/timoegersdorfer/filmroulette/scripts/start.sh

**Interfaces:**
- Consumes all application modules and tests.
- Produces documented start commands for local and Tailscale-only use.

- [ ] Step 1: Write the startup smoke test

~~~
def test_app_health_reports_ready(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    client = TestClient(create_app(db_path=tmp_path / "movies.sqlite3"))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "filmroulette"}
~~~

- [ ] Step 2: Run the smoke test to verify it fails

Run: cd /Users/timoegersdorfer/filmroulette && python3.14 -m pytest tests/test_static_app.py::test_app_health_reports_ready -q

Expected: FAIL until health response and startup initialization are finalized.

- [ ] Step 3: Add runtime files and safe startup

Pin compatible dependency floors, keep test dependencies separate, add .env.example with placeholder names only, and make scripts/start.sh load the environment then execute Uvicorn. The app must reject unsafe host values before Uvicorn starts and create the configured data directory/database automatically.

- [ ] Step 4: Document setup and Tailscale access

Document TMDB token setup, local default access, how to set the Mac's Tailscale IP, how to open http://<tailscale-ip>:7780 from another Tailnet device, German category precedence and rating/roulette rules. State that the token is never entered in the browser.

- [ ] Step 5: Run all verification commands

Run each command freshly:

~~~
cd /Users/timoegersdorfer/filmroulette
python3.14 -m pytest -q
python3.14 -m ruff check .
node --check app/static/app.js
~~~

Start Uvicorn separately on 127.0.0.1:7780, query /health and /api/movies from another shell, then stop the server. Confirm the server also rejects 0.0.0.0 in a focused configuration test.

- [ ] Step 6: Commit the operational layer

~~~
cd /Users/timoegersdorfer/filmroulette
git add requirements.txt requirements-test.txt .env.example README.md scripts/start.sh app/main.py tests
git commit -m "docs: document filmroulette operation"
~~~

## Completion Checklist

- [ ] The standalone project is outside Mantis and imports no Mantis code.
- [ ] IMDb-TT-ID deduplication and title/IMDb-link add flows work.
- [ ] German provider data is classified with Prime > Netflix > Disney+ > Other.
- [ ] The table supports search, sorting, category filter and seen filter.
- [ ] Seen transitions store a timestamp and enable a 1–100 rating.
- [ ] The roulette draws unique unseen candidates, eliminates three and decides on the fourth spin.
- [ ] The decision is atomic and repeated decisions return 409.
- [ ] Netflix/Prime action links and Disney/Other IMDb fallback work.
- [ ] The service is reachable via explicit Tailscale address and rejects public wildcard binds.
- [ ] Full tests, Ruff and JavaScript syntax verification were actually run successfully.
