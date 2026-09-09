# TikTok-Agent — Mantis scrollt, kuratiert & brieft

**Datum:** 2026-07-20
**Status:** ⏸️ **ON HOLD** (2026-07-20). Design steht. Bewusst zurückgestellt, bis stabiles **usbliter8**-Tooling erscheint → dann Weg A (Jailbreak/on-device) in einem Rutsch bauen. WDA-Weg (Weg B) *wäre* heute machbar, aber Bauen-jetzt + Ersetzen-später = Doppelarbeit, daher geparkt. Trigger zum Reaktivieren: public usbliter8 Downgrade/JB-Tool für A13.
**Motivation:** FOMO ist der einzige *logische* Grund für Timos hohen TikTok-Konsum (Dopamin ist Emotion, vergeht mit der Zeit). Mantis übernimmt das Scrollen, formt den Feed intentional Richtung Wert, und brieft die Learnings. Ziel: FOMO befriedigt, Zeit zurück.

## Entscheidungen (fix)
1. **Gerät:** altes iPhone 11 (A13, Screen teil-defekt, Touch unzuverlässig), dauerhaft am Strom, nur WLAN, keine SIM. **Ist-Stand: iOS 26.3.** → **Auf 26.3 bleiben, Auto-Update AUS.** Nicht auf 27 hoch (Beta, kein Mehrwert). Downgrade aktuell nicht möglich (siehe #2).
2. **Steuerung — Weg B jetzt, Weg A als Ziel sobald Tooling reift:**
   - **Weg B — kein Jailbreak (AKTUELLER Pfad, machbar):** iOS 26.3 + **WebDriverAgent/Appium** (XCTest-Injektion); Device Hub + Mac-Maus als Sekundär-Fallback. Braucht Mac-Signierung (7-Tage/Paid Dev) + go-ios-Tunnel für Linux-Dauerbetrieb.
   - **Weg A — Jailbreak (bessere Ziel-Architektur, aber HEUTE blockiert):** autonom **on-device** (AutoTouch/Lua), echter HID-Touch, kein Mac, kein Tunnel, nur Strom+WLAN. Blockiert weil: **Dopamine** reicht nur bis iOS 16.5.1 (26.3 zu hoch), und der Weg auf 16.5.1 bräuchte ein **usbliter8**-Downgrade-Tool (Bootrom-Exploit A12/A13, Juni 2026) — das ist Stand Juli 2026 **nur PoC, kein fertiges Tool**. Downgrade scheitert also nicht an Daten (egal), sondern am fehlenden Werkzeug. **Beobachten:** sobald stabiles usbliter8-Tool erscheint → downgrade 16.5.1 + Dopamine, dann auf Weg A wechseln. Risiko dann noch: TikTok Anti-Tamper/JB-Detection → Bypass (Shadow/Choicy), in M0 testen.
   - **Entscheidung/Verifikation in Milestone 0 am echten Gerät (26.3 + WDA).**
3. **Capture:** iPhone-Bild + Ton via **QuickTime/AVFoundation** auf den Mac. Mac verarbeitet.
4. **Verarbeitung:** **kein** Frame-für-Frame. Audio→Transkript (lokal Whisper) + Caption/Hashtags + **sparse Keyframe-OCR** (~1 Frame / 2 s). VLM nur für die paar Keyframes bei Bedarf.
5. **Kuratierung:** Timo labelt gut/normal/schlecht → Mantis lernt daraus eine Wert-Präferenz. Werte-Fokus: **Mindset, Psychologie**.
6. **Output:** **on-demand** („Mantis, was gab's?"). Gesprochenes Briefing (Voice) **+** Grafik-Karten in der Mac-App (wie Mini-Präsentation), echte Clips on-demand abrufbar. Learnings landen im Second Brain.
7. **Volumen:** Testtag **30 min** → Ramp-up → Ziel **3–5 h/Tag**, aber **über mehrere menschliche Sessions verteilt**, nie am Stück.
8. **Fingerprint-Realität:** Kein Software-Weg macht echten Digitizer-Touch — alle *injizieren*. Schutz = echter Geräte-Fingerprint (Hardware/Sensoren real) **+** menschliches Timing/Muster (der Verhaltens-Klon), **nicht** die Touch-Ebene selbst.

## Architektur (Komponenten, isoliert)

### A. Device-Bridge (`domains/tiktok/device.py`)
Dünne Abstraktion über die Steuerung. Interface: `tap(x,y)`, `swipe(path, duration)`, `screenshot()`, `is_alive()`.
- Impl. **WDA** (HTTP an WebDriverAgent) — primär.
- Impl. **DeviceHub-Maus** (Mac-GUI-Automation im Canvas) — Fallback, gleiches Interface.
- Consumer kennt nur das Interface → Backend-Wechsel ohne Änderung oben.

### B. Behavior-Clone (`domains/tiktok/behavior.py`) — **reine Logik, TDD**
Ahmt Timos Scroll-Signatur nach: Watch-Time-Verteilung pro Video, Swipe-Geschwindigkeit/-Kurve, Like-/Save-/Kommentar-Öffnen-Raten, Session-Länge, Pausen, Tageszeiten.
- Lernt aus einem aufgezeichneten **Timo-Profil** (echte Scroll-Session einmal mitschneiden → Verteilungen fitten).
- Reine Funktionen: `next_dwell(video_features, profile) -> seconds`, `swipe_gesture(profile) -> path`, `should_engage(video, profile) -> {like,save,open_comments}`, `session_plan(day, profile) -> [session_windows]`. Alles injizierbar → vitest/pytest.
- **Kill-Switches:** stoppt Session bei Anzeichen (CAPTCHA, „ungewöhnliche Aktivität"-Prompt, Login-Wall).

### C. Capture-Pipeline (`domains/tiktok/capture.py`)
Pro Video: Audio-Clip + N Keyframes ziehen (aus dem QuickTime-Stream, geschnitten an Swipe-Events der Bridge).
- `transcribe(audio) -> text` (lokal Whisper).
- `ocr_keyframes(frames) -> text`.
- `metadata(video) -> {caption, hashtags, sound, creator}` (aus On-Screen-Text/UI-Scrape).
- Output: strukturiertes `VideoRecord`.

### D. Wert-Scorer + Feed-Shaping (`domains/tiktok/curation.py`) — **reine Logik, TDD**
- `score_value(VideoRecord, preference_model) -> float` — nutzt Transkript+Caption+OCR (Text-Embedding gegen Timos gut/schlecht-Labels; Mindset/Psychologie hoch gewichtet).
- `feed_action(score) -> engagement` — hoher Wert → länger schauen/liken/saven; niedriger → schnell wegwischen/„nicht interessiert". So formt sich der Algorithmus **intentional**.
- Präferenzmodell lernt inkrementell aus neuen Labels.

### E. Digest (`domains/tiktok/digest.py`) — **reine Logik, TDD**
- `build_digest(records_since_last, top_k) -> {spoken_script, cards[]}` — clustert nach Thema, extrahiert Kern-Takeaways, verlinkt Clips + legt Second-Brain-Notizen an (Folgezettel).
- Karten: Kernaussage, Quelle/Creator, „merken?", Link zum Clip.

### F. Orchestrator (`domains/tiktok/agent.py`)
Fährt Session nach `session_plan`: Bridge holen → scrollen (Behavior) → capturen → scoren/handeln → Records ablegen. Robust gegen Bridge-Tode (neu verbinden), respektiert Kill-Switches.

### Host / Betrieb (wichtige Trennung)
- **Signieren + Milestone 0 → Mac** (Xcode-only, gilt auch für Free Apple ID). Kabel USB-C→Lightning nötig, unvermeidbar.
- **Dauerbetrieb → Debian-24/7-Server** (alter Medion) via **go-ios** — iPhone dort permanent angeschlossen, headless. Blockiert nicht das Arbeits-MacBook.
- **Re-Signierung:** Free Apple ID = alle 7 Tage neu (Mac); Paid Dev Account (99 $/J) = 1 Jahr. Für Dauerläufer Paid erwägen.
- **iOS 17+ auf Linux:** braucht `sudo ios tunnel start` (RemoteXPC). **Risiko:** go-ios `runwda` hat offene Bugs ab iOS 26 (#631) → iOS-27-Support auf Linux in M0 verifizieren; falls (noch) kaputt, Betrieb vorerst am Mac + später migrieren.
- Bridge-Interface (Komponente A) ist host-agnostisch → Mac- und Linux-Backend teilen denselben Consumer-Code.

### G. Schnittstellen (Mantis-Integration)
- Voice-Intent `tiktok_digest` (on-demand) → ruft `build_digest`, spricht Script, öffnet Karten-Overlay.
- Neues **TikTok-Overlay** (`apps/desktop/src/tiktok-overlay.ts`) auf dem SP1-Framework: Briefing-Karten + Clip-Player + gut/normal/schlecht-Labeling-UI (fürs Training).
- `GET /api/tiktok/digest`, `POST /api/tiktok/label`, `GET /api/tiktok/records` (`web/routers/tiktok.py`).

## Datenfluss
`session_plan` → Orchestrator → Bridge-Gesten (menschliches Timing) → QuickTime-Stream → Capture (Transkript+OCR+Meta) → `VideoRecord` (DB) → Wert-Scorer → Feed-Action zurück über Bridge → … → on-demand: Digest aus Records → Voice + Karten → Labels zurück ins Präferenzmodell.

## Milestones
- **M0 — Machbarkeit am Gerät (Gate, VOR Produktcode):** iPhone 11 auf iOS 27; WDA signieren + laufen lassen; **Kernfrage: injizierte Gesten steuern TikTok trotz kaputtem Digitizer?** und **flaggt TikTok WDA-Events in 30 min?** Falls WDA scheitert → DeviceHub-Maus-Fallback testen. Ergebnis entscheidet Bridge-Impl.
- **M1 — Behavior-Clone:** Timo-Profil aufzeichnen, Verteilungen fitten, Kill-Switches. Testtag 30 min.
- **M2 — Capture + Scorer:** Transkript/OCR-Pipeline, erste gut/schlecht-Labels, Wert-Score.
- **M3 — Feed-Shaping:** Engagement-Aktionen zurückspielen, über Tage beobachten ob Feed wertvoller wird.
- **M4 — Digest + Overlay:** Voice-Briefing + Karten + Labeling-UI. Ramp Richtung 3–5 h/Tag.

## Risiken / offen
- **Detection** (Haupt­risiko): injizierte Events + Nur-scroll-Gerät + Volumen. Mitigation: Ramp-up, verteilte menschliche Sessions, Verhaltens-Klon, Kill-Switches, Abbruch-bei-Prompt. M0/Testtag = empirischer Check.
- **Bridge-Wahl** offen bis M0.
- **ToS & Account-Bann (zentrale Spannung):** TikTok-Automation verstößt gegen die Nutzungsbedingungen. Der Zweck ist explizit **Timos eigener Algorithmus** → läuft auf dem **Haupt-Account**. Damit trifft ein Bann genau das, was aufgebaut werden soll (Account + geformter Algo weg). **Das Wegwerf-Gerät schützt nur die Hardware, NICHT den Account.** Ein Zweit-Account würde den Zweck untergraben (fremder Algo). → Bewusste Abwägung nötig: Risiko akzeptieren vs. mit Zweit-Account „proben", bis der Klon nachweislich unauffällig ist, dann auf Haupt-Account. **Offene Entscheidung.**
- **Watch-Time-Signal:** Feed-Shaping hängt an ehrlicher Dwell-Steuerung; wenn TikTok Dwell anders misst als erwartet, Scorer nachkalibrieren.

## Nicht in Scope (YAGNI)
Kein Video-Rendering eigener Zusammenfassungs-Clips (erst nur echte Clips zeigen). Kein Multi-Account. Kein Kommentar-Schreiben/Posten. Keine anderen Plattformen.
