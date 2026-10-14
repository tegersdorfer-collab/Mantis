# Spec: Mantis gesund machen (Nutzungs-Challenge, Phase 3a)

> **Überholt seit 2026-10-14:** Diese Spec hält den damaligen Stand fest. Die Cloud-Migration
> [`2026-10-14-cloud-umstellung.md`](2026-10-14-cloud-umstellung.md) ersetzt die lokalen
> LLM-Fallbacks und die lokale Datenschutzgrenze; nur Embeddings bleiben lokal.

Stand 2026-10-13. Kontext: `docs/HANDOFF-nutzungs-challenge.md`. Timo nutzt Mantis nicht; Mantis
beschäftigt sich vor allem mit sich selbst (~1000 Hintergrund-LLM-Calls in 8 Wochen, 1986 Fehler),
und Chats werden nach 3 Tagen gelöscht, sodass Nutzung nicht messbar ist.

Ziel: schlanker Kern, der zuverlässig antwortet und Nutzung messbar macht. **Nichts löschen**, alles
Abgeschaltete bleibt per Flag reaktivierbar. Keine neuen Features außer dem Nutzungs-Endpunkt.

## 1. Chat-Historie behalten (Messbarkeit)
- `memory/forgetting.py`: `CHAT_TTL_DAYS = 3` wird ein Setting `CHAT_TTL_DAYS` in `settings.py`
  (Default **365**, per `.env` überschreibbar). `_prune_chat` nutzt es; SQL parametrisiert
  (`INTERVAL` per `make_interval(days => %s)` o. ä., kein f-String).
- `core/message_handler.py` `_persist_msg`: Fehler mit `log.error` statt still `warning`, damit sie
  im events_log landen. Kanal (`telegram`, `web`, `ios`, `voice`, …) muss korrekt gesetzt werden;
  prüfen, dass alle Eingangskanäle ihren Kanal durchreichen.
- Neuer Endpunkt `GET /api/usage/daily?days=56` (auth-geschützt wie andere API-Routen): pro Tag und
  Kanal die Anzahl User-Nachrichten aus `chat_messages`. Antwort-JSON:
  `{"days": [{"date": "2026-10-13", "channel": "telegram", "user_messages": 3}, ...]}`.

## 2. Hintergrundjobs per Flag abschalten
Neue Flags in `settings.py`, alle **Default `False`**, per `.env` einschaltbar:

| Flag | Job |
|---|---|
| `INSIGHTS_ENABLED` | Insight Engine (`core/idle_loop.py` ~244) |
| `PATTERN_DETECTION_ENABLED` | Pattern Detection (~231) |
| `TRAINING_PLAN_AUTOGEN_ENABLED` | Trainingsplan-Autogenerierung (~147) |
| `NEWS_REFRESH_ENABLED` | News-Globus Refresh (~134) |
| `DAILY_REFLECTION_ENABLED` | Tages-Reflexion (~127) |
| `MEMORY_CONSOLIDATION_ENABLED` | Memory Consolidator (~168) |
| `TASK_DEEP_EXECUTION_ENABLED` | Autopilot Task Deep Execution (`core/autopilot.py` ~243) |
| `BACKGROUND_REVIEW_ENABLED` | Post-Turn Background Review (`core/background_review.py`) |

Außerdem `ACCOUNTABILITY_ENABLED` Default von `True` auf **`False`**.
`AUTONOMOUS_MESSAGES_ENABLED` bleibt Default `False` (unverändert).

**Bleibt an:** Telegram-Polling, DB-Backup + Verifikation, Forgetting/Chat-Pruning (mit neuer TTL),
KZG-Checkpoint, Fact-Extraction nach dem Turn, Monitoring-Loop, fällige Reminder.

Beim Start eine einzige Logzeile (INFO), welche Hintergrundjobs aktiv/inaktiv sind.

## 3. Fallback-Logging
`core/backends/fallback.py`: Wenn `str(exc)` leer ist (z. B. `asyncio.TimeoutError`), den
Exception-Typ loggen (`agent_fallback: TimeoutError`), nie leere Meldungen.

## 4. `/health` minimal öffentlich
`/health` ohne Auth liefert nur `{"status": "ok"}` (bzw. `"degraded"` + HTTP 503, wenn der Kern
nicht bereit ist), **keine** weiteren Details. Mit gültiger Auth wie bisher die volle Antwort.
Nicht über `PUBLIC_PATHS` den ganzen Handler freigeben, wenn der Details liefert; sicherheitsrelevant.

