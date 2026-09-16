# Mantis — Roadmap

> **Ziel**: Ein lokaler, autonomer AI-Concierge der das eigene Leben managt —
> reaktionsschnell, proaktiv, selbst-verbessernd. *"Leben auf Autopilot."*

---

## 🧭 Was als Nächstes? — Ideen-Sammlung & Priorisierung (2026-07-09)

Erstellt nach dem großen Qualitäts-Pass (20 Module, 8 echte Bugs, Tests 380→593).
Zuerst die volle Ideen-Sammlung nach Thema, danach die Priorisierung nach **Effekt**
(größter positiver Hebel zuerst).

### A. Zuverlässigkeit & Betrieb *(der Quality-Pass hat diese Lücken aufgedeckt)*
- Branch `robot/x5-autonomy` → `main` mergen (**29 Commits** hängen fest, main 3 Tage alt)
- **CI**: die 593 Tests bei jedem Commit automatisch laufen lassen (GitHub Actions / pre-push-Hook)
- **Backup-Restore verifizieren** — Backups existieren, aber Restore ist ungetestet = falsche Sicherheit
- **Externer Trigger-/Not-Stopp-HTTP-Endpunkt** — löst Robot-Not-Stopp + Automationen (wiederkehrender Schmerz)
- **Bluetooth-Patch-Automatik** — Robot-BLE bricht still bei jedem Python-Update (Info.plist-Patch weg)
- **Type-Checking (mypy/pyright) + Linting (ruff) in CI** — fängt genau die Bug-Klasse aus dem Pass (falsche DB-Spalten, dict-vs-dataclass)
- **Fehler-Observability** ausbauen — die stillen `except:pass`-Degradationen sichtbar/alertbar machen
- Watchdog/Auto-Restart härten (launchd KeepAlive verifizieren, Heartbeat)
- Secrets-Hygiene (.env-Audit, gcal-Token-Rotation, nichts im Git)
- Abhängigkeiten pinnen + Update-Strategie
- **`refs/.DS_Store` aus den git-Refs entfernen** — Finder-Artefakt in `.git/refs/`,
  `git fsck` meldet `badRefName`/`badRefContent`. Harmlos, aber es verrauscht jede
  Integritätsprüfung; zusätzlich `.DS_Store` global in `.gitignore` + `core.excludesFile`
  absichern, damit es nicht wiederkommt (gefunden 2026-08-13)
- **Repo-Gesundheitscheck als wiederkehrende Prüfung** — am 2026-07-25 lagen drei stale
  Lock-Dateien (`index.lock`, `HEAD.lock`, `objects/maintenance.lock`, alle 0 Bytes) im
  Repo und blockierten **jeden Schreibvorgang für 19 Tage unbemerkt**. Braucht eine
  regelmäßige Prüfung (Locks ohne zugehörigen git-Prozess, `git fsck`) mit Meldung an
  Mantis (gefunden 2026-08-13)

### B. Voice-Pipeline *(tägliche UX, Latenz)*
- **Streaming LLM→TTS** durchgängig verketten — größter Latenz-Hebel (noch offen)
- Wake-Word (openWakeWord) — hands-free
- Silero VAD statt RMS-Schwelle
- TTS-Stimmen-Dropdown im Settings-Panel
- Chat-Agent-Modell überdenken (Ollama-primär vs. Claude — 166s-Erkenntnis)

### C. Gedächtnis & Intelligenz
- **Memory-Konfliktauflösung** — ADD-only akkumuliert widersprüchliche Fakten (Umzug → alter+neuer Wohnort koexistieren)
- Recall-Qualitäts-Eval-Harness (Retrieval-Präzision messen, Schwellen tunen)
- KG-Memory-Verlinkung sauber neu bauen (entferntes `link_memory` + echte Tabelle → `kg_linked` schützt wieder)
- Forgetting-Kurve-Tuning + Visualisierung (was wird vergessen?)
- Proaktiv-Timing lernen (wann ist Timo empfänglich?)

### D. Hardware
- X5 **Weck-Routine** (eingelernter Pfad → hinfahren, Alarm, auf "aus" zurück)
- X5 grobe Karte + Sperrzonen (Dead-Reckoning)
- X5 smarteres Explorieren statt Random-Wander
- Flipper: weitere IR-Geräte (TV, Klima) → nur `remotes.json`
- Flipper: Sub-GHz (433 MHz Steckdosen/Garage), NFC/RFID
- **Geräte-Abstraktion + Szenen** ("Guten Morgen" = Lampe an + Briefing + Robot)

### E. Autopilot & Proaktivität
- Die 5 wiederbelebten Autopilot-Features live kalibrieren (waren tot, jetzt aktiv)
- Szenen-/Routine-Engine (Morgen/Abend, Domains + Hardware kombiniert)
- Insight-Generierung schärfen (Dedup, Qualität)

### F. UI & Clients
- Windows-Tauri-Build (blockiert: Maschine/CI)
- Live-Tauri-Build + Relaunch verifizieren (Ghost-Protocol-v2 offen)
- Standort-/Karten-Widget
- Mobile-Apps-Politur (BodyOS/BrainOS/FlowOS)
- Dashboard-HTTPS (blockiert: Tailscale-Cert)

### G. Domänen & Integrationen
- Finanz-/Ausgaben-Domäne
- WhatsApp (blockiert: Meta Business API)
- Kalender-Konflikt-Auflösung ausbauen
- Google-Takeout-Location-Timeline (braucht Timos Datei)

### H. Modell & Kosten
- Lokale Modell-Latenz weiter senken
- Prompt-Caching (Claude) für Kosten
- Eigenes Fine-Tuned MLX-Modell (blockiert: Compute)

### I. Developer Experience
- Tests für die Glue-Module (skills, prompt_builder, reflection, consolidator)
- `pytest-cov` einführen → Lücken systematisch finden
- Architektur-/Onboarding-Doku

---

### 🏆 Priorisierung nach Effekt

