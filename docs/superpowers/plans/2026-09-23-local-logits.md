# Lokales Logit-Scoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jev-Ausfälle können optional durch lokale, kalibrierbare Token-Logits für Noul-, Choice- und Score-Fragen ersetzt werden; der Benchmark misst diesen Pfad separat.

**Architecture:** `core/local_decide.py` baut pro Frage einen isolierten Ollama-`/api/generate`-Request und wandelt erlaubte Label-Logprobs in `core.decide.Answer` um. `core.decisions.py` probiert den optionalen Pfad nur nach Jev-Ausfall und akzeptiert ihn nur bei Band ACT. `bench/jev/run.py` erhält einen RAM-geschützten Lauf, tagged Ergebnisdateien und einen eigenen Kalibrierungsbericht.

**Tech Stack:** Python, asyncio, httpx MockTransport, jevkit Noul/Choice/Score, Ollama API, pytest.

**Spec:** Nutzerauftrag vom 2026-09-23 „Lokales Logit-Scoring als kalibrierbarer Fallback für Jev-Entscheidungen in Mantis“.

## Global Constraints

- `LOCAL_LOGITS_ENABLED` ist standardmäßig `False`.
- Ollama-Aufrufe verwenden `config.OLLAMA_BASE_URL`, `config.AGENT_MODEL_FAST`, `core.llm_gate.GATE` und ein konfigurierbares Timeout von standardmäßig 8 Sekunden.
- Jede Frage erhält einen eigenen `/api/generate`-Call mit genau einem generierten Token und Logprobs.
- Das lokale Ein-Token-Alphabet enthält 62 ASCII-Buchstaben/Ziffern; größere Choices lösen `LocalDecisionUnavailable` aus und bleiben beim bisherigen Fallback.
- Ollama generiert deterministisch mit `temperature=0`; die konfigurierbare Scoring-Temperatur skaliert nur die Logprobs vor dem Softmax.
- Ollama-Fehler und Timeouts werden als eigene lokale Ausnahme signalisiert.
- Tests greifen nicht auf Netzwerk oder Ollama zu.
- Der Benchmark wird in dieser Sandbox nicht ausgeführt; Ausgaben mit `--tag` überschreiben keine vorhandenen Dateien.
- Nicht committen.

## Review Focus

- Logprob-Tokens mit führenden Leerzeichen werden mit dem gleichen Label aggregiert; fehlende Labels erhalten einen Floor und werden markiert — Tests prüfen beide Fälle.
- Leere, unvollständige oder fehlerhafte Ollama-Antworten und Timeouts lösen die lokale Ausnahme aus — Tests patchen den Transport.
- State mit Zeilenumbrüchen oder Markertext bleibt serialisiert und als Fremdtext markiert — Prompt-Test prüft escaping.
- Ein-Optionen-Choice hat definierte Konfidenz und ein zu großes Label-Alphabet bricht mit der lokalen Ausnahme ab — Antwort-/Grenztest prüft beides.
- Ungültige Temperaturen erzeugen einen klaren Eingabefehler und positive Temperaturen skalieren Softmax — Rechentests prüfen beide Fälle.

---

### Task 1: Scoring-Engine und Config

**Files:**
- Modify: `settings.py`, `config.py`, `.env.example`
- Modify: `core/decide.py` (optionale Ergebnis-Metadaten für ersetzte Labels)
- Create: `core/local_decide.py`
- Test: `tests/test_settings_jev.py`
- Test: `tests/test_local_decide.py`

**Interfaces:**
- Produces: `async score(state, questions, *, model=None, temperature=1.0, prompt_format="auto") -> dict[str, core.decide.Answer]` und `LocalDecisionUnavailable`.
- Produces: reine `build_prompt(state, question, *, prompt_format="auto", model_name=None) -> str`.
- Produces: `Answer.metadata["missing_labels"]` für Optionen, deren Token im Top-Logprobs-Set fehlte.

- [x] Tests zuerst für Label-Varianten/Floor, temperatur-skaliertes Softmax, Noul/Choice/Score-Confidence, Score-Erwartungswert, Prompt-Escaping, Request-Parameter und Fehlerpfad schreiben und gezielt rot laufen lassen.
- [x] Config-Defaults und minimale Engine implementieren, bis diese Tests grün sind.
- [x] Entscheidungstypen und Antworten mit ihren jeweiligen jevkit-Rohantworten abbilden.

### Task 2: Fallback-Integration

**Files:**
- Modify: `core/decisions.py`
- Test: `tests/test_decisions.py` oder fokussierte neue Tests in `tests/test_local_decide.py`

**Interfaces:**
- Consumes: `local_decide.score`, `LocalDecisionUnavailable`, `decide.Answer`.
- Produces: Nach Jev-Ausfall wird bei aktivem Flag lokal bewertet; nur Band ACT darf den bisherigen Fallback ersetzen.

- [x] Tests zuerst für Flag aus, lokales ACT, lokales CONFIRM/ESCALATE und lokalen Fehler schreiben und rot ausführen.
- [x] `_noul_or_fallback`, `tool_categories` und `ui_action` nur im Jev-Ausfallpfad um den lokalen Versuch ergänzen.
- [x] Lokale Antworten über das bestehende Entscheidungslog mit ihrem Modellnamen loggen.

### Task 3: Benchmark und tagged Reports

**Files:**
- Modify: `bench/jev/run.py`
- Test: fokussierte Bench-Helfer-Tests

**Interfaces:**
- Adds: `--local-logits MODELL[,MODELL]`, `--tag TAG`, `write_report(rows, tag=None)`.
- Raw-Backend-Namen verwenden `local-logits:<modell>`; der Report enthält Accuracy je Fallart, Noul-Brier, Median-Latenz sowie Accuracy und Abdeckung bei Confidence ≥ 0.5.

- [x] Tests zuerst für Tag-Pfade, Konfliktvermeidung und Kalibrierungsmetriken ergänzen und rot ausführen.
- [x] Dieselben `jev_questions` wie Jev in jevkit-Typen überführen und an `local_decide.score` schicken.
- [x] RAM-Guard, Vorentladen, Warmup und `keep_alive=0`-Entladen am Ende für Logit-Modelle nutzen.

### Task 4: Verifikation

**Files:** keine weiteren Änderungen, außer durch Fehlerbehebung.

- [x] Relevante neue Tests und `tests/test_decide.py` ausführen.
- [x] Danach `python3.14 -m pytest -q` ausführen.
- [x] Ergebnisse und exakten tagged Benchmark-Befehl berichten; nicht committen und Benchmark nicht ausführen.
