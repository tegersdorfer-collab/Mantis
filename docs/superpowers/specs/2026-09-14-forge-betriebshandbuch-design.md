# Design-Spec: Betriebshandbuch der Forge (`docs/forge/betrieb.md`)

**Datum:** 2026-09-14  
**Status:** In Entwurf  

---

## Ziel

Erstellung der Dokumentationsdatei `docs/forge/betrieb.md` als kurzes, praxistaugliches Betriebshandbuch für den Nachtbetrieb der Forge. Das Handbuch richtet sich an Timo als Betreiber und beschreibt den nächtlichen Ablauf, die Morgen-Routine, Steuerungsmöglichkeiten (Halt vs. Not-Aus), Zustandsübergänge sowie konkrete Handlungsanweisungen bei Störungen.

---

## Betroffene Module

- **`docs/forge/betrieb.md`**: Einzige anzulegende Datei (Dokumentations-Handbuch, Deutsch, maximal 150 Zeilen).
- **Referenzierte Quelldateien (read-only Informationsquelle)**:
  - `forge/cli.py`: Befehle `status`, `approve <id>`, `reject <id> "<grund>"`, `requeue <id>`, `stop`.
  - `forge/daemon.py`: Nachtfenster (23:00–07:00 Uhr), Halt-Datei (`~/.mantis-forge-halt`), Not-Aus-Datei (`~/.mantis-forge-stop`), Fehler-Spirale (maximal 3 aufeinanderfolgende Fehlschläge).
  - `forge/bericht.py`: Morgenbericht (`python3.14 -m forge.cli status`).
  - `forge/launchd/README.md`: launchd-Job (`com.mantis.forge.plist`) und Bereinigung verwaister Worktrees.
  - `forge/models.py`: Zustandsmodell (`queued`, `speccing`, `planning`, `implementing`, `reviewing`, `gating`, `awaiting_approval`, `merged`, `parked`, `failed`).

---

## Datenfluss

1. **Nächtlicher Ausführungsfluss (Daemon)**:
   - **Start**: launchd startet den Daemon um 23:00 Uhr (`com.mantis.forge.plist`).
   - **Verarbeitung**: `daemon.py` wählt den nächsten Task in `queued` aus (oder nimmt einen unvollständigen Task in `ACTIVE_STATES` wieder auf).
   - **Pipeline**: Der Task durchläuft die Stufen `speccing` -> `planning` -> `implementing` -> `reviewing` -> `gating`.
   - **Gate**: Bei grünem Gate schaltet der Task in den Zustand `awaiting_approval` um und wartet auf Timos Freigabe.
   - **Abschluss**: Um 07:00 Uhr endet das Nachtfenster; der Daemon beendet sich geordnet mit Exit-Code 0.

2. **Morgen-Routine & Operator-Interaktion**:
   - Timo ruft morgens `python3.14 -m forge.cli status` auf.
   - Das System liest die Zustände und Journal-Einträge aus PostgreSQL und gibt den Morgenbericht aus.
   - Timo führt je nach Zustand Interaktionen durch:
     - `approve <id>`: Mergt den Branch nach `main`, setzt den Zustand auf `merged` und löscht den Worktree.
     - `reject <id> "<grund>"`: Parkt den Task in `parked` mit Begründung.
     - `requeue <id>`: Versetzt einen geparkten (`parked`) oder gescheiterten (`failed`) Task zurück in `queued`.

3. **Struktur des Betriebshandbuchs (`docs/forge/betrieb.md`)**:
   - **1. Was passiert in einer Nacht**: Ablauf in 5–8 prägnanten Sätzen.
   - **2. Morgen-Routine**: Schritt-für-Schritt-Anleitung mit `status`, Prüfung und CLI-Befehlen (`approve`, `reject`, `requeue`).
   - **3. Anhalten**: Gegenüberstellung von Weichem Halt (`forge.cli stop` / `~/.mantis-forge-halt`) und Not-Aus (`~/.mantis-forge-stop`), inkl. Einsatzszenarien.
   - **4. Zustände eines Tasks**: Markdown-Tabelle aller im System verankerten Zustände aus `forge/models.py`.
   - **5. Was tun wenn...**: Konkrete Lösungswege für 5 Fehlerszenarien (Gate rot, Task geparkt, Anbieter leer, kein Daemon-Start, verwaister Worktree).

---

## Fehlerbehandlung

Das Betriebshandbuch beschreibt das Vorgehen bei folgenden Fehlersituationen:

- **Gate rot**: Fehlergründe im Morgenbericht oder Journal prüfen. Bei Unklarheit Code/Tests korrigieren und mit `requeue <id>` neu starten, sonst mit `reject <id> "<grund>"` ablehnen.
- **Task geparkt**: Ursache (`parked_reason`) via `status` oder Journal ermitteln. Blockade beheben (z. B. fehlende Vorbedingungen) und mit `requeue <id>` wieder einreihen.
- **Anbieter leer / Kontingent erschöpft**: Daemon legt automatische Pause ein (`KONTINGENT_SLEEP_SECONDS` = 900s). Kein manueller Eingriff erforderlich; Kontingente erneuern sich am Folgetag.
- **Kein Daemon-Start / Not-Aus aktiv**:
  - Prüfen, ob `~/.mantis-forge-stop` existiert (durch 3 Fehlschläge in Folge via Fehler-Spirale oder manuelle Anlegung). Nach Ursachenbehebung Datei manuell löschen.
  - launchd-Status via `launchctl print gui/$(id -u)/com.mantis.forge` überprüfen.
- **Verwaister Worktree**: Falls nach `MERGED` ein Worktree unter `~/Mantis-forge/task-N` hängen bleibt: manuell mit `git worktree remove --force ~/Mantis-forge/task-N` aufräumen und Branch `forge/task-N` löschen.

---

## Was ausdrücklich NICHT gebaut wird

- Keine Code-Änderungen an Modulen in `forge/`, `core/`, `web/` oder Tests.
- Keine Erstellung neuer CLI-Befehle oder Automatisierungsskripte.
- Keine Änderung an bestehenden Konfigurationen oder Plist-Dateien.
- Die zu erstellende Dokumentationsdatei `docs/forge/betrieb.md` darf nicht mehr als 150 Zeilen umfassen.

---

## Getroffene Annahmen

- Annahme: Der Betreiber (Timo) führt alle CLI-Befehle im Hauptverzeichnis des Projekts über `python3.14 -m forge.cli <befehl>` aus.
- Annahme: Das Nachtfenster (23:00 bis 07:00 Uhr) wird durch launchd initiiert und durch `daemon.im_nachtfenster()` während des Betriebs eingehalten.
- Annahme: Das Dokument wird vollständig auf Deutsch verfasst und richtet sich speziell an Timo als Einzelbetreiber des Systems.
- Annahme: Entfernte oder veraltete Zustände (wie `awaiting_restart_window`) werden nicht in das Handbuch aufgenommen, da sie in `forge/models.py` gestrichen wurden.
- Annahme: Die Obergrenze von 150 Zeilen für `docs/forge/betrieb.md` ist eine strikte Anforderung, die bei der Erstellung einzuhalten ist.