**Tier 1 — ✅ ERLEDIGT (2026-07-09)**
1. ✅ **Branch → main mergen** — 30 Commits per Fast-Forward auf main, gepusht.
2. ✅ **CI: Tests automatisch** — GitHub Actions (pytest, in frischem venv validiert), grün.
3. ✅ **Backup-Restore verifizieren** — Restore in Wegwerf-DB + Sanity-Check, wöchentlich automatisch, live bewiesen.
4. ✅ **Externer Trigger-/Not-Stopp-Endpunkt** — POST /api/trigger/estop (Token), 0.04s.
5. ✅ **Bluetooth-Patch-Automatik** — Crash-Schutz (erkennt fehlendes Entitlement) + scripts/fix_bluetooth.sh.

**Tier 2 — teilweise erledigt**
6. **Streaming LLM→TTS** — größter spürbarer Latenz-Hebel. ⏸ Braucht Live-Audio-Test (mit Timo am Mikrofon).
7. ✅ **Linting in CI** — ruff-Gate (F/E9), Codebase bereinigt (601 tote Imports, 1× F821 gefunden). Type-Checking (mypy) noch offen (Code untypisiert → separater Annotations-Aufwand).
8. ✅ **Fehler-Observability** — WARNING/ERROR-Logs fließen dedupliziert ins Dashboard-Fehler-Widget.
9. **Wake-Word + Silero VAD** — hands-free. ⏸ Braucht Live-Audio-Test.
10. ✅ **Memory-Konfliktauflösung** — überholte Fakten werden abgewertet statt akkumuliert (ADD-only-treu).

**Tier 3 — Später (konkrete Features, klarer Nutzen)**
11. X5 Weck-Routine — der emotional greifbarste HW-Use-Case.
12. Szenen-/Routine-Engine — verbindet HW + Domains zu echtem "Autopilot".
13. Flipper: weitere IR-Geräte — billig (nur remotes.json), sofort nützlich.
14. X5 Karte + Sperrzonen — Voraussetzung für sichere Autonomie.
15. start.sh-PID-Bug sauber (klein, schon halb adressiert).
16. TTS-Stimmen-Dropdown, Recall-Eval-Harness, KG-Memory-Verlinkung, Insight-Schärfung.

**Tier 4 — Nice-to-have / extern blockiert**
- Windows-Build (Maschine/CI), Dashboard-HTTPS (Cert), WhatsApp (Meta), Fine-Tuned MLX (Compute),
  Standort-Widget, Finanz-Domäne, Location-Timeline.

> **Leitgedanke der Rangfolge**: erst *nichts verlieren* (Merge, Backup-Restore), dann *nicht still
> kaputtgehen* (CI, Typen, Observability, BT-Patch), dann *täglich besser* (Voice-Latenz, Memory-Qualität),
> dann *neue Fähigkeiten* (HW-Szenen). Blockierte Punkte warten auf externe Voraussetzungen.

---

## Fertig ✅

### Agent-Kern
- ReAct-Agent mit nativem Tool-Calling (57 Tools)
- LLM-Routing: Claude Haiku (Chat) + Ollama qwen3.5:9b (Background)
- Ollama + Claude API Backends (austauschbar)
- Streaming: Token-Streaming an Telegram + Dashboard (SSE)
- `_safe_task()` — alle Background-Tasks mit Fehlerbehandlung
- Subagent Delegation (`delegate_task`) — isolierter Kind-Agent, eigener Context, Timeout
- LLM-Fallback: Claude API ausgefallen → lokales Ollama übernimmt transparent
  (90s-Timeout, 180s-Cooldown, Ausfall im Fehler-Widget sichtbar)
- Generatives UI (Phase 2, erster Durchstich): Tool-Aufrufe (get_health) lösen automatisch
  Widgets im Tauri-Desktop-Client aus — deterministische Tool→Widget-Zuordnung
  (core/ui_state.py), neuer SSE-Kanal (/api/ui/stream, /api/ui/current), Sleep-Widget
  ersetzt den HUD-Ring bei Treffer und schaltet bei irrelevanten Turns zurück
- Generatives UI (Phase 3, Layout-Vorlagen + explizite Tools): Multi-Slot-Layout-Fundament
  (LAYOUT_PRESETS: single/split2), Agent-Tools show_widget/arrange_screen/close_widget für
  Fälle mit echtem Urteilsbedarf, Frontend rendert mehrere Widgets gleichzeitig im Grid
- Generatives UI (Phase 4, Widget-Bibliothek): 6 Widget-Typen (sleep/training/tasks/calendar/
  nutrition/habits), konsolidierte build_widget_payload()-Dispatch-Funktion, generisches
  Frontend-Rendering (Balken für sleep/training, Listen für tasks/calendar/habits)
- Sprach-Erfassungs-Messversuch (Phase 5a, isolierter Spike): core/voice.py bündelt
  Whisper-Transkription + Adress-Check (aus Telegram-Code extrahiert), neuer Endpunkt
  POST /api/voice/segment, Tauri-Client erfasst Mikrofon-Audio mit einfacher
  RMS-Lautstärke-VAD (kein ML, kein Wake-Word), zeigt Transkript + Adressierungs-Status
  im HUD — noch NICHT an den Agent/Chat-Loop angebunden, dient nur der Messung von
  Latenz/Trefferquote vor dem vollen Ausbau
- Versteckte manuelle Navigation (Phase 6): Cmd/Ctrl+K öffnet ein Vollbild-Grid mit den 6
  existierenden Widget-Typen im Tauri-Client, Klick setzt das gewählte Widget direkt als
  Hauptscreen — neuer Endpunkt POST /api/ui/select (kein Agent/LLM involviert, reiner
  Fallback für manuelle Navigation ohne Sprache/Text)

### Gedächtnis-Architektur
- PostgreSQL + pgvector Langzeitgedächtnis
- Knowledge-Graph (kg_entities + kg_relations)
- KZG Rolling Checkpoints (MemGPT-Muster, kein hartes Abschneiden)
- ADD-only Memory Writes (kein stale-update-Bug)
- Recall Gate (Jaccard-Heuristik vor pgvector-Lookups)
- Multi-Signal Retrieval (pgvector + Keyword-BM25 fusioniert)
- Temporales Retrieval-Scoring ("heute" vs. "letzten März")
- Warm Profile Injection (KG-User-Entitäten 90s gecacht, top of system prompt)
- Verification-Bump (Ebbinghaus-Stabilität steigt bei Bestätigung)
- Importance Triage (recall_count ≥ 3 → +0.1, nie recalled + 14d → -0.05)

