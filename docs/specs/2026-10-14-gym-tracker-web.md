# Spec: Gym-Tracker als Web-App (v1)

Stand 2026-10-14. Kontext: `docs/HANDOFF-nutzungs-challenge.md`, `docs/specs/2026-10-14-gym-coach.md`.

## Warum
Timo trackt seit Langem in AlphaProgression und will nicht wechseln, solange er nichts verliert.
Ziel ist ein eigener Tracker mit allen Daten in Mantis, auf dem später ein Live-Coach aufsetzt.
Was Timo an AlphaProgression wirklich nutzt (Antwort 14.10.): **schnelles Abhaken mit
vorausgefüllten Werten, Plan/Split, Pausentimer und Verlauf**. Progressionsvorschläge nutzt er
nicht. Vorgehen laut Timo: klein und sauber anfangen, dann nach echter Nutzung nachbessern.

## Entscheidungen
- Web-App statt nativer iOS-App (Timo): kein Ablauf nach 7 Tagen, kein Neuinstallieren, Empfang
  im Gym ist gut. Als Homescreen-App (Vollbild) nutzbar.
- Eigener Port wie Filmroulette (Timo, 14.10.: ein Unterpfad lässt sich nicht sauber als
  Homescreen-App speichern): **`http://100.107.172.123:7782/`** (Tailscale-IP, nur im Tailnet).
  Eigene kleine FastAPI-App, die im Mantis-Prozess als zweiter uvicorn-Server läuft
  (Settings `GYM_HOST`, Default = Tailscale-IP aus `.env`, `GYM_PORT` = 7782, `GYM_ENABLED`).
  Host-Validierung wie `DASHBOARD_HOST` (nur Loopback oder Tailscale-IP). Ist die IP beim Start
  noch nicht verfügbar, wird der Bind im Hintergrund mit Backoff wiederholt; Mantis selbst darf
  daran nie scheitern.
- Nur die Big Three: `Bench Press`, `Squats`, `Deadlifts` (exakt die Namen aus dem
  AlphaProgression-Import, damit Verlauf und „letztes Mal“ ab dem ersten Tag aus den Altdaten kommen).
- Daten in den bestehenden Tabellen `workouts`/`workout_sets`, Sessionlogik aus
  `domains/gym/session.py` (offene Session, Timeout, eine offene Gym-Session). Chat-Coach und
  Web-App teilen dieselbe Session.
- Kein Live-Coach, keine Charts, keine Plan-Rotation, keine Übungsverwaltung in v1.

## Umfang v1
**Startbildschirm:** offene Session fortsetzen oder neue starten. Darunter die drei Übungen in fester
Reihenfolge (Squats, Bench Press, Deadlifts; Reihenfolge per Konstante änderbar).

**Übungsansicht:**
- Satzzeilen vorausgefüllt mit den Arbeitssätzen der letzten Session dieser Übung
  (Gewicht × Wdh., Aufwärmsätze aus Altdaten ausgeblendet). Ohne Historie: eine leere Zeile.
- Pro Zeile: Gewicht und Wdh. editierbar (große Touch-Ziele, Schritte ±2,5 kg / ±1 Wdh.), Haken ✓
  speichert den Satz. Abgehakte Zeilen sind sichtbar markiert und per Tippen korrigierbar oder
  löschbar. „+ Satz“ fügt eine Zeile mit den Werten der letzten hinzu.
- Darunter „Letzte Trainings“: die letzten 5 Sessions der Übung (Datum, Sätze kompakt).

**Pausentimer:** startet automatisch nach jedem ✓ (Standard 180 s, pro Übung per Konstante),
läuft oben sichtbar, ±30 s, überspringbar. Bei Ablauf Vibration (`navigator.vibrate`, wo
verfügbar) und Ton. Timer läuft clientseitig; nach Neuladen aus gespeichertem Startzeitpunkt
wiederhergestellt.

**Session beenden:** Button „Training beenden“ → `finish()`; kurze Zusammenfassung (Dauer,
Sätze, Volumen). Ohne Beenden greift der vorhandene Session-Timeout.

## Technik
- **Frontend:** statische Dateien unter `web/gym/` (`index.html`, `app.js`, `app.css`,
  `manifest.webmanifest`; Vanilla-JS, kein Build-Schritt), mobil zuerst, Dark-Mode, iOS-Meta-Tags
  für Vollbild (`apple-mobile-web-app-capable`, Icon), `start_url: /`. Shell-Dateien sind ohne
  Login abrufbar (enthalten keine Daten), alle `/api/*` sind geschützt.
