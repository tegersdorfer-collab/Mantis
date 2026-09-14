# Mantis Desktop

Tauri-Desktop-Client für Mantis (Windows + macOS aus einer Codebasis) — reiner Client,
das Backend (Whisper/Piper/Ollama/DB) läuft zentral auf dem Mac. Holographic-HUD-Stil,
generatives UI (Mantis wählt Layout + Widgets selbst).

Architektur: `docs/superpowers/specs/2026-07-04-multi-device-jarvis-ui-design.md`.
Phasen-Historie: `docs/superpowers/plans/2026-07-04-*.md`.

## Funktionen

- **HUD**: Ruhezustand-Ring, Grid-Textur/Scanline/Vignette-Ästhetik, Sound-Feedback
- **Voice**: always-on Mikrofon (keine Wake-Word), lokale VAD (RMS-Lautstärke),
  Whisper.cpp-Transkription + Piper-TTS-Antwort, Konversations-Fenster für Folge-Antworten
- **Chat**: Text-Eingabefeld als Alternative zu Sprache
- **Widgets** (11 Typen): sleep/training/tasks/calendar/nutrition/habits/system/brain/
  skills/weather/brain_graph — automatisch bei Tool-Nutzung oder manuell per Cmd/Ctrl+K
- **Alerts**: proaktive Autopilot-Nachrichten + Tool-Fehler-Warnungen als HUD-Toasts
- **Einstellungen**: Cmd/Ctrl+, öffnet ein Panel für die Backend-Adresse (Tailscale-
  Hostname des Macs für Remote-Clients wie einen Windows-PC)
- **System**: Tray-Icon, Fenster-State-Persistenz, Autostart bei Login

## Verbindung einrichten

Im Backend `DASHBOARD_TOKEN` setzen. Mit Cmd/Ctrl+, die Backend-Adresse und denselben Zugangsschlüssel eintragen. Der Schlüssel wird lokal pro Backend-Adresse gespeichert; beim Ändern der Adresse muss der passende Schlüssel neu eingetragen werden. HTTP und SSE verwenden Bearer-Header, der Voice-WebSocket einen Authentifizierungs-Subprotocol. Tokens stehen nie in URLs. Remote-Verbindungen über Tailscale oder HTTPS betreiben.

## Entwicklung

```
npm install
npm run tauri dev
```

## Tests

```
npm test        # Vitest
npx tsc --noEmit
```

## Build

```
npm run tauri build
```

Windows-Build kann nicht auf macOS getestet werden — braucht eine echte Windows-Maschine
oder einen Windows-CI-Runner.
