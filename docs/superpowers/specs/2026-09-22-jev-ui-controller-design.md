# Jev UI Controller — Design

**Datum:** 2026-09-22
**Status:** Entwurf nach Nutzerfreigabe der Richtung
**Scope:** Mantis' bestehendes `computer_task` für macOS-Accessibility-Apps

## Ziel

Mantis soll bei wiederholten, klar begrenzten UI-Entscheidungen nicht für jeden
Schritt einen vollständigen qwen3.5:9b-ReAct-Turn verbrauchen. Jev übernimmt die
Auswahl der nächsten erlaubten UI-Aktion aus einem strukturierten Accessibility-
Snapshot. Das lokale qwen-Modell bleibt als Fallback und als Writer für echten
Freitext erhalten.

Erfolg bedeutet:

- Ein UI-Schritt kann ohne vollständigen qwen-Tool-Call über Jev entschieden und
  deterministisch ausgeführt werden.
- Ohne Jev-Key, bei Timeout, ungültiger Antwort oder zu niedriger Sicherheit
  bleibt der bisherige qwen-Loop verfügbar.
- Die roten Linien in `tools/uiauto/safety.py` bleiben eine harte Grenze vor
  jeder realen Aktion.
- Jev-UI-Entscheidungen werden getrennt von den bisherigen Mantis-Entscheidungen
  gezählt und geloggt, damit Kosten, Latenz und spätere Trefferquote messbar sind.

## Nicht-Ziele

- Kein Screenshot-/Vision-Fallback in dieser Ausbaustufe.
- Kein neuer Desktop-Client und keine Änderung an der Tauri-Oberfläche.
- Keine automatische Freigabe von Löschen, Senden, Kaufen, Passwortfeldern oder
  anderen Redline-Aktionen.
- Kein Ersetzen des qwen-Modells für freie Sprache, Planung außerhalb der UI oder
  Freitextgenerierung.

## Architektur

Der bestehende Einstiegspunkt bleibt `computer_task(goal, app)`. Die bisherige
interne qwen-ReAct-Schleife wird zu einem Hybrid mit zwei Pfaden:

1. **Jev-Controller:** Accessibility-Snapshot → geschlossene Choice-Frage →
   Konfidenz-/Sicherheitsprüfung → deterministische Aktion → neuer Snapshot.
2. **qwen-Fallback:** der vorhandene isolierte ReAct-Loop bleibt unverändert
   nutzbar, wenn Jev nicht verfügbar ist, die Antwort nicht sicher genug ist oder
   die UI nicht in die geschlossene Aktionsmenge passt.

Die Controller-Logik kommt in ein eigenes Modul `core/uiauto_controller.py`.
`core/skills/uiauto.py` bleibt die Tool-/Rechte-/Safety-Grenze und delegiert nur
die Orchestrierung. Damit kann die Aktionsauswahl ohne macOS und ohne echtes
Klicken getestet werden.

### Jev-State

Jeder Jev-Call enthält:

- das unveränderte Nutzerziel,
- den optionalen App-Namen,
- den aktuellen Schrittindex,
- die zuletzt sichtbaren, aktivierbaren Accessibility-Elemente mit `ref`, Rolle,
  Titel, Wert und Aktivierungsstatus,
- eine kurze Historie der letzten Aktionen.

UI-Titel und Werte werden als untrusted UI-Fremdtext behandelt. Jev darf daraus
nur eine Aktion aus der vom Controller gelieferten Choice-Menge wählen; es darf
keine eigene Tool-Syntax oder neue Ziel-Referenz erzeugen.

### Geschlossene Aktionsmenge

Der Controller erzeugt pro Snapshot nur diese Optionen:

- `click:<ref>` für aktivierte Elemente,
- `type_text` wenn ein nicht-sicheres Texteingabefeld vorhanden ist,
- `key:return` und `key:escape`, wenn sie für den UI-Zustand sinnvoll sind,
- `done`, wenn das Ziel bereits erreicht ist,
- `abort`, wenn kein sicherer nächster Schritt erkennbar ist.

