# Spec: Sprachsteuerung menschlicher und schneller

Stand 2026-10-14. Entscheidung Timo: alle Punkte der Reihe nach umsetzen. Ziel: erster Ton
gefühlt ~1 s, echte Antwort ~1,5–2 s (ohne Tool), Gesprächsgefühl statt Kommando-Gerät.
Messung live über `events_log` `voice_latency` (Phasen: stt_ms, tool_select_ms, context_ms,
llm_calls, tool_exec_ms, first_token_ms, first_tts_block_ms, total_ms).

Ausgangslage 22:08: STT 1,44 s, Tool-Auswahl 1,04 s, Haiku TTFT 1,05 s, erster Ton 4,6 s.

## Runde 0 (läuft, `perf/voice-speed2`)
STT-Warmup, Tool-Auswahl parallel zum LLM, erster TTS-Block an Kommagrenze.

## Runde 1 – Füller („Mhm“, „Moment, ich schau kurz nach“)
- Pools mit Nicos Stimme, einmalig über Cartesia erzeugt und als OGG auf Platte gecacht
  (`data/voice_fillers/`, neu erzeugen wenn Stimme/Text sich ändert); kein Cartesia-Aufruf pro Turn.
- Pool „zugehört“ (kurz: „Mhm.“, „Okay.“, „Moment.“): nur wenn ~800 ms nach Sprachende
  (Setting) noch kein Antwort-Audio gesendet wurde.
- Pool „nachschauen“ („Ich schau kurz nach.“, „Moment, ich check das.“, „Sekunde.“): sobald der
  erste Tool-Call eines Turns startet und noch kein Füller lief.
- Max. ein Füller pro Turn, nie zweimal derselbe hintereinander, nur Voice-WebSocket mit
  Audio-Streaming, nicht in Text/Chat-Historie. Echte Antwort reiht sich nahtlos dahinter.

## Runde 2 – Vorwissen + Reflexe
- **Lagebild** im dynamischen Prompt-Teil (nach Cache-Breakpoint): heutige/morgige Termine,
  offene Aufgaben (Top N), letzte Nacht Schlaf + Ruhepuls/HRV, Wetter heute, nächster Reminder.
  Hintergrund-Refresh alle 5 min (Cache im Speicher), Turn liest nur den Snapshot (0 ms).
  Prompt: Fragen dazu direkt aus dem Lagebild beantworten, Tools nur für Details/Änderungen.
- **Reflexe**: Voice nutzt `core/fast_commands.py` (Uhrzeit, Datum, Timer, Stopp, Lauter/Leiser,
  Musik, Licht …) vor dem LLM; Antwort lokal in Textform → TTS. Nur eindeutige Treffer.

## Runde 3 – Mitdenken + Satzende vorhersagen
- **Teil-Transkription während des Sprechens** (Parakeet auf wachsendem Puffer, z. B. alle
  ~600 ms im STT-Executor); mit Teiltext vorab Tool-Auswahl, Gedächtnis-Suche und Cache-Warmup
  (Anthropic `max_tokens: 0` Pre-Warming falls verfügbar, sonst nur Kontext vorbereiten).
  Bei Sprachende nur noch Rest transkribieren bzw. finalen Text nehmen; Vorarbeit verwerfen,
  wenn sich der Text stark ändert.
- **Satzende-Erkennung**: kleines lokales Turn-Modell (z. B. Smart-Turn-ONNX, < 100 MB) oder
  Heuristik (Grammatik vollständig + Fallton) senkt die Stille-Schwelle dynamisch
  (z. B. 350 ms bei „fertig“, 1200 ms bei „klingt unvollständig“). Setting zum Abschalten.

## Runde 4 – Unterbrechen (Barge-in)
- Spricht Timo, während Mantis spricht (VAD auf dem Mikro, mit Echo-Schutz: Wakeword oder
  Energie über Wiedergabepegel), stoppt die Wiedergabe sofort, laufende Generierung/TTS wird
  abgebrochen, das Gesagte wird als neuer Turn verarbeitet. Gesprochener Teil der alten Antwort
  wird als solcher im Verlauf markiert.
- Desktop-Client (`apps/desktop`) braucht dafür: Wiedergabe abbrechen auf Server-Signal,
  Mikro während Wiedergabe offen lassen (Echo-Cancellation des Browsers nutzen).

## Für jede Runde
TDD gegen `mantis_test`, keine echten API-Aufrufe in Tests, Review Sol, Merge + Neustart
(Freigabe Timo 14.10.), danach Live-Test durch Timo und Auswertung `voice_latency`.

## Runde 5 – Vorausdenken (Ideen Timo 15.10., Start erst wenn Teil-STT live misst)
Voraussetzung: Teil-STT läuft live (partial_runs > 0, partial_as_final greift), Adress-Check nicht mehr im kritischen Pfad.
- **Punkte vorab beantworten:** Erkennt das mitlaufende Transkript einen Übergang („zweitens“, „zum zweiten Punkt“, „außerdem“, „nächste Frage“, „und dann noch“), wird der abgeschlossene Teil spekulativ an das LLM geschickt (nur Text, keine Tools). Bei Satzende spielt Antwort 1 sofort, während der Rest läuft. Verwerfen bei Rücknahme („vergiss den ersten Punkt“, „…aber nur wenn“) oder wenn Punkt 2 sich auf Punkt 1 bezieht (dann gemeinsam beantworten).
- **Harmlose Befehle sofort ausführen:** „Mantis, mach das Licht an, ich hatte nen langen Tag …“ → nach abgeschlossener Befehlsklausel + Pause sofort ausführen, wenn: echtes Weckwort am Anfang, Klausel COMPLETE, Aktion auf Allowlist umkehrbarer Aktionen (Licht, Ventilator, Musik an/aus/lauter/leiser/Pause, Timer). Rücknahme im weiteren Satz („doch nicht“, „aber erst in …“) → rückgängig machen. Nie vorab: Nachrichten, Löschen, Termine, Käufe, Mails, alles Externe/Irreversible. Schlussantwort greift beides auf („Licht ist an. …“).
- **Dringendes zuerst:** Ist eine Erinnerung fällig/dringend (z. B. „Kreatin nehmen“), liegt dafür vorab synthetisiertes Audio bereit (Hintergrund, sobald fällig). Beim nächsten Voice-Turn spielt Mantis es sofort als Einstieg statt eines Füllers („Kurz vorab: du musst noch dein Kreatin nehmen.“), dann folgt die eigentliche Antwort. Max. einmal pro Erinnerung, nur echte Fälligkeit, nie bei Barge-in/Reflex-Stopp; gilt als erledigt-gemeldet, nicht als erledigt.