### Selbst-Verbesserung
- Skill-Factory (Python-Skills zur Laufzeit, AST-validiert, Git-committed)
- **Background Review Loop** (Hermes-Pattern): Fork-Agent nach jedem Turn, `BE ACTIVE`-Direktive
- **SKILL.md Prozedur-System**: Natürlichsprachliche Skills mit Frontmatter, Trigger-basierte Injektion
- Reflexions-Engine: Ton/Stil-Anpassung, Längen-Kalibrierung
- Proactive Engagement Decay: Frequenz sinkt wenn Nachrichten ignoriert werden
- Embedding-basiertes Tool-Routing (TF-IDF Semantic Fallback)
- Tool Discovery Escape Hatch (`refresh_tools`)
- Claude Code Subprocess: "Mantis, bau X" → spawnt `claude`-Subprocess

### Autopilot
- Morgen-Briefing, Abend-Review, Wöchentlicher Rückblick
- Tägliche KI-Reflexion 22 Uhr (Wins, Risiken, Muster → brain_notes)
- Workout-Empfehlung basierend auf HRV + Schlaf
- Smart Notifications (fällige Tasks + Habit-Lücken, mittags)
- Wetterbasiertes Coaching (morgens, Outdoor-Empfehlung)
- Wöchentliche Themen-Recherche (Montags-Autopilot)
- Personal Newsletter (Freitags-Digest via Telegram)
- Monitoring: Uptime-Check + Push-Notification bei Fehlern

### Domänen
- HealthKit Background-Push von iOS (kein HTTP-Server mehr)
- Health-Feldmapping vollständig (HR-Min/Avg/Max, HRV, SpO₂, Schlaf-Stages)
- Workouts aus HealthKit-Push (Swift workouts-Array → fitness.log_workout)
- AlphaProgression (HRV/Schlaf-basierte Gewichtsempfehlung)
- Körpermessungs-Tracking (Umfänge, body_measurements Tabelle)
- Ernährung: adaptive Ziele (BMR × Aktivität + Gewichtstrend-Regression)
- Google Calendar (lesen + schreiben)
- Voice-Transkription in Telegram (Whisper lokal)
- Foto-Analyse in Telegram (llava:7b, Mahlzeiten-Erkennung)
- Second Brain (brain_notes, Kategorien, Wiki-Links, Graph-View)
- Quotes mit evolving Thoughts (Gedanken über Zeit anfügen)
- Kindle-Highlights-Import (My Clippings.txt → Quotes)
- Web-Scraping + YouTube/Artikel-URL-Zusammenfassung
- Git-History als Gedächtnis (Commit-Log → brain_notes)
- Periodische Themen-Recherche + Research-Query-Skill

### Dashboard & Infrastruktur
- PWA (14 Views, installierbar, Service Worker)
- Dark/Light Mode, Pull-to-Refresh, Mobile-UX
- Memory-Viewer (Diary, Knowledge-Graph vis.js, Mahlzeiten)
- Top-3 Tages-Fokus, Daily Resurfacing, Slipping Tasks
- Web Push (VAPID) + Push-Templates + Dedup-Filterung
- Eval-Suite (6 Test-Cases, `/api/eval/run`)
- Mantis als MCP-Server (stdio JSON-RPC + HTTP `/mcp/`)
- Eval-Suite v2: echter Agent-Lauf (prompt_builder + agent.run), Dry-Run-Tools,
  must_call_tool-Check, Timeout pro Case — deckte den warm_profile-Crash auf
- LLM-Usage-Tracking: Token + Kosten pro Call (`llm_usage`), `/api/usage`,
  API-Kosten-Karte in Analytics, `api_costs`-Tool
- warm_profile-Fix (literales % in SQL crashte jeden Prompt-Build) +
  prompt_builder degradiert bei Kontext-Fehlern statt den Turn zu killen
- Batch-Embeddings (multi-text in einem Ollama-Call)
- Automatischer Start nach Neustart (launchd + KeepAlive)
- Automatische DB-Backups
- Neues UI-Design (dunklere Tokens, cleaner Home, bessere Typografie)

### Native iOS-Apps (SwiftUI)
- **BodyOS** — Training (Mantis-Sessions aus HRV/Schlaf) + Ernährung (Foto-Makros) + HealthKit-Push
- **BrainOS** — Second Brain: Wiki-Links, Force-directed Graph (SwiftUI Canvas, eigene Physik)
- **FlowOS** — Tasks + Kalender + Habits + Fokus-Timer
- Gemeinsamer `MantisClient` pro App: `waitsForConnectivity` + Retry → übersteht Tailscale-Aussetzer
- Details in `apps/README.md` (konsolidiert aus ursprünglich 5 geplanten Apps)

### Code-Architektur
- Orchestrator aufgeteilt: `prompt_builder`, `message_handler`, `idle_loop`
- `web/api.py` in Router-Module pro Domäne gesplittet (`web/routers/`)
- `core/skills.py` in Kategorie-Pakete gesplittet (`core/skills/`); CTX in `core/skill_context.py` (mutate-bind)
- GC-sichere Background-Tasks (`_bg_tasks` Liste mit Referenzen)
- DB-Fehler beim Start bricht Start ab (kein stiller Datenverlust)
- `api.py` nutzt `orch.chat_llm` direkt (toter `self.llm`-Alias entfernt)
- Dashboard bindet auf `0.0.0.0` — Tailscale-Drop kann Server nicht mehr auf localhost zwingen
- Embedding-Modell `qwen3-embedding:0.6b`; Python-Pfad in `start.sh` dynamisch aufgelöst

---

## In Arbeit — autonome Nachtsession (2026-07-05)

> Timo schläft, Auftrag: "Revolutioniere Mantis" — Stimme, Adress-Erkennung,
> Desktop-App groß ausbauen (Jarvis-Look, modulare Screens, Vollzugriff),
> Windows-Readiness, Jarvis-Feature-Parität recherchieren. Durchgehend
> autonom bis Nutzungslimit, Fortschritt hier laufend aktualisieren.

### Sofort-Fixes (hohe Priorität, konkret gemeldete Bugs)
- [x] **Adress-Erkennung ignoriert Folge-Antworten** — behoben (Commit 9d3670d):
  `mark_conversation_active()` öffnet ein 15s-Fenster nach jeder Mantis-Antwort,
  in dem Folge-Sprache ohne Namen/klaren Befehl automatisch als adressiert gilt.
