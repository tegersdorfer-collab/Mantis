# Mantis iOS Apps

Drei fokussierte SwiftUI-Apps, die alle gegen Mantis' FastAPI-Backend laufen
(`:7779`). Sie ersetzen das frühere, überladene PWA-Dashboard durch jeweils
ein klar abgegrenztes Werkzeug. Ziel: niedrige Reibung — wenn etwas nervt,
wird es nicht benutzt.

> **Historie:** Ursprünglich als 5 Apps geplant (GymOS, FoodSnap, FocusOS,
> HabitDot, BrainOS). Auf 3 konsolidiert — BodyOS bündelt Gym + Ernährung +
> Health, FlowOS bündelt Tasks + Kalender + Habits + Fokus.

---

## Die drei Apps

### BodyOS — Körper & Training
**Ordner:** `apps/BodyOS/` · **Bundle:** `de.timoegersdorfer.BodyOS`

Gym-Tracker, Ernährung und Health-Daten in einer App.
- **Training:** Mantis generiert die Session (3-Tage-Zyklus Upper/Jog/Lower)
  basierend auf HRV + Schlaf. RPE-Feedback pro Übung, Rest-Timer.
- **Ernährung:** Foto → Cloud-Bildanalyse → Makros bestätigen. Makro-Ringe,
  adaptive Kalorienziele.
- **Health:** HealthKit-Background-Push (Schritte, HRV, Schlaf, Gewicht) an Mantis.
- **Endpoints:** `/api/fitness/{today-plan,exercises,log-rpe}`,
  `/api/nutrition/{analyze-photo,log-meal,goals}`, `/api/health/{push,manual}`,
  `/api/workouts`

### BrainOS — Second Brain
**Ordner:** `apps/BrainOS/` · **Bundle:** `de.timoegersdorfer.BrainOS`

Obsidian-artige Notizen mit Graph.
- Notizen mit PARA-Kategorien, Markdown-Editor, `[[wiki-link]]`-Autocomplete
- Force-directed Graph (SwiftUI Canvas, eigene Physik — keine externen Libs)
- Quick Capture, Volltextsuche, Quotes
- **Endpoints:** `/api/brain/{notes,daily,graph}`

### FlowOS — Tasks, Kalender & Habits
**Ordner:** `apps/FlowOS/` · **Bundle:** `de.timoegersdorfer.FlowOS`

Tagesplanung und Gewohnheiten.
- Today-View (Mantis-Nachricht + Termine + priorisierte Tasks)
- Tasks mit Unteraufgaben, Kalender (lesen/schreiben), Habits-Grid, Fokus-Timer
- **Endpoints:** `/api/tasks`, `/api/calendar`, `/api/habits`

---

## Gemeinsame Architektur

- **SwiftUI** (Swift 5.9+), SwiftData für lokalen Offline-Cache
- **Netzwerk:** jede App hat einen `MantisClient` (`API/MantisClient.swift`),
  `URLSession` async/await. Identisches Muster in allen drei Apps:
  - Base-URL in `UserDefaults` (`mantis_base_url`), per Settings-View änderbar
  - Default: `http://macbook-air-von-timo.tail7e29ff.ts.net:7779` (Tailscale)
  - `waitsForConnectivity = true` + ein Retry nach 1,5 s → übersteht kurze
    Tailscale-Aussetzer ohne Fehler
  - `keyDecodingStrategy = .convertFromSnakeCase` (Mantis liefert snake_case)
- **Keine Auth** — nur im privaten Tailnet erreichbar
- **App Transport Security:** `Info.plist` erlaubt HTTP zum Tailscale-Host
  (`NSExceptionDomains` → `*.tail7e29ff.ts.net`)

## Tailscale-Setup

Mantis bindet auf `0.0.0.0:7779` (siehe `settings.py` / `.env`
`DASHBOARD_HOST`), ist also über jede Schnittstelle des Macs erreichbar —
inklusive der Tailscale-IP/-MagicDNS-Adresse. Die Apps sprechen den Mac über
seinen MagicDNS-Namen an, damit sich nichts ändert wenn die IP wechselt.

## Bauen & auf iPhone deployen

Xcode-beta ist installiert (Command-Line-Tools allein reichen nicht). Pro App:

```bash
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer xcodebuild \
  -project apps/BodyOS/BodyOS.xcodeproj \
  -scheme BodyOS \
  -destination "id=<DEVICE_UDID>" \
  -configuration Debug \
  install
```

Geräte-UDID ermitteln:
```bash
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  xcrun xctrace list devices | grep -v Simulator
```

Alternativ: `.xcodeproj` in Xcode öffnen, Gerät wählen, ⌘R.
