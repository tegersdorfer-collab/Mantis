# COROS MCP als primäre Health-Quelle

**Datum:** 2026-08-15
**Status:** Design abgenommen, Plan folgt

## Ziel

Gesundheitsdaten kommen künftig direkt aus der COROS-Cloud über deren offizielles
MCP statt über die Kette `COROS-Uhr → COROS-App → Apple Health → BodyOS-Push`.
Der alte Push-Pfad wird abgerissen.

## Warum

Der bestehende Pfad hat vier Glieder und ist nachweislich lückenhaft: laut
[health-overview-scores-design.md](2026-07-13-health-overview-scores-design.md)
liefern HRV, Ruhepuls und Schlafphasen nur an 3 von 14 Tagen Werte. Ursache ist die
Kombination aus HealthKit-Background-Delivery und COROS→Apple-Health-Freigaben — ein
seit Wochen offener ROADMAP-Punkt. Dazu nutzt Timo die iOS-Apps nicht mehr, womit der
Push-Pfad ohnehin tot ist.

Drei konkrete Gewinne:

1. **Ein Glied statt vier.** Kein iPhone in der Kette, keine Background-Delivery-Lotterie.
2. **Lücken sind nachholbar.** Die COROS-Tools nehmen Datumsbereiche entgegen; fehlende
   Tage lassen sich rückwirkend füllen. Der Push-Pfad konnte das prinzipiell nicht.
3. **Neue Daten.** Training Load, Recovery-Status, COROS' Sleep Score, Stress und echter
   VO₂max — alles Dinge, die über HealthKit nie ankamen.

## Verifizierte Vorbedingungen

Am 2026-08-15 live gegen `https://mcpeu.coros.com` geprüft:

| Prüfung | Ergebnis |
|---|---|
| `/.well-known/oauth-authorization-server` | 200, vollständige Metadaten |
| `registration_endpoint` | `/connect/register` — **Dynamic Client Registration offen** |
| Test-`POST /connect/register` | 201, `client_id` ausgestellt, keine Whitelist |
| `scopes_supported` | `openid`, `mcp.tools`, `offline_access` (→ Refresh-Token) |
| `token_endpoint_auth_methods_supported` | enthält `none` (→ Public Client + PKCE reicht) |
| `POST /mcp` ohne Token | 401 mit korrektem `WWW-Authenticate`-Header |

Ein selbstgebauter Client ist damit zulässig, obwohl die COROS-Doku nur
"verifizierte Plattformen" (ChatGPT, Claude, …) nennt.

## Nachtrag 2026-08-16 — was Phase A widerlegt hat

Phase A hat den Zugang gebaut und die echten Antworten geholt. Vier Annahmen dieses
Dokuments waren falsch. Sie sind unten im Text korrigiert; hier steht, was sich geändert hat
und warum, damit Phase B nicht gegen die alte Fassung gebaut wird.

1. **Es gibt kein JSON.** Kein einziges der 22 Tools deklariert ein `outputSchema` oder
   liefert `structuredContent`. Jede Antwort ist ein Textblock in einem `content`-Feld —
   LLM-erzeugte Prosa mit fester Zeilenstruktur:

   ```
   --- 20260811 ---
   Steps: 12,500 | Calories: 1,200 kcal | Exercise: 25 min
   Stress: Avg 50
   Sleep Summary:
     Total: 6h 0min | Deep: 1h 0min | Light: 3h 30min | REM: 1h 15min | Awake: 15 min
   ```

   `mapping.py` ist damit ein **Textparser**, kein Feldmapping. Das ist der größte
   Unterschied zur ursprünglichen Planung und der Grund, warum Phase B strengere
   Fehlerregeln braucht als vorgesehen (siehe "Fehlerverhalten").

2. **Das MCP ist serverseitig LLM-gestützt.** Belegt durch eine Fehlermeldung des Servers:
   "Tool call anomalies detected. High risk of session context pollution or request exceeds
   the LLM capability boundary. Resolution Strategy: … 2. Upgrade or switch to a
   high-capacity model instance." Formatstabilität ist folglich nicht zugesichert. Der
   Parser muss Formatabweichungen als Fehler behandeln, nicht als fehlende Werte.

