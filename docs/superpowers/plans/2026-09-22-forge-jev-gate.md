# Forge Jev-Gate

## Ziel

Die Forge erhält eine optionale Jev-basierte Vorprüfung vor dem ersten Agentenlauf. Jev klassifiziert den Arbeitsmodus, schätzt das Risiko und erkennt externe oder destruktive Absichten. Die Entscheidung ist ausschließlich Routing- und Sicherheitskontext; sie erteilt niemals eine Merge-, Push- oder sonstige Außenfreigabe.

## Sicherheits- und Architekturgrenzen

- Die bestehende deterministische Prüfung in `forge/gate.py` bleibt die letzte technische Instanz vor `awaiting_approval`.
- `forge/freigabe.py` bleibt die einzige Stelle, die nach Timos Freigabe merged.
- Bei deaktiviertem oder nicht erreichbarem Jev läuft die bestehende Forge unverändert weiter.
- Bei aktivem Jev-Gate und erkannter externer/destruktiver Absicht wird der Task vor einem Agentenlauf geparkt.
- Unsichere Klassifikation fällt auf das konservative Profil `plan` zurück; sie darf keinen riskanteren Modus aktivieren.
- An Jev gehen nur begrenzte, als untrusted markierte Tasktexte und minimale Metadaten. Persistiert werden nur die strukturierte Entscheidung und Hash-basierte Logs, keine Volltexte.
- Es gibt keine Datenbankmigration. Die Vorprüfung wird als internes `.forge/jev-preflight.json` im Task-Worktree abgelegt.

## Umsetzungsschritte

1. **Jev-Gate-Modul erstellen**
   - Neue Datei `forge/jev_gate.py`.
   - Definierte Choice-Werte: `plan`, `bugfix`, `tdd`, `security_review`.
   - Score mit geordneten Risikostufen.
   - Noul für externe/destruktive Absicht.
   - Antworten nur ab den festgelegten Confidence-/Risikobändern übernehmen.
   - Jev-Ausfall als `disabled`/`unavailable` zurückgeben, ohne lokale Policy zu umgehen.
   - Atomar und validiert in `.forge/jev-preflight.json` schreiben und lesen.

2. **Forge-Integration**
   - In `forge/daemon.py` nach dem Claim und vor `pipeline.eine_stufe()` die Vorprüfung ausführen.
   - Wiederaufnahmen verwenden das vorhandene Ergebnis und fragen Jev nicht erneut.
   - Externe/destruktive Absicht führt zu einem sichtbaren Park-Grund und keinem Agentenlauf.
   - Das gewählte Profil wird dem Pipeline-Kontext übergeben; es ändert nur die Prompts, nicht die Zustandsmaschine.

3. **Prompt-Kontext**
   - `forge/pipeline.py` bzw. der vorhandene Kontextpfad reicht Modus und Risikostufe an die Folgestufen weiter.
   - `security_review` verstärkt die Review-Anweisung; kein Profil darf den deterministischen Gate-Check abschalten.

4. **Konfiguration und Dokumentation**
   - Neue Opt-in-Konfiguration mit sicherem Default `False`.
   - Kurze Dokumentation des Datenflusses, der Fallbacks und der Tatsache, dass Jev keine Außenfreigabe erteilen darf.

5. **Tests zuerst**
   - Unit-Tests für Choice/Score/Noul-Auswertung, Confidence-Fallback, untrusted Tasktext, Persistenz/Validierung und Jev-Ausfall.
   - Daemon-Tests für „vor Agentenlauf parken“, Wiederaufnahme ohne zweiten Jev-Call und unveränderten Ablauf bei deaktiviertem Gate.
   - Pipeline-Tests für Profil-Kontext und unveränderte Zustandsübergänge.
   - Bestehende Forge- und Jev-Suites ausführen; anschließend ruff und die vollständige Testsuite.

## Abnahmekriterien

- Ein aktiviertes Jev-Gate kann keinen Merge und keinen Push auslösen.
- Ein Task mit externer/destruktiver Absicht startet keinen Agentenlauf.
- Jev ist optional: deaktiviert oder nicht erreichbar bleibt die bisherige Forge-Funktion erhalten.
- Ein unsicheres Ergebnis wird nicht als riskanter Modus oder als Freigabe interpretiert.
- Keine Taskbeschreibung landet unredigiert im Entscheidungslog.
- Alle neuen Pfade sind durch Tests abgedeckt und die bestehende Suite bleibt grün.
