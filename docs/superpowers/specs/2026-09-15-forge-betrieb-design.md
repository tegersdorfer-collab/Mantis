# Design Spezifikation: Betriebshandbuch der Forge

## Ziel
Ein kurzes Betriebshandbuch (docs/forge/betrieb.md) erstellen, das Timo als Betreiber die nächtliche Arbeit der Forge erklärt. Es enthält Ablauf, Morgenroutine, Anhalten, Aufgaben‑Zustände und typische Fehler-szenarien, ausschließlich auf Deutsch und basierend auf den bestehenden Quellen.

## Betroffene Module
- **docs/forge/betrieb.md** (Zieldokument, neu zu erstellen)
- Quelldateien als Informationsquelle:
  - forge/cli.py
  - forge/daemon.py
  - forge/bericht.py
  - forge/launchd/README.md
  - forge/models.py

Keine dieser Quelldateien wird geändert.

## Datenfluss
Die Informationen fließen von den Quelldateien zum Handbuch:
1. Die relevanten Abschnitte (Befehle, Nachtfenster, Halt‑/Stop‑Dateien, Morgenbericht‑Logik, Zustandsmodell) werden aus den genannten Dateien extrahiert.
2. Der extrahierte Inhalt wird strukturiert, kurz gefasst und in deutscher Sprache im Ziel‑Markdown‑File abgelegt.
3. Es erfolgt keinerlei Rückfluss oder automatische Einbindung in das Build‑System; das Handbuch ist ein eigenständiges Dokument.

## Fehlerbehandlung
- **Veraltetes Handbuch**: Wenn sich die Quelldateien ändern, kann das Handbuch veraltet werden. Dies wird durch manuelle Nachkorrektur behoben (keine automatische Fehlererkennung).
- **Fehlende Quellen**: Sollte eine der Quelldateien nicht zugänglich sein, wird das betreffende Abschnitt im Handbuch weggelassen und eine Annahme dokumentiert (siehe Annahmen).

## Was ausdrücklich NICHT gebaut wird
- Keine Änderungen an bestehendem Code (forge/*) oder Konfiguration.
- Keine neuen Skripte, Automatisierungen oder Tooling.
- Keine Integration des Handbuchs in das bestehende Dokumentations‑Build‑System (z. B. kein automatisches Einbinden in andere Docs).
- Keine mehrsprachigen Versionen oder internationalen Anpassungen.

## Getroffene Annahmen
1. Die genannten Quelldateien (cli.py, daemon.py, bericht.py, launchd/README.md, models.py) repräsentieren den aktuellen Stand des Projekts korrekt.
2. Das Nachtfenster ist fixed auf 23:00–07:00 Uhr und wird über launchd gesteuert.
3. Die Dateien `~/.mantis-forge-halt` und `~/.mantis-forge-stop` befinden sich im Home‑Verzeichnis des Benutzers, der die Forge ausführt (Timo).
4. Timo ist mit der Bash‑Kommandzeile vertraut und kann die angegebenen `python3.14 -m forge.cli …` Befehle ausführen.
5. Die Tabelle der Task‑Zustände basiert auf den Konstanten aus `forge/models.py` und ist vollständig.
6. Bei einem Gate‑Fehler wird der Task automatisch geparkt; die Gründe sind im Journal bzw. Morgenbericht nachvollziehbar.
7. Sollte kein Daemon‑Start möglich sein, liegt das entweder an einem nicht geladenen launchd‑Job oder an einer vorhandenen Stop‑/Halt‑Datei.
8. Das Handbuch ist ausschließlich für den nächtlichen Betrieb gedacht; Tagesbetrieb oder manuelle Eingriffe außerhalb des Nachtfensters werden nicht behandelt.
9. Der Informationsstand ist zum Erstellungszeitpunkt (2026-09-15) gültig; spätere Änderungen erfordern eine Überarbeitung des Handbuchs.