3. **Fehler kommen teilweise mit `isError: false`.** `querySleepData` und
   `queryTrainingSchedule` lieferten die "anomalies"-Meldung als *erfolgreiches* Ergebnis.
   Das `isError`-Flag allein genügt nicht; es braucht eine inhaltliche Prüfung.

4. **Die Historie ist klein und passt in einen Aufruf.** Timos Daten beginnen am
   **2026-03-05**; im Zeitraum bis 2026-08-16 gibt es **94 Tage mit Daten** (71 Kalendertage
   ohne) und **21 Aktivitäten insgesamt**. `queryDailyHealthData` mit `days=1000` liefert
   die komplette Historie in 18 KB. Der geplante Backfill mit 30-Tage-Fenstern, Wasserstand
   in der `settings`-Tabelle und Wiederaufsetzen nach Abbruch ist damit **überflüssig** —
   ein Aufruf genügt. Der Abschnitt "Sync-Verhalten" ist entsprechend vereinfacht.

Außerdem beobachtet, ohne Designfolge: das Datumsformat der Tools ist `yyyyMMdd`, nicht ISO;
mehrere Tagestools nehmen statt eines Datumsbereichs nur einen Rückblick-Zähler `days`; und
COROS liefert vereinzelt kaputte Zeitstempel (ein `Nap Window` im Jahr 1982). Der Parser
darf an solchen Werten nicht ersticken und sie nicht übernehmen.

## Ist-Zustand

```
idle_loop._tick_health (alle 1800 s) ─┐
orchestrator.py:240 (Start) ──────────┴→ dashboard.refresh_health()
                                            └→ health.import_from_icloud()
                                                 └→ HTTP-GET auf HEALTH_API_URL (BodyOS)
POST /api/health/push (BodyOS) ────────────→ health.process_health_data()
                                                 └→ UPSERT health_data
```

Alles darüber — `health_scores`, `_health_dict`, das `get_health`-Tool, PWA und
Desktop-Overlay — kennt ausschließlich die Tabelle `health_data`.

## Soll-Architektur

### Die Naht

Genau eine Funktion wechselt die Quelle:

```
idle_loop._tick_health ─┐
orchestrator.py:240 ────┴→ dashboard.refresh_health() → coros.importer.sync()
```

`idle_loop.py`, `orchestrator.py`, `health_scores.py`, `_helpers._health_dict`,
`core/skills/health.py` und die gesamte PWA bleiben **unverändert**. Das ist der
gesamte Grund für diesen Zuschnitt: der Umbau endet an der Tabellengrenze.

### Module

| Datei | Aufgabe | Abhängt von |
|---|---|---|
| `domains/coros/oauth.py` | DCR, PKCE-Flow, Token laden/refreshen | httpx |
| `domains/coros/client.py` | MCP-Session, `call_tool(name, args) -> dict` | oauth |
| `domains/coros/mapping.py` | COROS-**Prosa** → `health_data`-Spalten, **reine Funktionen** | — |
| `domains/coros/importer.py` | Tages-Sync, Backfill, Workout-Import | client, mapping, health, fitness |
| `scripts/coros_auth.py` | einmaliger Browser-Login durch Timo | oauth |

Der Zuschnitt spiegelt `gcal`/`gmail`: eigenes `scripts/*_auth.py`, eigenes Token-File
unter `data/` (gitignored), Callback auf einem eigenen Port.

`mapping.py` ist bewusst I/O-frei — dieselbe Entscheidung wie bei `health_scores.py`,
und aus demselben Grund: so ist der Teil, der am ehesten falsch ist, direkt testbar.

### OAuth