- [x] **TTS-Engine-Wahl abgeschlossen für diese Session** — Piper
  (de_DE-thorsten-high) gewählt: 0.7s Laden, ~1.6s Synthese, native deutsche
  Aussprache. Getestete Alternativen: Kokoro (kein Deutsch), Chatterbox-
  Multilingual (Deutsch ja, aber 7-12s Latenz — zu langsam), CosyVoice2
  (PyPI-Paket kaputt, Build-Fehler). Weitere "Jarvis-klingendere" Stimmen
  bleiben ein offener Punkt für später, aber kein blockierender Task mehr —
  Piper ist zuverlässig und schnell genug für den Live-Betrieb.

### Desktop-App: großzügiger Ausbau (Jarvis-Look + Funktionen)
- [x] Chat-Texteingabe im HUD (Commit f3a3836) — Eingabefeld unten im
  Fenster, sendet an POST /api/chat, Antwort im chat-status-Bereich
- [x] Holographic-HUD-Feinschliff (Commit a7e3eb5, Sound-Feedback in
  Commit 3912bf5): Scanline-Animation, Grid-Textur-Hintergrund, Vignette,
  Ring-Puls-Animation + synthetisierte Töne (Web Audio API) bei
  Widget-Wechsel, Alerts/Tool-Fehlern, adressierter Sprache.
- [x] **Modulare/adaptive Screens großzügig ausgebaut** (Commit e13d801 +
  vorherige Widget-Commits): Widget-Bibliothek von 6 auf 10 Typen erweitert
  (system/brain/skills/weather neu), Fade-In/Scale-Übergangsanimation bei
  jedem Widget-Wechsel. Layout-Wahl (single/split2) + Mantis' eigene
  Entscheidung welches Widget wann erscheint bestand schon aus Phase 2-4.
- [x] **Mantis volle Rechte** (Commits b7247d8, 72f25e0): 5 neue Agent-Tools
  (read_any_file/write_any_file/list_directory/open_path/open_app) —
  Zugriff auf das GESAMTE Mac-Dateisystem, nicht nur Mantis' eigene Codebase.
  Bewusst OHNE Lösch-Tool. Live-Test deckte einen Folgefehler auf: das
  Keyword-Tool-Routing (core/tools.py::select_tools) kannte die neue
  Kategorie 'filesystem' nicht — Datei-Anfragen fielen in den 'reines
  Gespräch'-Fast-Path und bekamen die neuen Tools nie angeboten. Gefixt,
  end-to-end live verifiziert (list_directory ~/Desktop → echte Dateiliste).
- [x] Zwei neue Widget-Typen (Commit 92a863f): **system** (CPU/RAM/Ollama-
  Status über psutil) und **brain** (zuletzt bearbeitete Second-Brain-Notizen)
  — beide über Cmd/Ctrl+K manuell wählbar und via build_widget_payload()
- [x] Skill-Factory-Status-Widget (Commit 23636a8): zeigt selbst erstellte
  Skills + Gesamt-Tool-Anzahl, live verifiziert
- [x] **Wetter-Karten-Widget** (Commit bd4881e): build_widget_payload() +
  maybe_update_ui() auf async umgebaut (inspect.iscoroutinefunction erkennt
  automatisch sync/async Builder), damit domains.weather.get_weather() (Live
  Open-Meteo-API) als Widget läuft. Live verifiziert (echte Nürnberg-Daten).
- [x] **Second-Brain-Graph-Widget** (Commit 4231c9b): visuell statt nur
  Liste, nutzt bereits bestehendes domains.second_brain.get_graph_data(),
  SVG-Kreis-Layout im Frontend, live verifiziert.
- [ ] Weitere Widget-Typen: Standort/Karte (keine klare Datenquelle bekannt)
- [x] Benachrichtigungs-/Alert-Overlay (Commit 1b3b38b): Autopilot._send()
  emittiert jetzt auf den bestehenden StatusBus, Desktop-HUD zeigt jede
  proaktive Nachricht (Morgen-Briefing, Smart Notifications, ...) als
  stapelbaren 12s-Toast oben rechts
- [x] Persistente Fenster-Position/-Größe, Tray-Icon, Autostart (Commit
  c7ef01a): tauri-plugin-window-state, tauri-plugin-autostart, Tray-Icon
  mit Show/Quit-Menü. Build erfolgreich; volle visuelle Verifikation der
  Tray-Interaktion steht noch aus (Bildschirm war zum Testzeitpunkt gesperrt).

### Windows-Readiness
- [x] **Architektur-Klarstellung + Assessment (Recherche abgeschlossen):**
  Backend (Whisper/Piper/Ollama/DB) bleibt laut Spec zentral auf dem Mac —
  Windows ist ein **reiner Client**, braucht also KEIN Python/whisper.cpp/
  Piper lokal. Das vereinfacht Windows-Readiness auf:
  1. **Tauri-Windows-Build** — kann in diesem (macOS-only) Environment nicht
     nativ gebaut/getestet werden. Braucht entweder eine echte Windows-
     Maschine oder einen Windows-CI-Runner (z.B. GitHub Actions
     `windows-latest`). `tauri.conf.json` hat bereits `.ico`-Icon hinterlegt,
     `bundle.targets: "all"` sollte auf Windows automatisch MSI/NSIS bauen.
  2. **Mikrofon-Berechtigung** — anders als macOS TCC/Info.plist läuft das
     unter Windows über WebView2s Standard-Berechtigungsdialog (kein
     Manifest-Eintrag nötig, sollte "einfach funktionieren" — aber ungetestet).
  3. **MediaRecorder-Format** — bereits robust: voice-capture.ts liest
     `recorder.mimeType` dynamisch aus statt einen Wert fest anzunehmen
     (siehe Fix aus der Voice-Capture-Session), Chromium/WebView2 liefert
     vermutlich `audio/webm;codecs=opus` statt macOS' `audio/mp4` — beides
     wird bereits korrekt behandelt.
  4. **Backend-Adresse konfigurierbar gemacht** (Commit c5db573): neues
     Cmd/Ctrl+,-Einstellungs-Panel, damit ein Windows-Client die Tailscale-
     Adresse des Mac-Backends eintragen kann, statt nur per Devtools-Hack.
  5. **CORS bereits gefixt** (aus Voice-Capture-Session) — cross-origin von
     einer anderen Maschine funktioniert bereits (`allow_origins: "*"`).
  6. **Nicht behoben/offen:** Code-Signing/Notarization für Windows (explizit
     Nicht-Ziel der Master-Spec), echter Build+Test auf einer Windows-Maschine.

