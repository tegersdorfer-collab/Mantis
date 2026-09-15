# Implementierungsplan: Betriebshandbuch der Forge

## Ziel
Erstellung von docs/forge/betrieb.md basierend auf der Design-Spezifikation unter docs/superpowers/specs/2026-09-15-forge-betrieb-design.md durch Extraktion und Strukturierung von Informationen aus den Quelldateien.

## Schritte

### Schritt 1: Extrahiere Kommandoinformationen aus forge/cli.py
**Test ZUERST:** 
```bash
grep -E "sub.add_parser|help=" forge/cli.py | grep -A1 -B1 "status\|approve\|reject\|requeue\|stop"
```
**Erwartetes Ergebnis:** Liste der Befehle status, approve, reject, requeue, stop mit ihren Hilfetexten gefunden.
**Dateipfade:** forge/cli.py
**Aktion:** Extrahiere die fünf Befehle (status/approve/reject/requeue/stop) mit ihren Parametern und Beschreibungen aus forge/cli.py für den Abschnitt "Morgen-Routine" des Betriebshandbuchs.

### Schritt 2: Extrahiere Nachtfenster und Steuerungsdateien aus forge/daemon.py
**Test ZUERST:**
```bash
grep -E "NACHT_BEGINN_STUNDE|NACHT_ENDE_STUNDE|HALT_FILE|STOP_FILE" forge/daemon.py
```
**Erwartetes Ergebnis:** NACHT_BEGINN_STUNDE = 23, NACHT_ENDE_STUNDE = 7, HALT_FILE und STOP_FILE Pfade gefunden.
**Dateipfade:** forge/daemon.py
**Aktion:** Extrahiere Nachtfenster (23:00-07:00), Halt-Datei (~/.mantis-forge-halt), Not-Aus-Datei (~/.mantis-forge-stop) und Fehler-Spirale Logik für den Abschnitt "Anhalten" des Betriebshandbuchs.

### Schritt 3: Extrahiere Morgenbericht-Informationen aus forge/bericht.py
**Test ZUERST:**
```bash
grep -E "morgenbericht|freigabe|geparkt|anbieter" forge/bericht.py | head -10
```
**Erwartetes Ergebnis:** Funktionen morgenbericht(), _zeile_freigabe(), _zeile_geparkt(), _zeile_anbieter() gefunden.
**Dateipfade:** forge/bericht.py
**Aktion:** Extrahiere, was im Morgenbericht enthalten ist (Freigabe-Tasks, geparkte Tasks, Anbieter-Status, Daemon-Ereignisse, Not-Aus-Status) für den Abschnitt "Morgen-Routine" des Betriebshandbuchs.

### Schritt 4: Extrahiere Installationsinformationen aus forge/launchd/README.md
**Test ZUERST:**
```bash
cat forge/launchd/README.md
```
**Erwartetes Ergebnis:** Installationsschritte mit launchctl Befehlen und Hinweis auf verwaiste Worktrees gefunden.
**Dateipfade:** forge/launchd/README.md
**Aktion:** Extrahiere Installationsanleitung und Hinweis auf verwaiste Worktree-Bereinigung für Referenz im Betriebshandbuch (falls benötigt).

### Schritt 5: Extrahiere Aufgaben-Zustände aus forge/models.py
**Test ZUERST:**
```bash
grep -E "QUEUED|SPECCING|PLANNING|IMPLEMENTING|REVIEWING|GATING|AWAITING_APPROVAL|MERGED|PARKED|FAILED" forge/models.py | grep -v "^#" | head -10
```
**Erwarteted Ergebnis:** Alle neun Task-Zustände mit ihren Werte gefunden.
**Dateipfade:** forge/models.py
**Aktion:** Erstelle eine Tabelle aller Task-Zustände (QUEUED, SPECCING, PLANNING, IMPLEMENTING, REVIEWING, GATING, AWAITING_APPROVAL, MERGED, PARKED, FAILED) mit Bedeutung und zulässigen Übergängen für den Abschnitt "Zustaende eines Tasks" des Betriebshandbuchs.

### Schritt 6: Strukturieren des Betriebshandbuchs
**Test ZUERST:**
```bash
# Überprüfe, dass alle Informationen extrahiert wurden
echo "Prüfe Vollständigkeit der Extraktion:"
echo "1. Befehle: status/approve/reject/requeue/stop"
echo "2. Nachtfenster: 23-07, Halt/Stop-Dateien"
echo "3. Morgenbericht-Komponenten"
echo "4. Installationsinfo"
echo "5. Task-Zustände-Tabelle"
```
**Erwartetes Ergebnis:** Alle fünf Informationsbereiche sind verfügbar für die Strukturierung.
**Dateipfade:** Keine (Konsolidierungsschritt)
**Aktion:** Strukturieren Sie das Betriebshandbuch mit den erforderlichen Abschnitten:
1. Was passiert in einer Nacht (Ablauf)
2. Morgen-Routine 
3. Anhalten (Halt vs. Not-Aus)
4. Zustaende eines Tasks als Tabelle
5. Was tun wenn: Gate rot, Task geparkt, Anbieter leer, kein Daemon-Start

### Schritt 7: Erstelle docs/forge/betrieb.md
**Test ZUERST:**
```bash
# Prüfe, ob Zieldokument noch nicht existiert
[ ! -f docs/forge/betrieb.md ] && echo "Zieldokument existiert noch nicht - bereit zum Erstellen" || echo "WARNUNG: Zieldokument existiert bereits"
```
**Erwartetes Ergebnis:** Bestätigung, dass docs/forge/betrieb.md noch nicht existiert (oder Überschreibung beabsichtigt).
**Dateipfade:** docs/forge/betrieb.md (Ziel), alle Quelldateien als Quelle
**Aktion:** Schreibe das strukturierte Inhalt in German nach docs/forge/betrieb.md mit maximal 150 Zeilen, couvrantend alle fünf erforderlichen Abschnitte basierend auf den extrahierten Informationen.

## Abschließende Überprüfung
**Test NACH Fertigstellung:**
```bash
# Überprüfe, dass das Handbuch erstellt wurde und die Anforderungen erfüllt
[ -f docs/forge/betrieb.md ] && echo "Handbuch existiert" || echo "FEHLER: Handbuch fehlt"
wc -l docs/forge/betrieb.md | awk '{if ($1 <= 150) print "ZEILEN OK: " $1 " <= 150"; else print "ZEILEN ÜBERSCHREITUNG: " $1 " > 150"}'
grep -E "Was passiert in einer Nacht|Morgen-Routine|Anhalten|Zustaende eines Tasks|Was tun wenn" docs/forge/betrieb.md
```
**Erwartetes Ergebnis:** Handbuch existiert, ≤150 Zeilen, enthält alle fünf required sections.