Die `Choice`-Menge ist auf die API-Grenze begrenzt. Bei einem zu großen Snapshot
oder fehlender sinnvoller Beschneidung fällt der Controller auf qwen zurück,
statt ein möglicherweise passendes Element still abzuschneiden.

### Konfidenz und Fallback

- Jev wird nur aktiviert, wenn der bestehende Mantis-Jev-Provider aktiviert und
  erreichbar ist.
- Eine `click`- oder `key`-Antwort unter der UI-ACT-Schwelle wird nicht ausgeführt;
  der Lauf wechselt in den qwen-Fallback.
- `done`/`abort` beendet den Hybrid-Lauf mit einer kurzen Statusantwort.
- `type_text` ruft einen einzelnen kurzen qwen-Writer ohne Tools auf und führt
  danach ausschließlich die bestehende Eingaberoutine aus. Der Writer darf keine
  Klickentscheidung treffen.
- Jev-Timeouts, Providerfehler, unbekannte Antworttypen und ungültige Refs sind
  kontrollierte Fallbacks, keine Ausnahme bis zum Nutzer.

Die Schwelle wird als neue Einstellung `JEV_UI_ACT_CONFIDENCE` geführt. Der
Default ist konservativ und wird nach einem kleinen Live-/Replay-Benchmark nicht
blind heruntergesetzt.

### Sicherheit

Vor einem Klick bleibt der bestehende Aufruf `safety.is_redline(element)` die
letzte Instanz. Jev kann eine rote Linie wählen, aber der Controller führt sie
nicht aus und meldet den Grund. Ungültige, deaktivierte oder veraltete Refs
werden ebenfalls nicht ausgeführt. Der qwen-Fallback benutzt dieselben
Low-Level-Tools und damit dieselbe Safety-Grenze.

## Beobachtbarkeit und Kosten

Für jede Hybrid-Entscheidung werden nur technische Metadaten geloggt:

- Controller-Pfad (`jev` oder `qwen-fallback`),
- Aktionstyp, Konfidenz, Modell, Latenz und Fallback-Grund,
- kein kompletter UI-Text und keine Eingabeinhalte im Kostenlog.

Der bestehende `JEV_LOG_PATH` bleibt die zentrale optionale JSONL-Senke; sensible
State-Inhalte werden nicht zusätzlich in Klartext protokolliert. Ein kleiner
offline Replay-/Kosten-Test vergleicht die Anzahl qwen-Calls mit dem alten Loop.
Die tatsächliche Ersparnis wird erst nach diesem Test behauptet.

## Teststrategie

Test-first in drei Schichten:

1. **Controller-Reinlogik:** Choice-Kriterien, State-Begrenzung, Ref-Mapping,
   Konfidenz- und Fallback-Entscheidungen.
2. **Skill-Integration:** Jev-Antwort führt genau eine erlaubte Engine-Aktion
   aus, `type_text` geht nur über den Writer, Redlines und ungültige Refs werden
   nicht ausgeführt, Jev-Ausfall startet den alten qwen-Pfad.
3. **Bestehende Regressionen:** UI-Automation-Tests, Jev-Entscheidungs-Tests und
   danach die vollständige Python-Suite sowie Ruff.

Live-Mac-Aktionen werden nicht in Unit-Tests ausgeführt. Ein echter Jev-Call wird
nur als expliziter Smoke-Test gegen den bereits vorhandenen Key und ohne Klick
auf eine reale App ausgeführt.

## Voraussichtliche Dateien

- Create: `core/uiauto_controller.py`
- Modify: `core/skills/uiauto.py`
- Modify: `settings.py`
- Create/modify: `tests/test_uiauto_controller.py`
- Modify: `tests/test_uiauto_skill.py`
- Modify: `README.md` oder eine kurze UI-Automation-Doku für Aktivierung und
  Fallback-Verhalten

Die bestehende `.env` wird nicht gelesen oder geändert. Aktivierung erfolgt über
die bereits vorhandenen Jev-Einstellungen; nur die neue UI-Schwelle bekommt einen
sicheren Default.
