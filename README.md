# Mantis

![Tests](https://github.com/tegersdorfer-collab/Mantis/actions/workflows/tests.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.14-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16+pgvector-blue)
![Swift](https://img.shields.io/badge/SwiftUI-iOS-orange)

**Persönlicher, autonomer AI-Concierge** — lokal betreibbar auf Apple Silicon, mit optionalen Cloud-LLMs. Erreichbar über Telegram, ein PWA-Dashboard, einen Tauri-Desktop-Client und drei native iOS-Apps.

Mantis ist ein Eigenprojekt, das ich vollständig allein konzipiert, entwickelt und in Produktion betrieben habe. Keine Tutorials, kein Starter-Template — von der Gedächtnisarchitektur bis zum BLE-Roboterprotokoll selbst erarbeitet.

---

## Auf einen Blick

| Kennzahl | Wert |
|----------|------|
| Programmiersprachen | Python 3.14 · Swift (SwiftUI) · JavaScript |
| Agent-Tools | Tool-Registry mit semantischem Routing und geprüften Ausführungsrechten |
| Tests | 1.100+ Python-Tests und 130+ Desktop-Tests; externe Dienste in Tests ersetzt |
| Laufzeit | 24/7 auf MacBook Pro M3 via launchd |
| LLM-Backends | Ollama lokal · Claude API (Haiku/Sonnet) |
| DB | PostgreSQL 16 + pgvector (Vektorsuche) |
| iOS-Apps | 3 SwiftUI-Apps gegen dieselbe FastAPI |

---

## Was Mantis kann

### Agent-Kern
- **ReAct-Loop** mit Tool-Registry, mehrstufiger Ausführung und sequenziellen Tool-Aufrufen; erlaubte Tools werden bei jedem Aufruf geprüft
- **LLM-Backends**: Ollama (qwen3.5:9b lokal) · Claude API (Haiku für Chat, Sonnet für komplexe Tasks)
- **Subagent-Delegation**: `delegate_task` startet Kind-Agenten mit eigenem Gesprächskontext und eingeschränkten Tools; bis zu drei gleichzeitige Delegationen, keine rekursive Delegation. Die Agents teilen denselben Prozess
- **LLM-Fallback**: Claude-Ausfall → lokales Ollama übernimmt transparent (90s-Timeout, 180s-Cooldown)
- **Token-Streaming** an Telegram + Dashboard (SSE)

### Gedächtnis-Architektur
| Schicht | Technologie | Funktion |
|---------|-------------|---------|
| **KZG** (Kurzzeitgedächtnis) | In-Memory + Rolling Checkpoints | Aktives Gesprächsfenster, ältere Turns via LLM komprimiert |
| **LZG** (Langzeitgedächtnis) | PostgreSQL + pgvector | Semantische Suche, Ebbinghaus Forgetting-Curve |
| **Knowledge-Graph** | kg_entities / kg_relations | Entitäten, Relationen, Directives, Warm-Profile |
| **SKILL.md** | Markdown + Frontmatter | Natürlichsprachliche Prozeduren, automatisch injiziert |

- **Recall Gate**: Jaccard-Heuristik überspringt pgvector wenn KZG bereits ausreichend Kontext hat
- **Multi-Signal Retrieval**: pgvector + Keyword-BM25 fusioniert
- **Warm Profile Injection**: KG-User-Entitäten (90s gecacht) top of every system prompt
- **Ebbinghaus Forgetting-Curve**: Wichtigkeit sinkt automatisch, steigt bei Bestätigung

### Selbst-Verbesserung
- **Skill-Factory**: Mantis speichert Python-Entwürfe als `.py.pending` zur manuellen Prüfung. Generierter Code wird weder automatisch importiert noch aktiviert oder committet. Die AST-Prüfung ist ein Strukturcheck, keine Sandbox; auch alte dynamische `.py`-Skills werden beim Start nicht geladen
- **SKILL.md Prozeduren**: Natürlichsprachliche Workflows werden automatisch aus Konversationen gelernt
- **Background Review Loop**: Ein separater LLM-Aufruf nach jedem Turn kann Markdown-Prozeduren und Erinnerungen speichern
- **Reflexions-Engine**: Ton/Stil-Anpassung, Längen-Kalibrierung, Proactive Engagement Decay
- **Embedding-basiertes Tool-Routing**: TF-IDF Semantic Fallback, max 14 Tools im Context

### Autopilot & Proaktivität
| Trigger | Was passiert |
|---------|-------------|
| 6–9h täglich | Morgen-Briefing mit Kalender, Wetter, Habits |
| 20–21h täglich | Abend-Review (Tages-Zusammenfassung) |
| 22–23h täglich | KI-Reflexion: Wins, Risiken, Muster |
| 7–10h täglich | Workout-Empfehlung basierend auf HRV + Schlaf |
| 12–14h täglich | Smart Notifications: fällige Tasks, Habit-Lücken |
| Montags 8–9h | Wöchentliche Themen-Recherche |
| Freitags 17–18h | Personal Newsletter (Telegram-Digest) |

### Domänen (Concierge-Skills)
| Domäne | Highlights |
|--------|-----------|
| **Health** | HealthKit Background-Push von iOS, HR-Min/Avg/Max, HRV, SpO₂, Schlaf-Stages |
| **Fitness** | Workout-Log, AlphaProgression (HRV/Schlaf-basierte Gewichtsempfehlung) |
| **Ernährung** | Mahlzeiten, Makros, adaptiver Kalorie-Rechner (BMR × Aktivität + Gewichtstrend) |
| **Habits** | Streak, Commit-Graph, Kategorien |
| **Tasks** | Unteraufgaben, AI-Zuweisung, Slipping-Detection |
| **Journal** | KI-Prompts, Stimmung/Energie |
| **Kalender** | Google Calendar lesen + schreiben |
| **Second Brain** | brain_notes, Wiki-Links, Graph-View (vis.js) |
| **Wissen** | Web-Suche, URL-Zusammenfassung, YouTube-Transkript |
| **Accountability** | Daily Anchor, Deep-Work-Block-Schutz, Abend-Check-in |
| **Robotik** | BLE-Steuerung Clementoni X5, per Reverse-Engineering entschlüsselt |
| **Smart Home** | Flipper Zero: IR-Befehle (Lampe, Ventilator) über USB-Serial |
| **Spotify** | Playback via official Web-API + User-OAuth, AppleScript/Spicetify fallback |
| **Gmail** | Lesen, Suchen, Archivieren, Senden (mit Bestätigungs-Gate) |

### Dashboard (PWA)
- **14 Views**: Home, Chat, Health, Habits, Tasks, Kalender, Ernährung, Journal, Ziele, Brain, Memory, A.I. Mind, Analytics, Settings
- **Live-Chat** mit SSE-Streaming direkt zum Agent-Kern
- **Dark/Light Mode**, Pull-to-Refresh, installierbar als Homescreen-App
- **Memory-Viewer**: Diary, Knowledge-Graph (vis.js), Mahlzeiten
- **Eval-Suite**: `/api/eval/run` — benannte Test-Cases für Agent-Verhalten
- **MCP-Server**: Mantis als Tool-Provider für Claude Code (`/mcp/` Endpunkte)
- **LLM-Usage-Tracking**: Token + Kosten pro Call, Kosten-Dashboard in Analytics
- **Generatives UI**: Tool-Aufrufe triggern automatisch passende Widgets

### Desktop und Forge
- **Desktop**: Tauri + TypeScript, HUD, Text-Chat, Sprache, Live-Widgets und Verbindungseinstellungen. Siehe [Desktop-README](apps/desktop/README.md).
- **Forge**: Entwicklungsaufgaben durchlaufen Spezifikation, Plan, Implementierung, Review und ein deterministisches Gate in eigenen Git-Worktrees. Nach bestandenem Gate wartet die Pipeline derzeit auf ein Neustartfenster; automatischer Merge und Neustart sind noch nicht umgesetzt.

### Native iOS-Apps (SwiftUI)
Drei fokussierte Apps gegen dieselbe FastAPI (`:7779`), erreichbar über Tailscale:

| App | Bündelt | Highlights |
|-----|---------|-----------|
| **BodyOS** | Training + Ernährung + Health | Mantis-generierte Sessions (HRV/Schlaf), Foto-Makros, HealthKit-Push |
| **BrainOS** | Second Brain | Wiki-Links, Force-directed Graph (SwiftUI Canvas + eigene Physik) |
| **FlowOS** | Tasks + Kalender + Habits | Today-View, Fokus-Timer, Habit-Grid |

---

## Stack

| Schicht | Technologie |
|---------|------------|
| Sprache | Python 3.14 (asyncio) · Swift (SwiftUI) · JavaScript |
| LLM | Ollama (qwen3.5:9b) · Claude API (Haiku/Sonnet) · MLX |
| DB | PostgreSQL 16 + pgvector |
| Backend | FastAPI + uvicorn |
| Frontend | Vanilla JS PWA (Chart.js, vis.js, marked.js, globe.gl, Leaflet) |
| iOS-Apps | 3 × SwiftUI — BodyOS / BrainOS / FlowOS |
| Kommunikation | python-telegram-bot · Voice (Whisper lokal + Piper TTS) · Foto-Analyse (llava:7b) |
| Health-Import | Swift-App → HealthKit Background Delivery → HTTP Push |
| Prozess-Management | launchd + KeepAlive (Auto-Restart nach Crash) |
| CI | GitHub Actions (ruff lint + pytest, Python 3.14) |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env       # Nur bei Ersteinrichtung; bestehende .env erhalten
# DASHBOARD_TOKEN, API-Keys und DB-Konfiguration in .env setzen
createdb mantis            # PostgreSQL + pgvector Extension
python3 main.py            # Mantis + Dashboard starten (Port 7779)
```

---

## Dashboard-Zugang und Umstellung

`DASHBOARD_TOKEN` muss ein zufälliger Zugangsschlüssel sein (beispielsweise mit `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` erzeugen und lokal in `.env` eintragen). Ohne Token bleiben geschützte Endpunkte gesperrt (HTTP 503). Bestehende Installationen müssen vor dem nächsten Start die Konfiguration ergänzen.

- **Bind-Adresse:** Standard ist `127.0.0.1`. Für andere Geräte eine explizite Tailscale-IP in `DASHBOARD_HOST` setzen; Wildcards (`0.0.0.0`, `::`) und normale LAN-Adressen werden abgelehnt. Der konfigurierte Port gilt für den Server.
- **PWA:** Dashboard öffnen und mit dem Token anmelden. Eine signierte HttpOnly-Sitzung gilt sieben Tage und erlaubt auch SSE. Token-Wechsel macht bestehende Sitzungen ungültig.
- **Desktop:** Unter Cmd/Ctrl+, Backend-Adresse und Zugangsschlüssel speichern. Der Schlüssel liegt im lokalen Webview-Speicher, getrennt nach Backend-Adresse. HTTP, SSE und Voice-WebSocket verwenden die Authentifizierung ohne URL-Parameter.
- **iOS:** In jeder App unter Einstellungen den Dashboard-Token setzen; Speicherung erfolgt pro Server im Keychain.
- **Spotify-Bridge:** Im Spotify-Profilmenü „Mantis-Verbindung“ Serveradresse und Token setzen. Zusätzlich `https://xpui.app.spotify.com` in `DASHBOARD_ALLOWED_ORIGINS` ergänzen (bestehende Desktop-Origins beibehalten). Standard ist `http://127.0.0.1:7779`; bei Tailscale-Bindung die entsprechende Adresse eintragen. Tokens werden pro Server gespeichert.
- **Andere API-Clients:** `Authorization: Bearer <DASHBOARD_TOKEN>` senden. Die drei `/api/trigger/…`-Routen verwenden weiterhin ihren eigenen `TRIGGER_TOKEN`.

`DASHBOARD_ALLOWED_ORIGINS` enthält ausschließlich explizite Browser-Origins. Die PWA funktioniert auf dem eigenen Origin; Desktop-Entwicklungs- und Tauri-Origins sind vorbelegt. Authentifizierung ersetzt keine Transportverschlüsselung: Remote-Zugriffe über Tailscale oder HTTPS betreiben. Nach Einrichtung der Schlüssel den Dienst regulär neu starten. Bereits laufende Prozesse können noch vorher geladene Python-Skills enthalten.

## Tests ausführen

```bash
pip install -r requirements-test.txt
pytest -q
```

Desktop und Spotify-Bridge:

```bash
npm --prefix apps/desktop ci
npm --prefix apps/desktop test
npm --prefix apps/desktop run build
node --test tests/test_spicetify_bridge.mjs
```

### Spotify-Web-API

Für die direkte Steuerung braucht Mantis eine Spotify-Developer-App und ein
Premium-Konto. In den App-Einstellungen die Redirect URI
`http://127.0.0.1:8084/callback` eintragen, `SPOTIFY_CLIENT_ID` und
`SPOTIFY_CLIENT_SECRET` in `.env` setzen und einmalig ausführen:

```bash
python3 scripts/spotify_auth.py
```

Der Login speichert den User-Token geschützt unter `data/spotify_token.json`.
Playback, Status und Suche laufen danach über die offizielle Web-API; AppleScript
und die Spicetify-Bridge bleiben als Fallback. Spotify steuert dabei das aktuell
aktive Spotify-Connect-Gerät. Wenn kein Gerät aktiv ist, wählt Mantis automatisch
einen verfügbaren Computer. Mit `SPOTIFY_DEVICE_NAME` kann optional ein exakter
Gerätename festgelegt werden.

CI prüft Python-Lint und Tests sowie Desktop-Tests, den Frontend-Build und die Spotify-Bridge bei Pushes und Pull Requests auf `main`. Die Tests ersetzen externe Dienste; sie ersetzen keinen Live-Test mit PostgreSQL, Ollama und echten Geräten.

---

## Architektur

```
Mantis/
├── main.py                  # Entry Point — Orchestrator + Dashboard im selben Prozess
├── orchestrator.py          # Schlanke Fassade: Init, Start/Stop, öffentliche API
│
├── core/
│   ├── prompt_builder.py    # System-Prompt (Memory, KG, Skills, Recall Gate)
│   ├── message_handler.py   # Nachrichtenverarbeitung, Streaming, Post-Turn Learning
│   ├── idle_loop.py         # Autopilot-Ticks, Maintenance, Monitoring
│   ├── agent.py             # ReAct-Loop, Tool-Calling
│   ├── autopilot.py         # Zeitbewusste autonome Aktivitäten
│   ├── background_review.py # Hintergrund-LLM lernt nach jedem Turn
│   ├── skill_factory.py     # Inaktive Python-Entwürfe zur manuellen Prüfung
│   ├── skill_md.py          # SKILL.md Prozeduren: Trigger-basierte System-Prompt-Injektion
│   ├── reflection.py        # Verhaltensanpassung, Stil-Kalibrierung
│   ├── eval_suite.py        # Benannte Test-Cases für Agent-Verhalten
│   ├── push.py              # Web Push (VAPID) + Templates + Dedup
│   ├── tools.py             # Tool-Registry + semantisches Routing
│   └── skills/              # Agent-Tools nach Kategorie (@T.register)
│
├── memory/
│   ├── kzg.py               # Kurzzeitgedächtnis + Rolling Checkpoints
│   ├── lzg.py               # Langzeitgedächtnis (pgvector, hybrid search)
│   ├── knowledge.py         # Knowledge-Graph + Warm-Profile
│   └── forgetting.py        # Ebbinghaus Forgetting-Curve + Importance Triage
│
├── domains/                 # Health, Fitness, Body, Ernährung, Habits, Tasks,
│                            # Journal, Ziele, Second Brain, Calendar, Weather, Gmail…
│
├── llm/                     # Ollama / Claude Provider (austauschbar über base.py)
├── communication/           # Telegram-Channel
├── tools/                   # WebSearch, DashboardReader, Delegate, Robot, Flipper, Spotify…
│
├── forge/                   # Entwicklungs-Pipeline mit Worktrees, Review und Gate
│
├── web/
│   ├── api.py               # FastAPI App-Factory: bindet Router aus web/routers/
│   ├── routers/             # REST + SSE pro Domäne (tasks, brain, fitness, …)
│   ├── mcp_server.py        # Mantis als MCP-Server (stdio JSON-RPC)
│   ├── index.html           # PWA-Frontend (Single-File)
│   └── sw.js                # Service Worker
│
└── apps/                    # Desktop (Tauri) und iOS (SwiftUI)
    ├── desktop/             # HUD, Voice, Chat und Live-Widgets
    ├── BodyOS/              # Training + Ernährung + Health
    ├── BrainOS/             # Second Brain (Notizen, Graph)
    └── FlowOS/              # Tasks + Kalender + Habits
```

---

## Architektur-Prinzipien

1. **Lokal first** — Daten verlassen den Mac nicht (außer explizite Cloud-Tools wie Claude API)
2. **Modulare Backends** — LLM, DB, Kommunikation sind austauschbar
3. **Kein Over-Engineering** — einfacher Code > komplexe Abstraktionen
4. **Mantis soll lernen** — jede Interaktion macht ihn besser, nicht nur reaktiver

---

*Entwickelt von Timo Egersdorfer — Eigenprojekt, vollständig selbst konzipiert und gebaut.*