- **Flow:** Authorization Code + PKCE (S256), Public Client (`token_endpoint_auth_method: none`).
- **Registrierung:** einmalig per DCR gegen `/connect/register`, `client_id` wandert ins Token-File.
- **Scopes:** `openid mcp.tools offline_access`.
- **Redirect:** `http://127.0.0.1:8083/callback` (8081/8082 sind gcal/gmail).
- **Token-File:** `data/coros_token.json` — `client_id`, `access_token`, `refresh_token`, `expires_at`, `issuer`.
- **Refresh:** vor jedem Aufruf, wenn `expires_at` < 60 s entfernt. Kein Hintergrund-Timer.
- **Login macht Timo.** Das Skript druckt die URL und wartet auf den Callback; Claude gibt
  keine Zugangsdaten ein.

### MCP-Transport

Handgeschriebener Client auf dem vorhandenen `httpx`, **keine neue Dependency**.

Ursprünglich war das offizielle `mcp`-SDK vorgesehen. Bei der Planung geprüft: `mcp` 2.0.0
zieht `httpx2>=2.5.0`, `starlette`, `uvicorn`, `sse-starlette`, `opentelemetry-api`,
`pyjwt[crypto]` und `python-multipart` nach — ein zweiter kompletter HTTP-Stack neben dem
`httpx`, das Mantis schon nutzt, für einen reinen Client-Anwendungsfall. Das ist für 16 GB
RAM und "einfacher Code > Abstraktion" die falsche Richtung.

Gebraucht werden drei Nachrichten: `initialize`, `notifications/initialized`, `tools/call`,
als JSON-RPC über POST, Antwort wahlweise als JSON oder SSE-Frame. Rund 150 Zeilen. Die
Schnittstelle nach außen (`call_tool`) ist dieselbe, die hier getroffene Entscheidung also
jederzeit umkehrbar.

### Genutzte COROS-Tools

| Tool | Liefert | Ziel |
|---|---|---|
| `queryDailyHealthData` | Schritte, Kalorien, Stress, Schlaf-Summary, HF | `steps`, `active_calories`, `stress_avg`, `hr_avg` |
| `querySleepData` | Score, Dauer, Phasen, Wachzeit | `sleep_*`, `sleep_score` |
| `querySleepHrv` | HRV | `hrv` |
| `queryRestingHeartRate` | Ruhepuls | `resting_hr` |
| `queryRecoveryStatus` | Erholung in % | `recovery_pct` |
| `queryTrainingLoadAssessment` | Kurz-/Langzeit-Load | `training_load_short/long` |
| `queryFitnessAssessmentOverview` | VO₂max, Race Predictions | `vo2max` |
| `querySportRecords` | Aktivitätsliste | `fitness.log_workout` |
| `queryUserInfo` | Profil inkl. Gewicht | `weight` |

Die meisten dieser Tools nehmen Datumsbereiche — ein Backfill-Fenster kostet also einen
Aufruf pro Tool, nicht einen pro Tag.

`downloadActivityFitFiles` wird **nicht** genutzt (Limit 50/Tag, kein Bedarf in v1).

### Neue Spalten

Per `MIGRATIONS`-Liste in `core/db.py`, Idiom `ALTER TABLE … ADD COLUMN IF NOT EXISTS`:

```sql
ALTER TABLE health_data ADD COLUMN IF NOT EXISTS training_load_short DOUBLE PRECISION;
ALTER TABLE health_data ADD COLUMN IF NOT EXISTS training_load_long  DOUBLE PRECISION;
ALTER TABLE health_data ADD COLUMN IF NOT EXISTS recovery_pct        DOUBLE PRECISION;
ALTER TABLE health_data ADD COLUMN IF NOT EXISTS sleep_score         INT;
ALTER TABLE health_data ADD COLUMN IF NOT EXISTS stress_avg          DOUBLE PRECISION;
```

Diese Spalten werden in v1 nur befüllt, nicht angezeigt oder gescort. Die Scoring-Gewichte
anzupassen ist eine eigene Entscheidung mit eigener Datengrundlage und gehört nicht in
denselben Umbau.

### Sync-Verhalten

*Vereinfacht nach dem Nachtrag: die Historie passt in einen Aufruf, ein gefensterter
Backfill mit Wasserstand ist unnötig.*

