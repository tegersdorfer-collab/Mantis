# Spec: Alles in die Cloud außer Embeddings + schnellere Sprachpipeline

Stand 2026-10-14. Entscheidung Timo (14.10.): „alles in die Cloud außer Embeddings“, Datenschutz
vorerst vernachlässigen, Sprachsteuerung soll wieder öfter genutzt werden (war bisher „nicht
wirklich lohnenswert“), kein Komplett-Sprache-zu-Sprache (Ebenen, in denen nur einzelne Teile
gebraucht werden, bleiben getrennt). Danach werden die lokalen Ollama-LLMs gelöscht.

## Ist-Stand (lokal per Ollama)
| Setting | Modell | Nutzer im Code |
|---|---|---|
| `VOICE_AGENT_MODEL` (keep_alive -1) | gemma4:e2b → Fallback `CLAUDE_CHAT_MODEL` (Haiku 4.5) | `main.py` Voice-Agent |
| `ADDRESS_CHECK_MODEL` | gemma4:e2b | `core/voice.is_addressed_to_mantis` |
| `AGENT_MODEL_FAST` | qwen3.5:9b | `core/fast.py`, `core/local_decide.py`, `core/voice.py`, `memory/extractor.py` |
| `AGENT_MODEL_STRONG` | gemma4:e2b | letzte Stufe Chat-Kette (`core/backends/factory.py`, `ollama.py`) |
| `OLLAMA_MODEL`/`BG_DEFAULT_MODEL`/`BG_REASONING_MODEL` | qwen3.5:9b | `main.py`, `llm/routed.py`, `tools/news/geolocate.py`, `core/skills/uiauto.py` |
| `BG_CODE_MODEL` | ornith-1.5:9b-sys | `llm/routed.py`, `core/skill_request.py` |
| `VISION_MODEL` | qwen3.5:9b | `core/vision.py`, `web/routers/nutrition.py` (+ Inbox-Vision) |
| `LZG_EMBED_MODEL` | qwen3-embedding:0.6b | `llm/local.py` — **bleibt lokal** |
Dazu: Aufwärmen lokaler Modelle beim Start (RAM-Spitze), totes `.env`-Setting `VOICE_CLOUD_MODEL`.

## Ziel
Jeder LLM-Aufruf außer Embeddings läuft über die Cloud-Kette
**OpenRouter `openai/gpt-6-luna` → Anthropic `claude-haiku-5-5` → ChatGPT-Abo `gpt-6-luna`**
(bestehende Backends aus `core/backends/`). Keine lokale LLM-Stufe mehr; scheitern alle Stufen,
gibt es eine klare Fehlermeldung statt Ollama-Fallback. Ollama wird nur noch für
`qwen3-embedding:0.6b` gebraucht.

1. **Eine zentrale Fabrik** (z. B. `core/backends/factory.py::cloud_chain(purpose, *, reasoning=..., timeout=...)`),
   die alle bisherigen lokalen Nutzer verwenden. Varianten über Parameter, nicht über neue Modelle:
   - `chat` (wie heute), `voice` (kurze Antworten, Reasoning `low`/minimal, Streaming),
   - `fast` (Klassifikation/Entscheidung/Adress-Check/Extraktion: kleine Antworten, kurzes Timeout,
     strukturiertes JSON wo heute gefordert), `background` (bg/reasoning/code), `vision` (Bild-Input;
     OpenRouter-Luna und ChatGPT-Abo akzeptieren Bilder, Anthropic ebenfalls).
2. **Datenschutzgrenze aufgehoben:** Die bisherige Regel „Dashboard-/Hintergrund-Routen mit privaten
   Daten nur lokal über `bg_llm`“ entfällt. Kommentare/Spec-Verweise aktualisieren.
3. **Lokale Stufen und Aufwärmen entfernen:** keine Ollama-Chat/Generate-Aufrufe mehr erreichbar,
   wenn `LLM_LOCAL_ONLY=False`. `LLM_LOCAL_ONLY=True` bleibt als Flag, führt aber zu einer klaren
   Startwarnung, dass die lokalen Modelle entfernt sind. Startlog nennt die Kette je Zweck.
   `VOICE_AGENT_KEEP_ALIVE`, totes `VOICE_CLOUD_MODEL` und lokale Warmups entfernen.
4. **Kosten/Logging:** alle Zwecke landen wie heute in `llm_usage` mit `purpose`
   (`chat-agent`, `voice`, `fast`, `background`, `vision`).

## Sprachpipeline
Bleibt lokal (kein LLM, klein, schneller als jeder Netzweg): Silero-VAD, openWakeWord, Parakeet-STT,
Kokoro-TTS (Piper-Fallback). Neu:
1. **Voice-Agent über die Cloud-Kette** (`voice`-Variante), mit Tools wie bisher.
2. **Streaming durchreichen:** `orch.voice_respond(text, stream_cb)` wird im WebSocket-Pfad mit
   Callback aufgerufen; fertige Sätze gehen sofort in die vorhandene satzweise TTS-Streaming-Logik
   (`core/tts.py`, `sende_antwort`), sodass der erste Ton nach dem ersten Satz kommt statt nach der
   ganzen Antwort. Reihenfolge der Audio-Blöcke bleibt garantiert. HTTP-Pfad `/api/voice/segment`
   und Clients ohne `hello/stream` funktionieren unverändert (Gesamtantwort).
3. **Adress-Check** (`is_addressed_to_mantis`) über `fast`-Variante; Timeout ≤ 3 s, bei Fehler
   wie heute konservativ entscheiden.
4. **Latenz messen:** pro Voice-Turn eine INFO-Zeile + `events_log`-Eintrag `voice_latency` mit
   ms für: STT, Adress-Check (falls), erstes Token, erster TTS-Block, Gesamt, Modell/Stufe.
   VAD-Stille (`SILENCE_MS_TO_STOP`=800) bleibt, wird aber ein Setting `VOICE_SILENCE_MS`.

## Nicht im Scope
Cloud-TTS/-STT-Anbieter (nach Latenzmessung separat entscheiden; dafür bräuchte Timo neue
Accounts), Sprache-zu-Sprache, Embeddings, iOS-/Desktop-Client-Änderungen.

## Tests (TDD, gegen `mantis_test`, HTTP der Anbieter gemockt)
- Fabrik: jede Variante liefert die Kette Luna → Haiku → ChatGPT-Abo ohne lokale Stufe; Fehler aller
  Stufen → definierte Exception/Meldung; `LLM_LOCAL_ONLY` verhält sich wie beschrieben.
- Für jeden bisherigen lokalen Nutzer (Tabelle oben): nutzt die Cloud-Kette (kein `OllamaBackend`/
  Ollama-HTTP außer Embeddings). Ein Test, der im Produktionspfad jede Ollama-Chat/Generate-URL als
  Fehler wertet.
- Vision: Bild wird als Image-Input im jeweiligen Backend-Format übergeben.
- Voice: `stream_cb` → TTS-Blöcke kommen in Satzreihenfolge, erster Block vor Ende der LLM-Antwort;
  HTTP-Pfad unverändert; Latenzzeile/-event wird geschrieben.
- Gesamtsuite + `ruff` grün.
