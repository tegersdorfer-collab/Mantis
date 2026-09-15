# Design-Spec: Betriebshandbuch der Forge (Nachtbetrieb)

## Ziel
Erstellung eines kompakten Betriebshandbuchs (docs/forge/betrieb.md) für den Nachtbetrieb der Forge, das Timo als Betreiber klare Abläufe, Entscheidungsgrundlagen und Handlungsanweisungen für den täglichen Betrieb bietet.

## Betroffene Module
- forge.cli (Kommandos status, approve, reject, requeue, stop)
- forge.daemon (Nachtfenster, Halt-Datei, Not-Aus-Datei, Fehler‑Spirale)
- forge.bericht (Morgenbericht‑Generierung)
- forge.models (Task‑Zustände und Übergänge)
- forge.queue (Task‑Claim, Parking, State‑Updates)
- forge.gate (deterministisches Gate‑Ergebnis)
- forge.pipeline (einzelne Stufe‑Ausführung)
- forge.journal (Ereignis‑Logging)
- forge.worktree (Arbeitsbereich‑Management)
- forge.launchd/README.md (Installationshinweise, verwaister Worktree)

## Datenfluss
1. Der Daemon startet im Nachtfenster (23:00‑07:00) und ruft wiederholt `queue.claim_next()` auf.
2. Für den zurückgeforderten Task wird ein Worktree angelegt und je nach aktuellem Zustand entweder eine Pipeline‑Stufe (`pipeline.eine_stufe`) ausgeführt oder, wenn Zustand GATING, das Gate (`gate.pruefe`) aufgerufen.
3. Bei erfolgreichem Gate wechselt der Task in `awaiting_approval` (wartet auf Freigabe); bei Fehlschlag oder rotem Gate wird der Task mit `queue.park` geparkt und Grund ins Journal geschrieben.
4. Zustandsänderungen werden über `queue.set_state` persisitiert; Fehlschläge werden mittels `queue.zaehle_fehlschlag` gezählt.
5. Der CLI liest aktuelle Zustände über `forge.cli.status` → `bericht.morgenbericht()`, welcher Daten aus Queue (awaiting_approval, parked), Budget (Anbieter‑Verbrauch), Journal (Daemon‑Ereignisse, Not‑Aus) und den Halt/Stop‑Dateien aggregiert und als reinen Text ausgibt.
6. Durch die CLI‑Kommandos `approve`, `reject`, `requeue`, `stop` kann der Betreiber Zustände ändern (z. B. `awaiting_approval → merged`, `parked → queued`, Daemon‑weicher Halt).

## Fehlerbehandlung
- **Gate rot:** Task wird sofort geparkt; Grund aus `gate.pruefe` wird im Journal und Morgenbericht sichtbar. Betreiber prüft Grund und entscheidet über `requeue` oder weitere Untersuchung.
- **Mehr als drei aufeinanderfolgende Fehlschläge (Fehler‑Spirale):** Daemon setzt Not‑Aus‑Datei (`~/.mantis-forge-stop`) mit Grund, schreibt Journal‑Eintrag und beendet sich mit Exit‑Code 0. Betreiber muss Not‑Aus‑Datei entfernen bzw. Ursache beheben, bevor weiterer Betrieb möglich ist.
- **Not‑Aus‑Datei vorhanden:** Beim nächsten `should_run()`‑Check beendet sich Daemon sofort (Exit 0). Nicht‑automatischer Wiederstart; Betreiber muss Datei löschen oder verschieben.
- **Halt‑Datei vorhanden:** Daemon beendet sich nach erfolgreichem Abschluss des aktuellen Ticks (weicher Stop, Exit 0). Datei wird beim Daemon‑Start automatisch entfernt, falls veraltet.
- **Kein Anbieter‑Kontingent:** Daemon erkennt leere Anbieter und erhöht Schlafzeit (`KONTINGENT_SLEEP_SECONDS = 900 s`). Keine Aufgaben werden gestartet, bis Kontingent wieder verfügbar (automatisch durch launchd‑Neustart zur nächsten Nacht).
- **Daemon startet nicht:** Prüfen von launchd‑Job (`launchctl list | grep com.mantis.forge`). Fehlgeschlagene Startvorgänge gemäß `forge/launchd/README.md` wiederholen oder manuell mit `python3.14 -m forge.daemon` innerhalb Nachtfenster starten. Störende Halt‑/Stop‑Dateien entfernen.

## Ausdrücklich NICHT gebaut
- Automatisches Mergen aus GATING (auch nach grünem Gate) – dieser Schritt bleibt beim Betreiber (forge.cli approve).
- Telegram‑Bot oder andere Benachrichtigungswege (Plan 3).
- Dynamische Anpassung des Nachtfensters oder der Tageszeiten.
- Parallele Claude‑Sitzungen während Daemon‑Lauf.
- Grafische oder Web‑Oberflächen für Betrieb.
- Farben, Tabellen oder formatierte Ausgaben im Morgenbericht (rein textbasiert).
- Automatisches Löschen oder Archivieren von Worktrees nach Merge (verwaister Worktree muss manuell entfernt werden).

## Getroffene Annahmen
- Das Nachtfenster ist fixed auf 23:00‑07:00 Uhr (siehe `daemon.NACHT_BEGINN_STUNDE` und `NACHT_ENDE_STUNDE`).
- Die Halt‑Datei liegt unter `~/.mantis-forge-halt`, die Not‑Aus‑Datei unter `~/.mantis-forge-stop` im Heimatverzeichnis des Benutzers, der die Forge ausführt.
- Die Queue‑Logik (`forge.queue`) entspricht dem aktuell im Repository sichtbaren Stand (Zustandsübergänge, Parking, Fehlerschlag‑Zählung).
- Das deterministische Gate liefert ein Ergebnis mit Attribut `.ok` und einer Liste `.gruende` bei Misserfolg.
- Das Morgenbericht‑Format bleibt reiner Text ohne Farben, Tabellen oder sonstige Formatierung.
- Der Daemon wird ausschließlich über launchd (oder manuell über `python3.14 -m forge.daemon` innerhalb des Nachtfensters) gestartet.
- Es werden keine zusätzlichen externen Abhängigkeiten außer einer funktionierenden Postgres‑Datenbank und den konfigurierten LLM‑Anbietern vorausgesetzt.
- Der Betreiber (Timo) besitzt Zugriff auf das Home‑Verzeichnis und kann Dateien dort lesen, schreiben und löschen.