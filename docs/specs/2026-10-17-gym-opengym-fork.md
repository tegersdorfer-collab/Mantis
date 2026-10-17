# Spec: Gym-App auf openGym-Frontend umstellen

Stand 2026-10-17. Entscheidung Timo: Optik und Stack zählen, Funktionen passt KI an. Nach Vergleich
(LiftTrace, Skulpt, wger, Liftosaur, LiftLog) gefällt ihm **openGym** (AGPL-3.0, v1.4.1 vom
10.10.2026, Quelle `https://gitea.com/DuarteSantos/openGym`, lokale Kopie `~/opengym-src`).
Einwand: „Übungen sind mir zu viele“ (1324 im Katalog) → nur eigene Übungen.
Ersetzt die AP-Kopie (`2026-10-15-gym-ap-look.md`), Backend und Daten bleiben.

## Architektur (Option A: Sync-Vertrag nachbauen)
openGym ist local-first: Der Zustand-Store hält den gesamten Zustand `S` (localStorage), der Server
gleicht ihn per Snapshot ab. Das React-Frontend bleibt so unverändert wie möglich; FastAPI
implementiert den Vertrag und bildet ihn auf Postgres ab.

- `GET /api/data` → `{state, rev}`: `S` aus Postgres erzeugen (workouts mit entries/sets,
  routines aus `gym_plans`/`gym_plan_days`/`gym_plan_exercises`, bodyweight aus Mantis-Messwerten,
  customEx = **unsere Übungen**, Settings).
- `GET /api/data/rev` → `{rev, wid}`: Revisionszähler in Postgres; jede Gym-Schreiboperation
  (auch Mantis/Sprache/alte Endpunkte) erhöht ihn → Frontend pollt (30 s) und lädt nach.
- `PUT /api/data` `{state, baseRev, baseWid}` → Diff gegen DB, Upserts/Deletes in einer
  Transaktion; bei `baseRev` ≠ aktueller Rev HTTP 409 `{error:'conflict', rev, state}` (Frontend
  merged selbst, `lib/sync-merge.js`). Tombstones in `S.deleted` → Löschen.
- `GET /api/me`, `GET /api/config`, `GET /api/health`: minimal (ein Nutzer, keine Registrierung,
  kein Coach, keine Medien, kein Push). Nicht unterstützte Endpunkte → 404/`{ok:false}` ohne
  Frontend-Absturz.
- ID-Abbildung: openGym-IDs sind Strings (`w-…`, `r-…`). Neue Spalten `client_id text unique`
  an workouts / gym_plan_days o. Ä.; vorhandene Datensätze bekommen stabile IDs `w-<id>`, `r-<id>`.
  Übungen: `m-<exercise_id>`. Sätze werden je Workout vollständig aus `entries` neu geschrieben.
- Einheiten: kg; Zeitübungen (`exercises.unit='min'`, `duration_s`) ↔ Set-Feld `sec`.
  Aufwärmsätze ↔ `warmup`. RIR/RPE nur, wenn Spalten existieren, sonst verwerfen.

## Übungen
Eingebauter Katalog leer bauen (`EXDB = []`), Übungen kommen ausschließlich als `customEx` aus der
DB (Name = AP-/Alias-Name, `bp`/`tg` aus `muscle`, `eq` aus `equipment`, `cat` strength/cardio).
Neue Übungen im Frontend → `customEx` → Insert in `exercises`. Keine Gymvisual-Medien (proprietär);
Kacheln ohne Bild/Initialen. Später optional: wger-Bilder (CC-BY-SA, Attribution je Übung).

## Plan und Rotation
openGym plant per Wochentag (`week`) bzw. Datum (`dayPlan`). Timo trainiert in AP-Rotation:
`GET /api/data` setzt `dayPlan[heute]` auf den **nächsten Plantag der Rotation** (bestehende
Logik in `domains/gym/plans.py`), `week` leer. „Choose a different workout“ bleibt nutzbar.

## Auslieferung, Auth, Sicherheit
- Fork-Quelle unter `web/gym_og/` (nur `frontend/` + nötige Skripte, ohne `exercise-media`,
  Coach, Admin, Capacitor-Plattformen). Build `vite build --base=/` → `web/gym_og/dist/`
  (eingecheckt, damit der Mantis-Start keinen Node-Build braucht). Build-Skript
  `scripts/build_gym_og.sh`. AGPL-Hinweis und NOTICE behalten.
- Port 7782 wie bisher. Übergangsweise neue App unter `/og/`, alte unter `/`; Umschalten nach
  Abnahme durch Timo.
- Auth: bestehendes `mantis_gym`-Cookie + Origin-Prüfung für alle `/api/*`. Ohne gültige Session
  liefert der Server die bestehende Token-Login-Seite aus, **nie** die App im Gast-Modus (sonst
  landen Trainings nur im localStorage). Frontend: Gast-/Passkey-/Registrierungs-Pfade
  deaktivieren; `/api/me` 401 → Weiterleitung zur Login-Seite.
- Keine echten Gesundheitsdaten in Fixtures; Tests gegen `mantis_test`.

## Phasen
1. **Lesen:** Fork einbinden, Build, Auslieferung unter `/og/`, Auth-Gate, `GET /api/data`,
   `/rev`, `/me`, `/config`; Schreiben serverseitig noch abgelehnt (409/403, Frontend zeigt Hinweis
   statt Datenverlust). Abnahme: Timo sieht Historie, Plan, Übungen im openGym-Look.
2. **Schreiben:** `PUT /api/data` mit Diff, Rev-Zähler bei allen Gym-Schreibpfaden, Konfliktfall.
   Roundtrip-Tests: GET → PUT unverändert = keine DB-Änderung; Satz hinzufügen/löschen;
   Workout löschen; Übung anlegen; Plan bearbeiten; gleichzeitige Mantis-Änderung → 409.
3. **Umschalten:** `/` = openGym, alte App entfernen oder unter `/old/`.
4. **Optional:** wger-Bilder, Feinschliff (Farben, Texte Deutsch/Englisch nach Wunsch).

## Nicht im Umfang
Coach, Admin, Mehrbenutzer, Passkeys, Push/Live Activity, Progress-Fotos/Medien-Sync,
Capacitor-Apps.