## Tests (Pflicht, TDD)
- TTL-Setting wird in `_prune_chat` verwendet (Default 365).
- Jeder neue Flag: Job läuft bei `False` nicht, bei `True` schon (Idle-Loop/Autopilot mit Mocks).
- `/api/usage/daily`: Aggregation korrekt, ohne Auth 401.
- `/health` ohne Auth: nur `status`-Feld; mit Auth: volle Antwort.
- Fallback-Log bei `TimeoutError` enthält den Typnamen.
- Gesamte Suite grün: `python3.14 -m pytest tests/ -q`, `python3.14 -m ruff check .`.

## Nicht im Scope (macht Claude / entscheidet Timo)
launchd-Dienste (forge, forge-bot, console_watch, gods-eye-view), Wahl des Chat-Modells
(`LLM_LOCAL_ONLY`), `DASHBOARD_HOST`/Tailscale-Binding, dynamische Skill-Dateien.

## Teil 2: Chat über OpenRouter

Der interaktive Agent nutzt bei gesetztem `OPENROUTER_API_KEY`, nichtleerem
`CHAT_CLOUD_MODEL` und `LLM_LOCAL_ONLY=False` diese Kette:
`OpenRouter(CHAT_CLOUD_MODEL) → OpenRouter(CHAT_CLOUD_FALLBACK_MODEL) → lokales Ollama`.
Neue Settings in `settings.py`/`config.py`:

| Setting | Default | Zweck |
|---|---|---|
| `CHAT_CLOUD_MODEL` | `openai/gpt-6-luna` | Primäres OpenRouter-Chatmodell |
| `CHAT_CLOUD_FALLBACK_MODEL` | `google/gemini-3.1-flash-lite` | OpenRouter-Fallback eines anderen Anbieters |
| `CHAT_CLOUD_REASONING_EFFORT` | `low` | OpenRouter-Reasoning-Aufwand |
| `CHAT_CLOUD_TIMEOUT_S` | `30` | Timeout je Cloud-Stufe; lokales Ollama behält sein bisheriges Timeout |

`LLM_LOCAL_ONLY=True` erzwingt weiterhin ausschließlich lokale LLMs. Ist OpenRouter
nicht konfiguriert, bleibt die bisherige Claude- beziehungsweise Ollama-Kette
unverändert. Mehrstufiges Failover führt pro Stufe Cooldowns und protokolliert bei
Fehlern immer den Exception-Typ, auch wenn die Meldung leer ist. Beim Start wird die
aktive Chat-Kette in einer INFO-Zeile ausgegeben.

`BG_CHAT_TASKS_USE_CLOUD` (Default `True`) routet ausschließlich Post-Turn-
Fact-Extraction und KZG-Checkpoint-Summary über dieselbe Cloud-Kette mit lokalem
Fallback. Bei `False` bleiben diese Aufgaben lokal. Alle anderen Background-Jobs
verwenden unverändert `bg_llm`. Chat- und Background-Aufrufe werden als
`provider='openrouter'` mit `purpose='chat-agent'` beziehungsweise
`purpose='background'` erfasst. Der Request fordert `usage.include=true` an und
`llm_usage` speichert `usage.cost` als tatsächliche OpenRouter-Kosten.

**Datenschutzgrenze (Entscheidung Timo, 13.10.2026):** Der interaktive Chat-Turn
darf den vollen Kontext an die Cloud-Kette senden: System-Prompt mit LZG-Erinnerungen,
Aufgaben, Dashboard-/KG-Profil und Health/COROS-Werten, wiederhergestellte KZG-Historie
(auch autonome Meldungen) sowie Fact-Extraction und KZG-Summary der Chat-Turns.
Dashboard- und Hintergrund-Routen, die gespeicherte private Daten verarbeiten (Journal
inkl. Antworten, Gesundheit/COROS, Fitness, Ernährung, Aufgaben-Klassifikation und
-Vorschläge), laufen weiterhin ausschließlich lokal über `bg_llm`. Für
Journal-Extraktion sind auch Verifier und Konfliktprüfung lokal.

Tool-Schemas müssen auch im OpenAI-/Anthropic-kompatiblen Export gültiges JSON
Schema enthalten: `type: object`, `properties` als Objekt, `required` als Teilmenge
der Properties, gültige Property-Typen und `items` für Arrays. Der fehlerhafte
`git_history_import`-Schemaeintrag und weitere Treffer werden korrigiert.

Pflichttests: Reihenfolge der Chat-Kette; Fallback bei Timeout und HTTP-Fehlern auf
jeder Cloud-Stufe; `LLM_LOCAL_ONLY`; bisheriges Verhalten ohne OpenRouter-Key;
strukturelle Validierung aller registrierten Tool-Schemas; Übernahme echter
OpenRouter-Kosten; `BG_CHAT_TASKS_USE_CLOUD` an/aus. Abschließend:
`python3.14 -m pytest tests/ -q` und `python3.14 -m ruff check .`.
