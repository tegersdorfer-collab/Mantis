# God's Eye View Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** God's Eye View im Mantis-Dashboard anzeigen und durch Mantis-Tools steuern.

**Architecture:** GEV bleibt lokaler Sidecar-Prozess. Ein authentifizierter Mantis-Befehlskanal liefert Aktionen an die Dashboard-Ansicht, die sie über eine eng begrenzte Bridge an GEV sendet und quittiert.

**Tech Stack:** Python 3.14, FastAPI, SSE, Browser JavaScript, Node.js 26, Vite, Cesium.

**Spec:** `docs/superpowers/specs/2026-09-16-gods-eye-view-design.md`

## Global Constraints

- Upstream-Commit `a65d9d8` und Node.js 24.14+ oder 26.x.
- GEV nur an `127.0.0.1:4173` binden.
- Keine Zugangsdaten aus Mantis an GEV übertragen.
- Keine Änderung am bestehenden News-Globus.

---

### Task 1: Befehlskanal und Agent-Tools

**Files:** `core/gev_control.py`, `core/skills/gev.py`, `core/skills/__init__.py`, `core/tools.py`, `web/routers/gev.py`, `web/routers/__init__.py`, `tests/test_gev_control.py`

**Interfaces:** `GEV_BUS.dispatch(action: str, args: dict) -> dict` wartet auf eine Quittierung; `subscribe() -> asyncio.Queue` und `ack(command_id: str, result: dict) -> bool` versorgen die API. Aktionen: `fly_to`, `set_layer_visibility`, `set_visual_style`, `zoom_to_globe`.

- [x] Test für gültige Befehle, ungültige Werte, Quittierung und Timeout schreiben.
- [x] `python3.14 -m pytest tests/test_gev_control.py -q` ausführen und den erwarteten Fehler prüfen.
- [x] Bus, Router, Tools und Routing-Schlüsselwörter implementieren.
- [x] Tests erneut ausführen und Ergebnis prüfen.

### Task 2: Lokalen GEV-Sidecar reproduzierbar installieren

**Files:** `scripts/setup-gods-eye-view.sh`, `integrations/gev/bridge.js`, `integrations/gev/bridge.test.mjs`, `integrations/gev/README.md`

**Interfaces:** Bridge empfängt `{source:'mantis', id, action, args}` vom erlaubten Parent-Origin, ruft GEVs Action-Runner auf und sendet `{source:'gev', id, result}` zurück. Setup patcht einen fixierten Upstream-Commit und startet ausschließlich lokal.

- [x] Node-Tests für Origin-Prüfung und Aktionsresultat schreiben und fehlschlagen lassen.
- [x] Bridge und Setup-Skript implementieren, Tests erneut ausführen.
- [x] Upstream-Doctor sowie Start auf `127.0.0.1:4173` prüfen.

### Task 3: Dashboard und Integrationstest

**Files:** `web/index.html`, `tests/test_gev_api.py`, `README.md`

**Interfaces:** `VIEWS.gev` zeigt den iframe; ein Dashboard-EventSource empfängt Befehle und quittiert sie über `/api/gev/ack`.

- [x] API- und Authentifizierungstest schreiben und fehlschlagen lassen.
- [x] Dashboard-Ansicht, SSE-Anbindung, Bereitschafts-Handshake und Fehleranzeige implementieren.
- [x] Tests, Ruff und Browserprüfung ausführen; README mit Startanleitung ergänzen.
