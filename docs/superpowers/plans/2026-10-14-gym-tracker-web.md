# Gym-Tracker Web-App v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eigene Tracking-Web-App für die Big Three unter `http://100.107.172.123:7782/`, die in die bestehenden Tabellen `workouts`/`workout_sets` schreibt.

**Architecture:** Neue Domain-Fassade `domains/gym/tracker.py` (reine Funktionen über `core.db`, nutzt die Sessionlogik aus `domains/gym/session.py`). Eine eigene kleine FastAPI-App `web/gym_app.py` mit eigener Cookie-Session liefert statische Dateien aus `web/gym/` und JSON unter `/api/*`. `main.py` startet sie als zweiten uvicorn-Server mit Retry, ohne dass Mantis an einem Bind-Fehler scheitert.

**Tech Stack:** Python 3.14, FastAPI/Starlette, uvicorn, psycopg2 (PostgreSQL), Vanilla HTML/CSS/JS, pytest gegen `mantis_test`.

**Spec:** `docs/specs/2026-10-14-gym-tracker-web.md`

## Global Constraints
- Übungs-Allowlist exakt: `("Squats", "Bench Press", "Deadlifts")` in dieser Reihenfolge.
- Port `GYM_PORT=7782`, Host `GYM_HOST` (Default `"127.0.0.1"`, in `.env` später `100.107.172.123`), `GYM_ENABLED=True`, `GYM_SESSION_DAYS=180`.
- Host nur Loopback oder Tailscale-Netz (gleiche Regel wie `validate_dashboard_host` in `main.py`).
- Cookie `mantis_gym`, HttpOnly, SameSite=Strict, `secure` nur bei https. Token-Vergleich mit `hmac.compare_digest` gegen `config.DASHBOARD_TOKEN`; leerer Token-Server → 503.
- Gewicht `0 < weight_kg <= 500`, Wdh. `1 <= reps <= 100`, Gewicht auf 0,25 kg gerundet.
- psycopg2: kein literales `%` in SQL mit Parametern.
- Tests: `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .` grün. Tests laufen gegen `mantis_test` (conftest erzwingt das).
- Keine echten Gesundheits-/Trainingsdaten in Fixtures; Testdaten synthetisch.
- Dashboard-Auth (`web/auth.py`) bleibt unverändert.

## Review Focus
- Doppelt gesendeter Satz (Retry nach Timeout, der Server hatte schon gespeichert) → genau ein Satz in der DB, Antwort identisch. Test in Task 1.
- Altdaten mit Aufwärmsätzen und deutschen Legacy-Duplikat-Zeilen → Vorbefüllung nur aus Arbeitssätzen der letzten Session, via `exercise_history` (die Duplikate schon zusammenführt). Test in Task 1.
- Satz der geschlossenen (oder fremden) Session per PATCH/DELETE → 409/404, nichts geändert. Test in Task 1/2.
- Session per Timeout abgelaufen, dann neuer Satz → neue Session, kein Fehler. Test in Task 1.
- Gym-Server-Bind schlägt fehl (IP noch nicht da / Port belegt) → Mantis läuft weiter, Retry mit Backoff, Log-Warnung. Test in Task 4.

---

### Task 1: Schema `client_id` + Domain-Fassade `domains/gym/tracker.py`

**Files:**
- Modify: `core/db.py` (idempotente Migration neben den bestehenden `workout_sets`-DDL, ~Zeile 208)
- Create: `domains/gym/tracker.py`
- Test: `tests/test_gym_tracker.py`