### Jarvis-Feature-Parität (Recherche + Implementierung)
- [x] **Web-Recherche abgeschlossen:** Die meisten kanonischen Jarvis-
  Fähigkeiten (Anzug-Steuerung, Energiequellen-Scan, House-Party-Protocol)
  sind fiktionsspezifisch/kampfbezogen und nicht sinnvoll übertragbar. Die
  realistisch übertragbaren Kernmerkmale — natürlichsprachliche Konversation,
  proaktive Analyse/Warnungen, Multi-Tasking im Hintergrund, visuelle
  Datenanzeige — deckt Mantis über Autopilot/Reflexions-Engine/Background-
  Review-Loop/HUD bereits ab oder sie stehen schon im Brainstorm unten
  (Smart-Home, Screen-Context-Awareness). Kein neuer Feature-Fund, der nicht
  schon anderswo in dieser Roadmap steht.
- [x] Gefundene, machbare Lücken priorisiert und umgesetzt — siehe Brainstorm-
  Liste unten (Screen-Context-Awareness umgesetzt, Kalender-Konflikt-Erkennung
  bestand bereits, Emotionserkennung recherchiert+bewusst zurückgestellt).

### Weitere gesammelte Ideen (Brainstorm, nicht priorisiert)
- [ ] Smart-Home-Steuerung (falls HomeKit/HomeAssistant vorhanden — prüfen)
- [x] **Screen-Context-Awareness** (Commits cc0852b, 05a6760, b694472): neues
  Tool `see_screen` — macOS `screencapture` (core/skills/vision.py) +
  Ollama-Vision (core/vision.py, aus Telegram-Foto-Analyse extrahiert und
  zentralisiert). Zwei Live-Bugs gefunden und gefixt: (1) `screencapture` lag
  in /usr/sbin, das der launchd-Prozess nicht im PATH hat → absoluter Pfad;
  (2) Tool-Routing kannte die Kategorie 'vision' nicht (derselbe Bug-Typ wie
  bei filesystem) → Keywords ergänzt. **Verbleibende Blockade (braucht Timo):**
  macOS verweigert den Screenshot mit "could not create image from display" —
  fehlende Bildschirmaufnahme-Berechtigung (Privacy & Security). Das ist eine
  Systemeinstellung, die ich nicht selbst ändern darf/kann — Mantis gibt jetzt
  eine konkrete Anleitung dazu aus, aber Timo muss die Berechtigung einmalig
  manuell erteilen und Mantis neu starten, damit das Tool nutzbar wird.
- [x] **Kalender-Konflikt-Erkennung** — existierte bereits (`_calendar_check()`
  in core/autopilot.py, nutzt domains/calendar_optimizer.py::analyze_day),
  läuft jeden Morgen proaktiv, keine Umsetzung nötig gewesen.
- [x] **Sprach-Emotionserkennung recherchiert, bewusst zurückgestellt:**
  DistilHuBERT/emotion2vec sind zwar klein (~19M Parameter), aber nur über
  die schwere FunASR/ModelScope-Toolchain ladbar (kein eigenständiges PyPI-
  Paket) — ähnliches Risiko wie der gescheiterte Chatterbox-Versuch (siehe
  TTS-Wahl oben), der starlette/transformers durcheinandergebracht hat. Bei
  "nur wenn leichtgewichtig verfügbar" die Kosten-Nutzen-Abwägung nicht wert;
  aufgeschoben statt eine fragile schwere Abhängigkeit zu riskieren.
- [x] Multi-Turn-Konversations-Historie im HUD (Commit 7b265a9):
  conversation-log.ts zeigt die letzten 12 Turns (Nutzer + Mantis) oben
  links, statt nur den letzten Austausch zu überschreiben
- [x] **Fehler-Selbstheilung Stufe 1** (Commit cb28517): core/tools.py::execute()
  zählt aufeinanderfolgende Fehlschläge pro Tool, ab 3x wird 'tool_failure'
  auf den StatusBus emittiert → bernsteinfarbener Warn-Toast im Desktop-HUD.
  Bewusst OHNE automatisches Auto-Fixing (zu riskant ohne Aufsicht) — reine
  Erkennung + Sichtbarkeit, damit Timo es selbst reparieren/create_skill
  nutzen kann.

---

## UI-Polish-Pass (2026-07-05) — "Ghost Protocol"

Spec: `docs/superpowers/specs/2026-07-05-ui-polish-design.md`
Plan: `docs/superpowers/plans/2026-07-05-ui-polish-ghost-protocol.md`

- [x] CSS-Token-System (`:root`-Block in `style.css`)
- [x] `motion.ts` — `tweenNumber`-Helper für animierte Zahlenwerte
- [x] `.charge-pulse` — gemeinsame State-Transition-Animation
- [x] `system`-Widget: Text → animierte Radial-Gauges (CPU/RAM/Ollama)
- [x] `nutrition`-Widget: Text → Makro-Gauges
- [x] `sleep`/`training`: Token-Migration + Charge-Pulse bei Update
- [x] `brain_graph`: Token-Migration + Hover-Feedback auf Nodes
- [x] `tasks`/`habits`: Status-Akzente (Inline-Progress-Bar, Streak-Dot) statt reinem Text/Emoji
- [x] Nicht-Widget-UI (Nav-Overlay, Chat-Input, Settings, Alert-Overlay, Conversation-Log):
      Token-Migration, neuer `--c-error`/`alert-error`-Zustand ergänzt (existierte vorher nicht)
- [ ] `calendar`/`brain`/`skills`/`weather`-Listen-Widgets: nur Token-Migration übernommen
      (kein individuelles Redesign vorgesehen, sind bereits als Listen die richtige Darstellung)

---

## Ghost Protocol v2 — Cinematic HUD (2026-07-05)

Spec: `docs/superpowers/specs/2026-07-05-ghost-protocol-v2-cinematic-hud-design.md`
Plan: `docs/superpowers/plans/2026-07-05-ghost-protocol-v2-cinematic-hud.md`