- **Login:** eigene, von der Dashboard-Auth getrennte Session für die Gym-App: Shell zeigt einmalig
  ein Token-Feld, `POST /auth/session` prüft `DASHBOARD_TOKEN` (hmac-Vergleich) und setzt ein
  eigenes HttpOnly-Cookie `mantis_gym` (SameSite=Strict, Laufzeit Setting `GYM_SESSION_DAYS`,
  Default **180**). Dashboard-Session bleibt unverändert bei 7 Tagen. Strikte Origin-Prüfung bei
  schreibenden Requests (nur eigene Origin). Sicherheitsrelevant → Claude prüft diesen Teil selbst.
- **Offline-Puffer:** abgehakte Sätze gehen zuerst in eine Warteschlange in `localStorage` (mit
  Client-ID je Satz) und werden sofort gesendet; bei Fehler erneuter Versuch beim nächsten ✓,
  bei `online`-Event und beim Öffnen. Anzeige „n Sätze nicht synchronisiert“, solange offen.
  Server dedupliziert über die Client-ID (Idempotenz), damit Wiederholungen keine Doppel erzeugen.
- **API** (in der Gym-App, Modul `web/gym_app.py`, JSON, alles hinter der Gym-Session):
  - `GET /api/gym/state` → offene Session (oder null), Übungsliste mit Vorbefüllung je Übung,
    bereits in der offenen Session gespeicherte Sätze je Übung.
  - `POST /api/gym/sets` `{client_id, exercise, weight_kg, reps}` → speichert einen Arbeitssatz
    (öffnet bei Bedarf die Session), idempotent über `client_id`.
  - `PATCH /api/gym/sets/{id}` `{weight_kg?, reps?}` und `DELETE /api/gym/sets/{id}` → nur für
    Sätze der offenen Session.
  - `POST /api/gym/finish` → Zusammenfassung.
  - `GET /api/gym/history?exercise=…&limit=5`.
  Die Endpunkte rufen Funktionen in `domains/gym/` auf (neue schlanke Funktionen dort, wo
  `add_sets` den Parser-Typ erwartet); keine SQL im Router.
- **Schema:** neue Spalte `workout_sets.client_id text` mit eindeutigem Teilindex
  (`WHERE client_id IS NOT NULL`), idempotente Migration in `core/db.py`.
- Übungsauswahl nur aus der Allowlist der drei Namen; unbekannte Namen → 400.
- Nutzung messbar: jedes gespeicherte Set zählt als Gym-Nutzung für die Challenge-Auswertung
  (vorhandenen `use_case='gym'`-Zähler bzw. Usage-Endpoint mit Kanal `web-gym` bedienen).

## Fehlerfälle
- Server nicht erreichbar → Satz bleibt in der Warteschlange, UI zeigt es, nichts geht verloren.
- Doppeltes Senden → gleiche `client_id` liefert den bestehenden Satz zurück.
- Session per Timeout geschlossen, während Sätze in der Warteschlange liegen → Server öffnet neue
  Session (vorhandenes Verhalten), Zeitpunkt des Satzes wird nicht rückdatiert (v1, bewusst).
- Gewicht ≤ 0 oder > 500, Wdh. < 1 oder > 100 → 400.

## Tests (TDD, gegen `mantis_test`)
- Gym-App: Auth nötig (401 ohne Cookie), Shell/Manifest öffentlich ohne Daten, falsches Token 401,
  fremde Origin bei POST 403, Cookie-Laufzeit aus `GYM_SESSION_DAYS`, Host-Validierung.
- Start: Bind-Fehler des Gym-Servers bringt Mantis nicht zum Absturz.
- `state`: Vorbefüllung aus letzter Session, Aufwärmsätze ausgeschlossen, leere Historie.
- `sets`: Anlegen öffnet Session, Idempotenz über `client_id`, Validierung, Allowlist.
- `PATCH`/`DELETE` nur in offener Session, sonst 404/409.
- `finish` und `history`.
- Frontend: Smoke-Test im Browser-Pane (Login, Satz abhaken, Timer, Offline-Puffer per
  simuliertem Netzfehler, Neuladen) durch Claude nach dem Merge-Kandidaten.
- `python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .` grün.

## Später (nur wenn Timo es vermisst)
Live-Coach in der Session, weitere Übungen/Übungsverwaltung, Plan/Split-Rotation, Charts/PRs,
COROS-Tagesform, laufender Import aus AlphaProgression bis zum Umstieg.