**Interfaces:**
- Consumes: `domains.gym.session.open_session(now=None) -> dict|None`, `finish(now=None) -> dict|None`, `exercise_history(name, limit) -> list[{workout_id,date,created_at,sets:[row]}]`, `add_sets(parsed, allow_create=False, now=None)`, `domains.gym.parser.ParsedLog/ParsedSet`, `core.db.cursor()/query/query_one`.
- Produces (alle werfen `TrackerError(status:int, message:str)` bei Fehlern):
  - `EXERCISES: tuple[str, ...] = ("Squats", "Bench Press", "Deadlifts")`
  - `REST_SECONDS: dict[str,int] = {"Squats": 180, "Bench Press": 180, "Deadlifts": 180}`
  - `get_state(now=None) -> dict` → `{"session": {"id","date","started_at"}|None, "exercises": [{"name","rest_s","prefill":[{"weight_kg","reps"}],"logged":[{"id","weight_kg","reps","set_index"}]}]}`
  - `log_set(client_id:str, exercise:str, weight_kg:float, reps:int, now=None) -> dict` → `{"id","exercise","weight_kg","reps","set_index","workout_id","duplicate":bool}`
  - `update_set(set_id:int, weight_kg:float|None, reps:int|None, now=None) -> dict`
  - `delete_set(set_id:int, now=None) -> None`
  - `finish_session(now=None) -> dict|None` → `{"workout_id","duration_min","sets","volume_kg"}`
  - `history(exercise:str, limit:int=5) -> list[{"date":"YYYY-MM-DD","sets":[{"weight_kg","reps"}]}]`

- [ ] **Step 1: Migration.** In `core/db.py` nach der `workout_sets`-DDL ergänzen:
```sql
ALTER TABLE workout_sets ADD COLUMN IF NOT EXISTS client_id text;
CREATE UNIQUE INDEX IF NOT EXISTS workout_sets_client_id_idx ON workout_sets (client_id) WHERE client_id IS NOT NULL;
```
- [ ] **Step 2: Failing tests** in `tests/test_gym_tracker.py` (Fixture legt Übungen `Squats`/`Bench Press`/`Deadlifts` und ein synthetisches altes Workout an, räumt nach dem Test auf; Muster aus `tests/test_gym_session.py` übernehmen):
```python
def test_prefill_uses_last_session_working_sets(old_bench):  # alt: 1 Warmup 40x10, 80x8, 80x7
    st = tracker.get_state(now=NOW)
    bench = next(e for e in st["exercises"] if e["name"] == "Bench Press")
    assert bench["prefill"] == [{"weight_kg": 80.0, "reps": 8}, {"weight_kg": 80.0, "reps": 7}]
    assert [e["name"] for e in st["exercises"]] == ["Squats", "Bench Press", "Deadlifts"]

def test_prefill_empty_without_history():
    sq = next(e for e in tracker.get_state(now=NOW)["exercises"] if e["name"] == "Squats")
    assert sq["prefill"] == [] and sq["logged"] == []

def test_log_set_opens_session_and_is_idempotent():
    a = tracker.log_set("c1", "Bench Press", 82.5, 8, now=NOW)
    b = tracker.log_set("c1", "Bench Press", 82.5, 8, now=NOW)
    assert a["id"] == b["id"] and b["duplicate"] is True
    assert db.query_one("SELECT count(*) AS n FROM workout_sets WHERE client_id='c1'")["n"] == 1
    assert tracker.get_state(now=NOW)["session"]["id"] == a["workout_id"]

def test_log_set_rejects_unknown_exercise_and_bad_values():
    for args in [("x","Curl",20,10), ("y","Squats",0,5), ("z","Squats",600,5), ("w","Squats",100,0), ("v","Squats",100,101)]:
        with pytest.raises(tracker.TrackerError) as e: tracker.log_set(*args, now=NOW)
        assert e.value.status == 400

def test_update_and_delete_only_in_open_session():
    s = tracker.log_set("c2", "Squats", 100, 5, now=NOW)
    assert tracker.update_set(s["id"], 102.5, None, now=NOW)["weight_kg"] == 102.5
    tracker.finish_session(now=NOW)
    with pytest.raises(tracker.TrackerError) as e: tracker.delete_set(s["id"], now=NOW)
    assert e.value.status == 409
    with pytest.raises(tracker.TrackerError) as e: tracker.delete_set(999999999, now=NOW)
    assert e.value.status == 404

def test_timeout_opens_new_session():
    a = tracker.log_set("c3", "Squats", 100, 5, now=NOW)
    b = tracker.log_set("c4", "Squats", 100, 5, now=NOW + timedelta(hours=6))
    assert a["workout_id"] != b["workout_id"]

def test_finish_summary_and_history():
    tracker.log_set("c5", "Deadlifts", 140, 5, now=NOW); tracker.log_set("c6", "Deadlifts", 140, 4, now=NOW)
    s = tracker.finish_session(now=NOW + timedelta(minutes=30))
    assert s["sets"] == 2 and s["volume_kg"] == 1260.0
    h = tracker.history("Deadlifts", limit=5)
    assert h[0]["sets"] == [{"weight_kg": 140.0, "reps": 5}, {"weight_kg": 140.0, "reps": 4}]
```
(Session-Timeout aus `domains/gym/session.session_timeout_end` nachsehen und `NOW + timedelta(...)` so wählen, dass er sicher überschritten ist.)
- [ ] **Step 3:** `python3.14 -m pytest tests/test_gym_tracker.py -q` → FAIL (Modul fehlt).
- [ ] **Step 4: Implementieren.** `log_set`: validieren; in einer Transaktion zuerst `SELECT … FROM workout_sets WHERE client_id=%s` (→ `duplicate=True` zurück); sonst `session.add_sets(ParsedLog(exercise=name, sets=(ParsedSet(weight_kg, reps),), known=True), now=now)` aufrufen und den neu eingefügten Satz (höchster `set_index` dieser Übung im Workout) per `UPDATE … SET client_id=%s` markieren. Race zweier gleichzeitiger Requests: `UniqueViolation` abfangen, den eingefügten Satz der Transaktion zurückrollen und den bestehenden zurückgeben (falls `add_sets` eigene Transaktion nutzt, stattdessen eine Variante `add_sets(..., client_id=None)` in `session.py` ergänzen, die `client_id` direkt im INSERT setzt – bevorzugt). `get_state`: `prefill` = Sätze von `exercise_history(name, 2)` – die erste Session, die NICHT die offene ist; `logged` aus der offenen Session. `update_set/delete_set`: Satz per JOIN auf `workouts` laden; nicht gefunden → 404; Workout nicht offen (`ended_at` gesetzt oder ≠ `open_session()`) → 409; danach `last_activity_at` aktualisieren. `finish_session`: `session.finish(now)`, Summary aus den Sätzen berechnen (`volume_kg = Σ weight*reps`, Arbeitssätze). Gewichte auf 0,25 runden.
- [ ] **Step 5:** Tests grün, `ruff` grün.
- [ ] **Step 6: Commit** (macht Claude): `feat(gym): Tracker-Fassade mit idempotentem Satz-Logging`