- [x] Shared framework: particle field, panel-chrome corner brackets, bespoke SVG icon set,
      staggerIn/drawIn motion choreography, depth/bracket/blur tokens
- [x] HUD core: radar bezel, particle halo, chrome info panel
- [x] System-Status/Nutrition gauges: panel chrome, greeble min/max readouts, icons
      (needle-sweep + load-based particle tint from the spec's §3 were descoped — no shared
      per-widget RAF abstraction existed for it and no acceptance criterion required it)
- [x] Second Brain: animated graph edges + pulsing node halos, staggered list
- [x] Sleep/Training/Tasks/Calendar: chrome, icons, staggered rows, liquid-fill bars
- [x] Habits/Nutrition/Skills/Weather: icons, streak-to-milestone ring, per-condition weather icon
- [x] Nav overlay: chrome-framed tiles, staggered grid on open
- [x] Settings panel: connection-status dot
- [x] Chat reply / alert toasts: icon set replacing emoji
- [ ] Live Tauri build + relaunch verified

---

## Voice-Sensitivity, TTS-Stimmen, Wetter-Radar-Karte (2026-07-05)

Spec: `docs/superpowers/specs/2026-07-05-voice-tts-weather-radar-design.md`
Plan: `docs/superpowers/plans/2026-07-05-voice-tts-weather-radar.md`

- [x] VAD-Schwellwert kalibriert sich jetzt selbst aus dem Umgebungsrauschpegel statt eines
      festen Werts (`computeCalibratedThreshold` in `voice-capture.ts`)
- [x] Drei zusätzliche männliche Piper-Stimmen heruntergeladen und über die
      `tts_voice`-Einstellung umschaltbar (thorsten_emotional-medium, karlsson-low,
      pavoque-low) — Auswahl bisher nur per direktem Settings-Aufruf, kein UI-Dropdown
- [x] Wetter-Widget zeigt jetzt eine OSM-Karte mit RainViewer-Regenradar-Overlay
      (letzte 4 Frames, ~800ms-Loop) statt der reinen Text-/Forecast-Liste
- [ ] TTS-Stimmen-Auswahl im Settings-Panel als Dropdown (Folgearbeit, sobald Timo eine
      Favoritenstimme gewählt hat)

---

## X5-Roboter — physischer Körper (2026-07-09, pausiert)

Spec: `docs/superpowers/specs/2026-07-07-x5-robot-autonomy-design.md`
Protokoll: `docs/robot/protocol.md` · Code: `tools/robot/`, `core/skills/robot.py`
Branch: `robot/x5-autonomy` (noch nicht gemergt) · Gedächtnis: `[[mantis-x5-robot]]`

**Fertig:**
- [x] BLE-Protokoll des Clementoni RoboMaker X5 (`EVRobot2`) komplett per Reverse-
      Engineering entschlüsselt (Il2CppDumper + capstone, nicht Sniffing) — Motoren,
      Sensoren (ir0=vorne/ir1=hinten/Greifer-Druck), Sound. Ohne Clementoni-App.
- [x] Treiber + Tools: `protocol.py` (rein, 12 Tests), async `driver.py`, dauerhafter
      `manager.py` (Auto-Reconnect), 3 Mantis-Tools (`robot_control`/`robot_sensors`/
      `robot_autonomy`). 41 Tests grün.
- [x] Autonomer Fahrmodus mit geschlossener Ausweich-Regelung (dreht bis Front frei,
      `max_turn_bursts`, rear-bewusster Rückzug) — behob das "dreht nur minimal, schaut
      Wand weiter an"-Problem.
- [x] Tool-Routing-Fix (`robot`-Kategorie in `core/tools.py::_CATEGORY_KEYWORDS` fehlte).
- [x] macOS-Bluetooth-Crash gefixt: `NSBluetoothAlwaysUsageDescription` in Python.app-
      Info.plist ergänzt + ad-hoc neu signiert (Backup `~/mantis-python314-Info.plist.
      pre-bluetooth.bak`). **Achtung: bei Python-Update weg → erneut patchen.**

**Offen (später weiter):**
- [ ] **Live-Verifikation des Bluetooth-Fixes** — Roboter-Befehl aus der App auslösen,
      macOS-BT-Dialog erlauben, bestätigen dass X5 fährt und Mantis nicht crasht.
- [ ] **Weck-Routine**: eingelernter Pfad → morgens zu Timo fahren, Alarm-Ton im Loop,
      auf "aus" Ton stoppen + zurückfahren.
- [ ] **Grobe Karte + Sperrzonen**: Dead-Reckoning-Occupancy-Grid (kein Odometer → driftet),
      Timo kann Bereiche manuell sperren.
