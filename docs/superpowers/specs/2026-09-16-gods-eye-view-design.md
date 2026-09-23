# God's Eye View in Mantis — Design

**Datum:** 2026-09-16 · **Status:** vom User im Chat freigegeben.

## Ziel

God's Eye View (GEV) erscheint als eigene Ansicht im Mantis-Dashboard. Mantis kann
den Globus über dieselben Agent-Tools aus Chat und Mantis-Spracherkennung steuern.
Die vorhandenen News-Globus- und Kartenansichten bleiben erhalten.

## Architektur

GEV läuft als separater, ausschließlich an `127.0.0.1:4173` gebundener
Node/Vite-Prozess. Ein Installationsskript checkt den getesteten Upstream-Commit
`a65d9d8` in ein ignoriertes Verzeichnis unter `data/` aus, installiert die
gesperrten npm-Abhängigkeiten und ergänzt einen kleinen Mantis-Bridge-Import.
Der Upstream wird nicht als Kopie in Mantis eingecheckt. Seine MIT-Lizenz und
die Datenquellen-Hinweise bleiben Bestandteil der Installation.

Die Dashboard-Ansicht lädt GEV in einem iframe. Ein lokaler Integrations-Patch
entfernt dessen Frame-Sperr-Header für den expliziten Mantis-Origin. Die
GEV-Bridge akzeptiert `postMessage`-Befehle ausschließlich vom Parent-Fenster
mit dem konfigurierten Mantis-Origin. Sie nutzt GEVs bestehende
`createGevActionRunner`-Schnittstelle und meldet Erfolg oder Fehler zurück.

Mantis stellt einen authentifizierten SSE-Kanal für Globus-Befehle bereit.
Agent-Tools veröffentlichen nur fest definierte Aktionen und validierte
Argumente. Der Dashboard-Client öffnet bei einem Befehl die GEV-Ansicht, sendet
ihn an den iframe und quittiert das Ergebnis per geschütztem API-Aufruf. Ein
Tool meldet erst nach Quittierung Erfolg; bei nicht erreichbarem Client läuft
es in einen kurzen Timeout. Aktionen werden nicht an beliebige Browser-Tabs
oder externe Hosts gesendet.

## Erste Befehle

- `gev_fly_to(place)` — Ortssuche und Kameraflug.
- `gev_set_layer(layer, enabled)` — bekannte Ebene ein- oder ausschalten.
- `gev_set_style(style)` — eine bekannte Darstellung wählen.
- `gev_reset_view()` — zum Gesamtglobus zurückkehren.

Das Tool-Routing nimmt Formulierungen zu Globus, Flugzeugen, Satelliten,
Schiffen und Kartenansichten auf. Mantis' Chat und Sprachpfad verwenden dieselbe
Tool-Registry. Die eigenen Voice-Funktionen von GEV sind für diese Integration
nicht nötig.

## Grenzen und Sicherheit

Die erste Laufzeit ist bewusst auf den Mac mit Mantis und GEV auf demselben
Rechner begrenzt. Das Upstream-Projekt ist laut `SECURITY.md` kein gehärteter
öffentlicher Dienst; GEV darf nicht für LAN/Tailscale gebunden werden. Ein
remote geöffnetes Mantis-Dashboard zeigt eine klare lokale Verfügbarkeitsmeldung.
Provider-Schlüssel werden weder von Mantis übernommen noch in diesem Feature
gespeichert. GEVs optionale Anbieter werden innerhalb von GEV eingerichtet;
deren Quoten und Nutzungsbedingungen gelten weiter.

## Prüfung

Python-Tests prüfen Validierung, Kanal, Timeout und API-Authentifizierung.
Node-Tests prüfen die Bridge-Origin-Grenze und die Aktionsübergabe. Danach
werden die relevanten Mantis-Tests, Ruff und die Upstream-Doctor-Prüfung
ausgeführt. Ein manueller Browser-Test prüft das Laden und einen Kamerabefehl.