### Task 2: Gym-App `web/gym_app.py` (Auth + API + Static)

**Files:**
- Modify: `settings.py` (+ `config.py` falls dort gespiegelt): `GYM_ENABLED: bool = True`, `GYM_HOST: str = "127.0.0.1"`, `GYM_PORT: int = 7782`, `GYM_SESSION_DAYS: int = 180`
- Create: `web/gym_app.py`
- Test: `tests/test_gym_app.py`

**Interfaces:**
- Consumes: alles aus Task 1 (`domains.gym.tracker`).
- Produces: `create_gym_app() -> FastAPI`; Routen: `GET /` (index.html), `GET /app.js`, `GET /app.css`, `GET /manifest.webmanifest`, `GET /icon.png` (öffentlich, `Cache-Control: no-cache`), `POST /auth/session` `{token}` → setzt Cookie, `DELETE /auth/session`, `GET /api/state`, `POST /api/sets` `{client_id, exercise, weight_kg, reps}`, `PATCH /api/sets/{id}` `{weight_kg?, reps?}`, `DELETE /api/sets/{id}`, `POST /api/finish`, `GET /api/history?exercise=&limit=`. `TrackerError` → JSON `{"error": msg}` mit `status`. Blocking-DB-Aufrufe via `await asyncio.to_thread(...)`.