- [ ] **Smarteres Explorieren** statt reinem Random-Wander.
- [x] **Not-Stopp-Endpunkt** ohne Mantis-Neustart — erledigt: POST /api/trigger/estop (Tier-1 #4).
      (Aktuell: Neustart droppt BLE → Motoren
      bremsen in <1s von selbst, aber unsauber).
- [ ] `start.sh` PID-Bug (liest `/tmp/mantis.pid`, schreibt `/tmp/mantis_pid.txt`) fixen.
- [ ] Branch `robot/x5-autonomy` mergen.

---

## Flipper Zero — IR/Smart-Home-Aktoren (2026-07-09)

Code: `tools/flipper/`, `core/skills/flipper.py` · Gedächtnis: `[[mantis-flipper]]`

**Fertig:**
- [x] **Schreibtischlampe per IR an/aus** — Flipper-Fernbedienungssignale per `ir rx`
      angelernt (NECext EF00: AN=FC03, AUS=FD02), Mantis sendet sie über die Flipper-
      USB-Serial-CLI (`ir tx`). Tool `lampe` (an/aus) + generisches `flipper_ir`.
      Geräte-Registry `remotes.json` (neue Geräte ohne Code-Change). 5 Tests grün,
      End-to-End live verifiziert. Kein Bluetooth → kein TCC-Problem wie beim X5.

**Offen / Ideen:**
- [ ] Weitere IR-Fernbedienungen anlernen (TV, Klimaanlage) → nur `remotes.json` ergänzen.
- [ ] Sub-GHz (433 MHz Funksteckdosen/Garagentor), NFC/RFID als weitere Aktoren prüfen.
- [x] Flipper-Arbeit committen (erledigt: Commit 56c4c57).

---

## Ventilator-IR + Gmail (Vorbereitung, 2026-07-10)

Code: `core/skills/ventilator.py`, `tools/flipper/remotes.json`; `domains/gmail_client.py`,
`core/skills/email.py`, `scripts/gmail_auth.py` · Spec: `docs/superpowers/specs/2026-07-10-fan-gmail-prep-design.md` ·
Setup: `docs/fan-gmail-setup.md`

**Fertig (Scaffolding, Tests grün, degradiert live sauber):**
- [x] **Ventilator per Flipper-IR** — Tool `ventilator` (an/aus/stärker/schwächer + schritte) +
      Fast-Paths, klont Lampen-Muster. remotes.json-Platzhalter (`learned:false`) → „nicht
      angelernt"-Meldung, bis Timo die Codes einträgt.
- [x] **Gmail** — `gmail_client.py` (OAuth-Spiegel von gcal, Scope modify+send): list/search/
      read/mark_read/archive/send. `email_*`-Tools; **Senden über Entwurf-Gate** (confirm).
      `scripts/gmail_auth.py` (Port 8082). Fast-Path für „neue mails?" → email_unread.

**Offen (braucht Timos Daten — siehe `docs/fan-gmail-setup.md`):**
- [ ] Ventilator: echte IR-Codes aufnehmen, in remotes.json eintragen (`learned:true`).
- [ ] Gmail: Gmail-API in der Cloud Console aktivieren, `python scripts/gmail_auth.py` ausführen.

---

## News-Globus + OSM (2026-07-10)

Code: `tools/geo/nominatim.py`, `tools/news/` (feeds + geolocate), `core/news_globe.py`,
`web/routers/globe.py`, `core/skills/geo.py`, Globus/Karten-Views in `web/index.html` ·
Spec: `docs/superpowers/specs/2026-07-10-news-globe-design.md`

**Fertig (Code + Tests, 740 grün; Pipeline live verifiziert):**
- [x] **Nominatim-Client** (Geocoding + Ortssuche, Cache, Policy-Drossel, ASCII-User-Agent).
- [x] **RSS/Atom-Aggregator** (feedparser, Dedup, HTML-Clean).
- [x] **Geolokalisierung** — Ort aus Schlagzeile via qwen → Nominatim → Koordinate, gecacht.
      Live getestet: Spanien/Philippinen/Südafrika/Houston korrekt erkannt (5/6 verortet).
- [x] **News-Globus-Aggregator** (`refresh`/`cache`) + **stündlicher Idle-Loop-Tick**.
- [x] **API**: `/api/globe/news`, `/api/geo/search` (beide live). Agent-Tools `where_is`, `news_briefing`.
- [x] **Globus-View** (globe.gl) mit News-Punkten + Klick-Popup + Ortssuche-Anflug.
- [x] **2D-Karte** (Leaflet, OSM-Kacheln, circleMarker) als eigene View.
- [x] Config-Defaults (`data/news_sources.json` bzw. `news_sources.example.json`).

**Offen:**
- [ ] **Visueller Check** von Globus + Karte im Browser (Render, Interaktion) — nur mit Timos Augen.
- [x] Themen-News (`topics`) über Google-News-RSS anbinden; werden wie bestehende
      Quellen geolokalisiert und im Globus-Cache berücksichtigt.
- [ ] Globe.gl/Leaflet per CDN eingebunden (wie chart.js/vis-network); bei Bedarf lokal vendored.

**Später (eigener Spec):** Karten-Analysen.

---

## UI-Automatik + Spicetify-Bridge (2026-07-10)

Code: `tools/uiauto/` (engine + safety), `core/skills/uiauto.py`, `tools/spotify/bridge.py`,
`tools/spotify/spicetify_ext/mantis-bridge.js`, `web/routers/spicetify.py` ·
Spec: `docs/superpowers/specs/2026-07-10-ui-automation-design.md` · Setup: `docs/ui-automation-setup.md`

**Fertig (Code + Tests, 718 grün):**
- [x] **Generelle UI-Automatik** über die Accessibility-API (OSS-Engine **atomacos**,
      statt selbst gebaut). Tool `computer_task(goal, app)` startet einen ReAct-Loop
      auf **qwen3.5:9b** (stärker als gemma) mit den Low-Level-Tools ui_inspect/
      ui_click/ui_type/ui_key. Electron-Apps werden per `AXManualAccessibility` geweckt.
- [x] **Rote Linien** (`tools/uiauto/safety.py`): destruktive/riskante Elemente
      (Löschen/Senden/Kaufen/Passwortfelder) werden in der Primitivschicht hart
      verweigert — nicht dem Modell überlassen. Token-Matching (keine Teilwort-Fehler).
- [x] **Fast-Path** für computer_task (gemma wählte es sonst nicht).
- [x] **Spicetify-Bridge**: Extension (`mantis-bridge.js`) verbindet sich per WebSocket
      mit `/spicetify/ws`, liefert strukturierte Suche/Track über Spotifys interne APIs
      (`CosmosAsync`/`Player`) — kein Developer-Key nötig. spotify-Skill bevorzugt die
      Bridge, fällt sonst auf AppleScript/Web-API zurück.
- [x] **Engine-Lese-Pfad live verifiziert** (182 echte AX-Elemente gelesen, read-only).

**Offen (braucht Timo am Mac — siehe `docs/ui-automation-setup.md`):**
- [ ] Bedienungshilfen-Recht für den Mantis-Python-Prozess vergeben → Schreib-Pfad live.
- [ ] Spicetify-Extension installieren (`spicetify apply`) → Bridge-Handshake live.
- [ ] qwen-Loop beim ersten echten Einsatz tunen (Element-Wahl, Schrittzahl).

**Bewusst NICHT gebaut:** Vision-Fallback (Screenshot + Koordinaten-Klick) — beide
gebauten Wege liefern strukturierte Daten; Vision bleibt späterer Notnagel.

---

## Spotify-Steuerung (2026-07-10)

Code: `tools/spotify/` (applescript + web_api), `core/skills/spotify.py` ·
Spec: `docs/2026-07-10-spotify-control-design.md`

**Fertig:**
- [x] **Playback lokal per AppleScript** — Tool `spotify` mit play/pause/next/previous,
      Lautstärke, status („was läuft"), spiel („spiel [X]" via Web-API-Suche). Trotz
      Spicetify voll steuerbar (nur UI gepatcht). 27 Tests grün, End-to-End live
      verifiziert (echter Track, next, pause).
- [x] **Deterministische Fast-Paths** — play/pause/next/prev, status UND „spiel [X]"
      laufen ohne LLM: Live-Test zeigte, dass gemma bei status Erfolg vortäuscht und
      „spiel [Song]" gar nicht als Tool anbietet. Status wird vor dem Fragezeichen-Guard
      gefangen, „spiel [X]" extrahiert den Suchbegriff selbst.

**Offen:**
- [ ] **Spotify-Web-API-Credentials in `.env`** — für „spiel [X]": kostenlose App auf
      developer.spotify.com, `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` eintragen.
      Ohne Credentials liefert „spiel [X]" einen Setup-Hinweis; alles andere läuft.
- [ ] Free-Tier spielt bei „spiel [Track]" den Album-Kontext (Spotify-Verhalten, kein
      Bug); auf Premium exakt der Song.