- **`sync(days: int = 14)`** — eine Funktion für beides. Holt die letzten `days` Tage und
  upsertet sie. Vierzehn statt drei im Regelbetrieb, weil die Uhr verzögert synchronisiert,
  COROS Werte nachträglich korrigiert und der Aufruf bei 94 Tagen Gesamthistorie ohnehin
  billig ist. `ON CONFLICT DO UPDATE` macht das idempotent.
- **Erst-Import**: derselbe Aufruf mit `days=365` — `MAX_LOOKBACK_DAYS` deckelt jeden
  größeren Wert ohnehin, weil manche Tools jenseits davon mit einem Fehlertext statt
  Daten antworten. Deckt die komplette Historie (94 Tage) reichlich ab, in einem
  Rutsch. Kein Wasserstand, kein Fenster, kein Wiederaufsetzen — ein fehlgeschlagener
  Lauf wird einfach wiederholt.
- Angestoßen wird der Erst-Import einmalig von Hand; danach übernimmt der 1800-s-Tick.

## Abriss

| Weg | Datei |
|---|---|
| `POST /api/health/push` | `web/routers/health.py` |
| `import_health()`, `import_from_icloud()` | `domains/health.py` |
| `map_health_fields()` — reines HealthKit-Mapping | `domains/health.py` |
| `process_health_data()` samt HealthKit-Workout-Import und `_last_updated`-Dedup | `domains/health.py` |
| `HEALTH_API_URL` | `config.py`, `settings.py` |
| HealthKit-Mapping-Tests | `tests/test_health_mapping.py` |

Nach dem Abriss hat `process_health_data()` keinen Aufrufer mehr — Push und Poll sind beide
weg. An seine Stelle tritt `health.upsert_day(day, fields)`: dieselbe
`INSERT … ON CONFLICT DO UPDATE`-Logik, aber ohne Format-Wissen. Wer die Felder erzeugt,
ist damit nicht mehr die Sache der Health-Domäne. Die Dedup über `_last_updated` entfällt
ersatzlos; Idempotenz kommt vom Upsert selbst.

`domains/health.py` behält `upsert_day()`, `recent()`, `latest()`, `history()`.

**Bleibt:**

- `POST /api/health/manual` (HRV-Handeingabe im Dashboard) und `/api/body/measurements`.
- `POST /api/health/import` samt Sync-Button in der PWA; zeigt danach auf COROS.
- Die iOS-Apps unter `apps/`. Sie laufen nicht und stören niemanden; ihr Quelltext bleibt
  liegen. Der Server nimmt ihre Pushes nur nicht mehr an.
- **Die toten Spalten** `calories`, `protein`, `carbs`, `fat`, `water`, `body_fat`, `bmi`,
  `body_temp`. Sie zu droppen bringt nichts und würde die historischen Werte aus der Zeit
  vernichten, als der Sync noch lief. Nur der Schreibpfad verschwindet.

**Ausdrücklich nicht angetastet:** die Ernährungs-Ansicht der PWA und die Body-/Gewichts-
Kachel. Die Ernährung läuft über `/api/nutrition` und eine eigene `meals`-Tabelle, hängt
also nie am HealthKit-Pfad. Gewicht kommt weiter aus `queryUserInfo`, `/api/health/manual`
und `/api/body/measurements`. Beides funktioniert nach dem Umbau unverändert.

## Fehlerverhalten

Leitlinie ist das Ventilator-Muster: freundlich degradieren statt crashen.