- [ ] **Step 1: Failing tests** (`fastapi.testclient.TestClient`, `monkeypatch.setattr(config, "DASHBOARD_TOKEN", "t")`, tracker-Funktionen per monkeypatch gestubbt – DB-Verhalten ist in Task 1 getestet):
```python
def test_shell_public_api_protected(client):
    assert client.get("/").status_code == 200 and "text/html" in client.get("/").headers["content-type"]
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/api/state").status_code == 401

def test_login_wrong_token_401_and_right_token_sets_cookie(client):
    assert client.post("/auth/session", json={"token": "nope"}).status_code == 401
    r = client.post("/auth/session", json={"token": "t"})
    assert r.status_code == 200 and "mantis_gym" in r.cookies
    assert "Max-Age=15552000" in r.headers["set-cookie"] and "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/api/state").status_code == 200

def test_no_server_token_503(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "")
    assert client.post("/auth/session", json={"token": ""}).status_code == 503

def test_foreign_origin_rejected_on_write(logged_in):
    r = logged_in.post("/api/sets", json={...}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403

def test_tracker_error_maps_to_status(logged_in, monkeypatch):
    monkeypatch.setattr(tracker, "delete_set", raising(tracker.TrackerError(409, "geschlossen")))
    assert logged_in.delete("/api/sets/5").status_code == 409

def test_bearer_header_also_accepted(client):
    assert client.get("/api/state", headers={"Authorization": "Bearer t"}).status_code == 200
```
- [ ] **Step 2:** FAIL bestätigen.
- [ ] **Step 3: Implementieren.** Session-Wert wie `web/auth.session_value`, aber eigener HMAC-Kontext `"gym-session:" + expiry`, Laufzeit `GYM_SESSION_DAYS*86400`. Middleware/Dependency: `/api/*` braucht gültiges Cookie oder Bearer; bei Methoden ≠ GET ist ein vorhandener `Origin`-Header nur erlaubt, wenn er der eigenen Origin entspricht. Static aus `Path(__file__).parent / "gym"` (nur die fünf Dateinamen, kein Directory-Listing). Für Task 2 genügt ein Platzhalter-`web/gym/index.html` + Manifest; Task 3 ersetzt sie.
- [ ] **Step 4:** Tests + ruff grün. **Commit:** `feat(gym): eigene Web-App mit Cookie-Session und API`

### Task 3: Frontend `web/gym/`

**Files:**
- Create/Replace: `web/gym/index.html`, `web/gym/app.css`, `web/gym/app.js`, `web/gym/manifest.webmanifest`, `web/gym/icon.png` (180×180, schlichtes Hantel-Icon, per Python/Pillow generiert oder einfaches PNG)
- Test: `tests/test_gym_frontend.py` (strukturell) + `node --check web/gym/app.js`

**Interfaces:** Consumes die API aus Task 2. Keine Produces.

Verhalten (Spec „Umfang v1“):
- Ohne Session (401 von `/api/state`): Token-Feld + Button → `POST /auth/session`, dann neu laden.
- Startansicht: Kopf mit Session-Status („Training läuft seit hh:mm“ / „Kein Training offen“), Liste der drei Übungen (Name, n/m Sätze erledigt). Tippen öffnet Übung.
- Übungsansicht: Zeilen aus `logged` (abgehakt, grün) + restliche `prefill`-Zeilen (offen). Je Zeile: Gewicht mit −/+ (2,5 kg), Wdh. mit −/+ (1), Zahlen direkt editierbar (`inputmode="decimal"` / `"numeric"`), großer ✓-Button (≥ 48 px). Abgehakte Zeile antippen → Bearbeiten (PATCH) oder Löschen (DELETE, mit Bestätigung). „+ Satz“ kopiert die letzte Zeile. Darunter „Letzte Trainings“ aus `/api/history` (Datum + `80×8 · 80×7`).
- ✓: `client_id = crypto.randomUUID()`, Satz sofort als abgehakt anzeigen, in `localStorage["gym_queue"]` legen, `flushQueue()` senden. `flushQueue()` sendet sequentiell, entfernt bei 2xx (auch `duplicate`), behält bei Netzwerkfehler/5xx, verwirft bei 4xx mit sichtbarer Fehlermeldung. Aufruf bei ✓, `online`-Event, `visibilitychange` → visible und beim Start. Banner „n Sätze nicht synchronisiert“, solange Queue nicht leer. Alle `localStorage`-Zugriffe in try/catch.
- Pausentimer: nach ✓ startet `rest_s` der Übung; Endzeit in `localStorage["gym_timer_end"]`, Anzeige oben fixiert (mm:ss), Buttons −30/+30/Überspringen; bei 0: `navigator.vibrate?.([300,150,300])` + kurzer Ton über WebAudio (Oszillator, 0,3 s). Nach Reload aus der gespeicherten Endzeit fortsetzen.
- „Training beenden“ (mit Bestätigung) → `POST /api/finish` → Zusammenfassung (Dauer, Sätze, Volumen).
- Design: dunkel, System-Schrift, mobile first, max-width 520 px zentriert, `env(safe-area-inset-*)` beachten, `<meta name="apple-mobile-web-app-capable" content="yes">`, `apple-touch-icon`, `theme-color`. Texte deutsch. Dezimalanzeige mit Komma.

