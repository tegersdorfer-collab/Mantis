# Mantis – Projektkontext für Hermes

## Projekt

Mantis ist ein persönlicher, autonomer AI-Concierge mit Python-Backend, PWA, Tauri-Desktop-Client, nativen SwiftUI-Apps und der Forge für Entwicklungsaufgaben.

Wichtige Bereiche:

- `main.py` – Einstiegspunkt
- `orchestrator.py` – Orchestrator-Fassade
- `core/` – Agent-Kern, Tools, Autopilot und UI-Zustand
- `domains/` – Fachdomänen
- `web/` – FastAPI und Dashboard
- `memory/` – Kurzzeit-, Langzeit- und Knowledge-Graph-Speicher
- `forge/` – Entwicklungs-Pipeline
- `tests/` – Python-Tests
- `apps/desktop/` – Tauri-Desktop-Client
- `apps/` – native Apple-Apps

## Verifikation

- Python: `python3.14`
- Tests: `python3.14 -m pytest tests/ -q`
- Linting: `python3.14 -m ruff check .`
- Desktop: `npm --prefix apps/desktop test` und `npm --prefix apps/desktop run build`
- Spotify-Bridge: `node --test tests/test_spicetify_bridge.mjs`

Nach Codeänderungen zuerst passende Tests ausführen, danach bei größeren Änderungen die vollständige relevante Suite und Ruff. Nur erfolgreiche, tatsächlich ausgeführte Prüfungen als bestanden melden.

## Arbeitsregeln

- Vor Änderungen README, relevante Architektur und bestehende Tests prüfen.
- Änderungen klein und fokussiert halten; bestehendes Verhalten nicht ohne Begründung ändern.
- Keine `.env`-Dateien, Tokens, Passwörter oder sonstigen Secrets lesen, ausgeben oder committen.
- Keine externen Nachrichten, Veröffentlichungen, Käufe, Merges oder Pushes ohne ausdrückliche Bestätigung.
- Bei paralleler Arbeit eigene Git-Worktrees verwenden; nicht mehrere Agenten im selben Checkout editieren lassen.
- Neue Features benötigen Tests; Fehlerbehebungen benötigen einen Regressionstest, sofern sinnvoll.
- Bei Unsicherheit Annahmen klar kennzeichnen und nicht stillschweigend raten.

## Forge-spezifisch

- `forge/` importiert aus Mantis nur die ausdrücklich erlaubten Schnittstellen, insbesondere `core.db` gemäß bestehender Forge-Dokumentation.
- Forge-Änderungen an `forge/`, den zugehörigen Tests und `docs/forge/` getrennt von anderen Änderungen halten.
- Telegram- und andere externe Dienste in Tests vollständig patchen; keine echten Nachrichten aus Tests senden.
- Kommentare, Docstrings, Tests und Dokumentation im Forge-Bereich auf Deutsch halten, sofern bestehende Dateien nichts anderes vorgeben.

## Abschlussbericht

Am Ende kurz nennen: geänderte Dateien, reale Testergebnisse, offene Punkte und ob externe Aktionen ausstehen.