| Fall | Verhalten |
|---|---|
| Kein Token-File | `sync()` gibt 0 zurück, loggt einmal "COROS nicht autorisiert — `python3 scripts/coros_auth.py`" |
| Netz weg / MCP 5xx | Warnung ins Log, bestehende Daten bleiben, nächster Tick versucht erneut |
| Refresh-Token abgelaufen | Eine Telegram-Nachricht "COROS neu autorisieren", danach stumm bis zur Reparatur |
| Einzelnes Tool schlägt fehl | Die anderen Felder des Tages werden trotzdem geschrieben — Teildaten sind besser als keine |
| Antwort mit `isError: false`, aber Fehlertext im Inhalt | Als Fehler behandeln. Der Parser prüft den Text auf die bekannten Server-Fehlermuster, bevor er ihn zu parsen versucht |
| Unbekanntes Antwortformat | Feld wird übersprungen und einmal pro Prozess geloggt, nie geraten |
| Wert außerhalb plausibler Grenzen | Verworfen, nicht geschrieben. Bei Prosa ist eine falsch geparste Zahl schlimmer als eine fehlende — sie sieht aus wie eine Messung |
| Ein ganzer Tagesblock unparsebar | Der Tag wird übersprungen, die übrigen Tage der Antwort werden geschrieben |

## Testing

- `mapping.py`: Unit-Tests gegen **aufgezeichnete echte Antworten** aus Phase A
  (`tests/fixtures/coros/*.json`). TDD — Test vor Mapping-Code.
- `importer.py`: gegen einen Fake-Client, der die Fixtures ausliefert. Geprüft werden
  Upsert-Idempotenz, Backfill-Wasserstand, Abbruchbedingung, Teilausfall eines Tools.
- `oauth.py`: PKCE-Erzeugung und Refresh-Entscheidung als reine Funktionen; der
  Browser-Flow selbst wird nicht automatisiert getestet.
- Live-Smoke-Test nach der Autorisierung: ein Tag synchronisieren, Zeile in `health_data`
  prüfen, `/api/health/scores` aufrufen.

## Bauabschnitte

Der Zuschnitt folgt einem echten Nichtwissen: **die JSON-Feldnamen der COROS-Tools sind
nirgends dokumentiert.** `mapping.py` lässt sich nicht blind schreiben.

- **A — Erkennen.** `oauth.py` + `client.py`, Timo autorisiert einmal, danach dumpt ein
  Wegwerf-Skript je Tool eine Roh-Antwort nach `data/coros_samples/`. Ergebnis: die
  Fixtures, gegen die Phase B entwickelt.
- **B — Bauen.** `mapping.py` per TDD gegen die Fixtures, `importer.py` gegen den
  Fake-Client, Migration, dann die Naht in `dashboard.refresh_health()` umlegen.
- **C — Abreißen.** Die Tabelle unter "Abriss" abarbeiten, Tests grün, Live-Check.

## Risiken

| Risiko | Umgang |
|---|---|
| **Das Antwortformat ist LLM-erzeugte Prosa und kann sich ändern** | Das größte Risiko dieses Entwurfs. Gegenmittel ist ausschließlich Strenge: jeder Parser erwartet ein exaktes Muster, verwirft implausible Werte und meldet Formatabweichungen, statt still etwas Falsches zu schreiben. Ein fehlender Wert degradiert sichtbar (die Scores sagen "zu wenig Daten"); ein falsch geparster Wert sieht aus wie eine Messung und vergiftet die Baselines. |
| COROS schließt DCR oder ändert Tool-Namen | Der Importer degradiert freundlich; die Tabelle behält alles Bisherige. Wiederherstellung wäre ein neuer Ingest, kein Datenverlust. |
| Antwortformate weichen von der Doku ab | Genau dafür Phase A. Kein Mapping ohne echte Antwort. |
| Cloud-Abhängigkeit widerspricht "lokal first" | Bewusst akzeptiert: die Daten liegen ohnehin in der COROS-Cloud, sobald die Uhr synchronisiert. Neu ist der Abruf, nicht der Abfluss. |
| `mcp`-SDK bricht auf Python 3.14 | Rückfall auf ~120 Zeilen httpx, Schnittstelle bleibt gleich. |

## Nicht im Scope

- FIT-Datei-Download und Sekunden-Zeitreihen.
- Schreibender Zugriff (Trainingspläne zurück in den COROS-Kalender) — das MCP ist read-only.
- Anpassung der Scoring-Gewichte an die neuen Metriken.
- Die COROS-Tools als Agent-Tools verfügbar machen ("wie war mein Lauf am Dienstag").
  Naheliegender nächster Schritt, aber eine eigene Änderung.