---

## Offen — externe Abhängigkeiten

Diese Items brauchen externe Infrastruktur oder Hardware die nicht im Code lösbar ist:

| Item | Blockiert durch |
|------|----------------|
| WhatsApp-Integration | Meta Business API + verifizierte Nummer |
| HTTPS für Dashboard | Tailscale-Zertifikat oder mkcert-Setup |
| Free Dictation Side-Mode | macOS Accessibility-Daemon (Hotkey → Whisper → Tastatureingabe) |
| Eigenes MLX-Modell | Fine-Tuning Compute + Trainingsdaten |
| Ambient Listening Mode | Always-on Mikrofon (Privacy-kritisch, Hardware) |
| Google Takeout Location-Timeline | Timos persönliche Takeout-Datei |

---

## Native Mac-App-Migration + Health-Overview (2026-07-13)

Weg vom Web-Dashboard, hin zur Tauri-Mac-App (`apps/desktop/`) — ohne Nav-Leiste, nur
Voice + Cmd/Ctrl+K-Hidden-Menu. Zerlegt in 4 Sub-Projekte (Shell/Navigation, Feature-
Parität, Memory-Overhaul+Wissensgraph, Health-Overview). Umbrella-Scope:
`docs/superpowers/specs/2026-07-13-native-app-migration-scope.md`.

**SP4 — Health-Overview mit Domain-Scores (zuerst, in Arbeit).** 4 Domain-Scores
(Recovery/Sleep/Activity/Body) + Metrik-Graphen + Mantis-Klartext, transparent
aufschlüsselbar, Ring-Hero-Layout. Spec: `2026-07-13-health-overview-scores-design.md`.

- [x] **BodyOS-Health-Sync reparieren** — behoben: der lossy Push-Pfad (COROS →
      Apple Health → BodyOS → `/api/health/push`) ist ersetzt durch einen direkten
      COROS-MCP-Sync (`domains/coros/`), der `health_data` befüllt. Recovery/Sleep/
      Activity melden jetzt `status=ok, coverage=1.0`.
- [ ] SP1 App-Shell & Navigation (Overlay-Framework generalisieren)
- [ ] SP2 Feature-Parität (goals/journal/insights/fitness/globe … als Widgets/Overlays)
- [ ] SP3 Memory-Overhaul (Obsidian/Zettelkasten) + Wissensgraph-Overlay

---

## Architektur-Prinzipien

1. **Lokal first** — Daten verlassen den Mac nicht (außer explizite Cloud-Tools wie Claude API)
2. **Modulare Backends** — LLM, DB, Kommunikation sind austauschbar
3. **Kein Over-Engineering** — einfacher Code > komplexe Abstraktionen
4. **Funktionalität > Ästhetik** — ein Feature das funktioniert > fünf die aussehen
5. **Mantis soll lernen** — jede Interaktion macht ihn besser, nicht nur reaktiver

## Voice-Pipeline-Latenz-Untersuchung + lokales Voice-Modell (2026-07-05, spät)

- [x] STT-Benchmark: pywhispercpp (aktuell), faster-whisper, mlx-whisper,
      lightning-whisper-mlx auf 6 deutschen Testsätzen verglichen —
      **aktuelles Setup (whisper.cpp/Metal) bleibt bestes für kurze
      Sprachbefehle**, keine der Alternativen war schneller (siehe
      `scripts/stt_benchmark_run.py`)
- [x] End-to-End-Latenztest gegen echte Pipeline (`scripts/e2e_voice_latency_test.py`)
      aufgedeckt: Voice-Antworten liefen über Claude (Cloud), Latenz dominiert
      von Netzwerk-Roundtrips (6-32s/Anfrage), nicht STT/TTS
- [x] Root-Cause für "lokales Modell ist noch langsamer" gefunden: `qwen3.5:9b`
      brauchte lokal bis zu 166s (16GB RAM, `OLLAMA_KEEP_ALIVE="0"` entlädt
      Modell nach jedem Call, plus Speicherdruck)
- [x] Dediziertes, dauerhaft geladenes Voice-Modell (`gemma4:e2b`,
      `VOICE_AGENT_KEEP_ALIVE="-1"`) eingebaut — wiederverwendet dasselbe
      Modell wie der Adress-Check. Warm ~5.5-7.5s statt 166s. Claude bleibt
      Fallback bei Ausfall/unzureichender Qualität.
- [x] XTTS-v2-Umgebung vorbereitet (`data/xtts/venv`, Python 3.11 isoliert vom
      Haupt-Backend (Python 3.14, noch keine kompatiblen XTTS-Pakete)) —
      wartet auf Stimmprobe zum Klonen
- [ ] VAD-Wechsel auf Silero VAD (offen)
- [ ] Wake-Word via openWakeWord (offen)
- [ ] Streaming durchgängig verketten (LLM → TTS, nicht erst nach kompletter
      Antwort) — größter verbleibender Latenz-Hebel, noch nicht umgesetzt
- [ ] Haupt-Dashboard/Telegram-Agent: aktuell Ollama-primär (`qwen3.5:9b`) seit
      der früheren Änderung — angesichts der 166s-Erkenntnis fraglich, ob das
      auch dort sinnvoll ist; noch nicht mit Timo geklärt