- [ ] **Step 1: Failing strukturelle Tests** (`tests/test_gym_frontend.py`): index.html verlinkt `app.js`, `app.css`, `manifest.webmanifest`, enthält `apple-mobile-web-app-capable`; Manifest-JSON hat `"display": "standalone"` und `"start_url": "/"`; `app.js` enthält `randomUUID`, `gym_queue`, `/api/sets`, `vibrate`.
- [ ] **Step 2:** Implementieren, `node --check web/gym/app.js`, Tests grün.
- [ ] **Step 3: Commit:** `feat(gym): Tracking-Oberfläche mit Offline-Queue und Pausentimer`

### Task 4: Start im Mantis-Prozess

**Files:**
- Modify: `main.py` (nach dem Dashboard-Server, ~Zeile 160–180)
- Create: `web/gym_server.py`
- Test: `tests/test_gym_server.py`

**Interfaces:**
- Consumes: `create_gym_app()`, `main.validate_dashboard_host` (Logik nach `web/gym_server.py` als `validate_host(host)` duplizieren oder gemeinsam auslagern; `main.validate_dashboard_host` muss weiter existieren).
- Produces: `async def run_gym_server(host:str, port:int, *, attempts:int|None=None, base_delay:float=5.0, max_delay:float=300.0) -> None` – validiert den Host (ValueError → `log.error`, return), startet `uvicorn.Server(...).serve()`; endet `serve()` ohne `should_exit` (Bind-Fehler/`SystemExit`/`OSError`) → `log.warning` und erneuter Versuch mit exponentiellem Backoff; `attempts` begrenzt nur für Tests.

- [ ] **Step 1: Failing tests:** ungültiger Host (`0.0.0.0`, `192.168.1.5`) → kein Start, kein Raise; Bind-Fehler (Fake-Server, dessen `serve()` `SystemExit(1)` bzw. `OSError` wirft) → genau `attempts` Versuche, kein Raise; Erfolg im zweiten Versuch → stoppt. Uvicorn per monkeypatch ersetzen, `asyncio.sleep` patchen.
- [ ] **Step 2:** Implementieren. In `main.py`: `if cfg.GYM_ENABLED: asyncio.create_task(run_gym_server(cfg.GYM_HOST, cfg.GYM_PORT))` + INFO-Log `🏋️ Gym-App: http://{host}:{port}`. Hinweis: uvicorn ruft bei Bind-Fehler `sys.exit(1)` → `SystemExit` muss im Task gefangen werden, damit der Prozess nicht endet.
- [ ] **Step 3:** Tests + gesamte Suite + ruff grün. **Commit:** `feat(gym): Gym-App startet mit Mantis auf eigenem Port`

### Task 5: Inbetriebnahme (Claude)
- [ ] Merge nach `main` erst nach Timos Zustimmung; danach `.env`: `GYM_HOST=100.107.172.123`.
- [ ] Neustart `launchctl kickstart -k gui/$(id -u)/com.mantis.assistant`, Log prüfen, `curl -s http://100.107.172.123:7782/` → 200, `/api/state` → 401.
- [ ] Browser-Smoke-Test gegen eine Preview-Instanz mit `mantis_test` (nicht gegen Produktivdaten): Login, Satz abhaken, Timer, Netzfehler simulieren (Queue bleibt, Banner), Reload, Sync. Mobile-Viewport-Screenshot an Timo.
- [ ] Handoff-Log und Brain-Notiz aktualisieren.

## Abweichung von der Spec
Nutzungsmessung: Web-Sätze laufen nicht über `chat_messages`; für die Auswertung am 21.10. werden Gym-Tage direkt aus `workouts`/`workout_sets` (Sätze mit `client_id`) gezählt. Kein eigener Zähler in v1.
