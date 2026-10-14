# Mantis Desktop — Windows-Handoff

> Diese Datei ist die Übergabe für **Claude Code auf dem Windows-PC**. Ziel: die
> bestehende Tauri-App (`apps/desktop`) auf Windows bauen, starten, testen und ein
> installierbares Windows-Binary erzeugen. **Es ist KEIN Neubau** — die App ist von
> Anfang an cross-platform (Windows + macOS aus einer Codebasis). Auf dem Mac läuft
> sie bereits. Der Windows-Teil ist reines „bauen + plattformspezifische Ecken fixen".

## Kontext in einem Absatz

`apps/desktop` ist ein **reiner Client** (Tauri v2, Rust + Vite/TypeScript-Frontend).
Das gesamte Backend (Whisper/Piper/Ollama-Embeddings/Postgres/FastAPI) läuft zentral auf Timos
**Mac** und wird NICHT auf Windows gestartet. Der Windows-Client redet nur über HTTP/WS
mit dem Mac-Backend übers Tailnet. Holographic-HUD-Stil, generatives UI (Backend wählt
Layout + Widgets). Voller Architektur-Spec:
`docs/superpowers/specs/2026-07-04-multi-device-jarvis-ui-design.md`.

## Backend-Adresse (Mac übers Tailnet)

- Tailscale-Hostname: `macbook-air-von-timo.tail7e29ff.ts.net`
- Tailscale-IP (Fallback): `100.107.172.123`
- Port: `7779`
- **In der App setzen**: `Strg + ,` öffnet das Settings-Panel → Backend-Adresse auf
  `http://macbook-air-von-timo.tail7e29ff.ts.net:7779` stellen (Default ist
  `http://localhost:7779`, s. `src/config.ts`, gespeichert in localStorage
  `mantis_base_url`).
- Voraussetzung: **Tailscale läuft auf dem Windows-PC** und der Mac ist online +
  Mantis-Backend gestartet. Health-Check: `http://…:7779/health` muss antworten.

## Voraussetzungen installieren (einmalig)

1. **Git** — https://git-scm.com
2. **Node.js LTS** — https://nodejs.org
3. **Rust** — https://rustup.rs (default MSVC-Toolchain übernehmen)
4. **Visual Studio Build Tools** — Workload **„Desktop development with C++"**
   (liefert den MSVC-Linker `link.exe`, den Rust zum Linken braucht)
5. **WebView2 Runtime** — auf Win11 vorinstalliert; sonst den Evergreen-Installer von
   Microsoft
6. **Tailscale** für Windows — angemeldet im selben Tailnet wie der Mac

Prüfen, dass alles da ist:
```
git --version
node --version
rustc --version && cargo --version
```

## Starten

```
git clone https://github.com/tegersdorfer-collab/Mantis.git
cd Mantis/apps/desktop
npm install
npm run tauri dev
```
Der erste `tauri dev`-Lauf kompiliert die ganze Rust-Toolchain — das dauert einige
Minuten, ist einmalig, danach inkrementell.

## Wo die echte Arbeit liegt (Risiko-Stellen, nach Wahrscheinlichkeit)

Der Build läuft mit hoher Wahrscheinlichkeit durch (Rust-Seite ist Standard-Tauri,
`src-tauri/src/lib.rs` hat keinen macOS-spezifischen Code außer dem Autostart-Plugin-
Parameter, der Windows automatisch abdeckt; `icon.ico` + MSIX-`Square*Logo.png` sind da).
Debuggen wird es vor allem hier:

1. **Mikrofon in WebView2** (Hauptrisiko). Die Voice-Pipeline nutzt
   `navigator.mediaDevices.getUserMedia({audio:true})` (`src/voice-capture.ts:98`,
   `src/voice-capture-stream.ts:58`).
   - Windows-System: **Einstellungen → Datenschutz & Sicherheit → Mikrofon** →
     „Desktop-Apps den Zugriff auf das Mikrofon erlauben" AN.
   - WebView2 fragt Permission nicht immer automatisch ab. Falls `getUserMedia` mit
     `NotAllowedError`/`NotFoundError` fehlschlägt: den Permission-Request in der
     WebView behandeln (Tauri v2: ggf. über `WebviewWindow`-Konfiguration bzw. einen
     Rust-seitigen Permission-Handler). Erst hier ansetzen, wenn es real fehlschlägt.

2. **AudioWorklet** (`src/voice-capture-stream.ts:68`, lädt ein `pcm-worklet`-Modul mit
   `AudioContext({sampleRate:16000})`). Läuft im `dev` meist sofort; im **`tauri build`**
   verifizieren, dass der Worklet-Asset-Pfad im gebündelten Frontend auflöst (sonst
   startet der Streaming-Pfad still nicht). Prüfen: kommt beim Reden ein Segment am
   Backend an?

3. **Audio-Formate** — HIER ist Windows im Vorteil: WebView2 = Chromium, kann
   `audio/webm` (MediaRecorder, `voice-capture.ts:170`) und `data:audio/ogg`-Wiedergabe
   (`voice-capture.ts:58`, `voice-capture-stream.ts:32`) nativ. Das sind die Formate, die
   auf dem Mac (WKWebView) zickig sind — auf Windows sollten sie einfach gehen. Falls
   doch nicht: hier ist es Format-Support, nicht Permission.

4. **Tauri-Capabilities** (`src-tauri/capabilities/default.json`). Aktuell nur
   `core:default` + `opener:default` gelistet, obwohl `window-state`- und `autostart`-
   Plugins geladen sind. Falls beim Start ein Plugin-Permission-Fehler kommt: die
   passende Permission dort ergänzen (z.B. `autostart:default`, `window-state:default`).

5. **Autostart** (`src-tauri/src/lib.rs`, `tauri-plugin-autostart`). Nutzt den
   `MacosLauncher`-Parameter, der unter Windows ignoriert wird (Windows = Registry-Run-
   Key, macht das Plugin selbst). Verhalten nach dem ersten Build kurz gegenprüfen.

## Build (installierbares Binary)

```
npm run tauri build
```
Erzeugt unter `src-tauri/target/release/bundle/` einen `.msi` (WiX) und/oder `.exe`
(NSIS). `bundle.targets` steht auf `"all"` (`tauri.conf.json`) — auf Windows greifen
davon die Windows-Installer.

## Fertig = verifiziert, wenn:

- [ ] `npm run tauri dev` startet die App, HUD erscheint, kein Rust-/Konsolen-Fehler
- [ ] Settings (`Strg+,`) → Backend-Adresse gesetzt → `/health` grün / Verbindung steht
- [ ] Text-Chat schickt eine Nachricht ans Mac-Backend und bekommt eine Antwort
- [ ] Mikro-Permission erteilt; Reden → Segment kommt am Backend an → Piper-Antwort
      wird abgespielt (Audio hörbar)
- [ ] Mind. ein Widget erscheint (Tool-Nutzung oder manuell `Strg+K`)
- [ ] `npm run tauri build` erzeugt ein `.msi`/`.exe` im `bundle/`-Ordner

## Konventionen (aus dem Repo)

- Tests: `npm test` (Vitest) + `npx tsc --noEmit` müssen grün bleiben — bei Frontend-
  Änderungen mitlaufen lassen.
- Tests-first ist im Repo etabliert (viele `*.test.ts` neben den Modulen). Für neue
  Windows-Fixes im Frontend: erst Test, dann Fix.
- Nichts am Backend/Mac ändern, um Windows zu fixen — der Client passt sich an, nicht
  umgekehrt.
