# Handoff: Nutzungs-Challenge — Mantis so bauen, dass Timo ihn wirklich nutzt

> Erstellt 2026-10-13 auf dem Windows-PC, zum Weitermachen auf dem Mac.
> **Start auf dem Mac:** `git pull`, dann in Claude Code: *„Lies docs/HANDOFF-nutzungs-challenge.md und mach da weiter."*

---

## 1. Ausgangslage (ehrlich)

- Timo nutzt Mantis aktuell **gar nicht** („0").
- Mantis hat ~690 Dateien, 57+ Tools, Memory-Graph, Skill-Factory, Voice, Robot, Flipper, Skilltree,
  Jev, Forge, God's Eye … — Features sind nicht das Problem.
- Timos eigene Diagnose: *„Mein Ansatz war falsch, so schnell wie möglich so viele Features wie möglich.
  Ich hätte mich immer auf ein Feature konzentrieren sollen, das wirklich Nutzen bringt."*
- Was Timo **wirklich** nutzt:
  - **Filmroulette** (die DB fast täglich) — klares, wiederkehrendes Problem („was schauen wir?"),
    kleiner Scope, sofortiger Nutzen.
  - **PC-Optimierung** (Lüfter/FanControl/OpenRGB) — konkreter Anlass, einmal gebaut, wirkt automatisch.
- Mantis dagegen startete bei *„was könnte ein Assistent alles?"* statt bei *„was nervt mich?"* —
  darum gibt es keinen Moment am Tag, in dem Timo ihn braucht.
- Am 2026-10-13 war das Backend (`http://100.107.172.123:7779/health`) vom Windows-PC aus **nicht
  erreichbar** (Mac aus/schlafend oder Mantis läuft nicht). Letzter Commit: 2026-09-23.

## 2. Die Challenge

Timo hat Claude die Challenge gestellt: **Mantis so gestalten, dass er ihn wirklich nutzt.**
Timo hilft so gut er kann.

**Erfolgskriterium:** Timo nutzt Mantis an **≥ 5 von 7 Tagen**, ohne sich zu zwingen —
und das auch **nach 4 Wochen** noch.

## 3. Regeln (nicht verhandelbar)

1. **Kein neues Feature, das Timo nicht in der Vorwoche mindestens 2× gebraucht hätte.**
2. **Ein Use-Case zur Zeit.** Erst wenn der zuverlässig läuft *und* genutzt wird, kommt der nächste.
3. **Kleinste Version zuerst**, eine Woche nutzen, dann iterieren.
4. **Abspecken statt anbauen:** alles, was nicht dem aktuellen Use-Case dient, wird per Flag
   **deaktiviert** (nicht gelöscht). Proaktive/autonome Nachrichten bleiben aus
   (`AUTONOMOUS_MESSAGES_ENABLED` Default aus), außer der Use-Case braucht sie.
5. **Dort erreichbar sein, wo Timo sowieso ist** — vermutlich Telegram am Handy, nicht ein Dashboard.
6. **Zuverlässigkeit vor Breite:** Ein Assistent, der manchmal nicht antwortet, wird nie Gewohnheit.

## 4. Vorgehen

| Phase | Was | Ergebnis |
|---|---|---|
| 0. Bestandsaufnahme | Läuft das Backend? launchd/KeepAlive? Ist der Mac dauerhaft an? Nutzungs-Baseline aus der DB (Nachrichten pro Tag der letzten 8 Wochen) | Ehrlicher Ist-Zustand |
| 1. Discovery | Timos Alltag verstehen (Fragen unten). **Nichts bauen.** | Liste echter, wiederkehrender Schmerzpunkte |
| 2. Auswahl | **Einen** Use-Case wählen, der mehrmals pro Woche auftritt | Ein Satz: „Mantis hilft mir bei X, wenn Y" |
| 3. Abspecken + Bauen | Rest per Flags aus, Use-Case zuverlässig + schnell machen, Nutzungs-Logging (Tag, Kanal, Use-Case) | Minimal-Mantis, der eine Sache gut kann |
| 4. Messen | 1 Woche nutzen, dann Zahlen anschauen + Timo fragen, was genervt hat | Entscheidung: verbessern / nächster Use-Case / Use-Case verwerfen |

## 5. Offene Discovery-Fragen an Timo (noch unbeantwortet)

1. Wie sieht ein normaler Tag aus? (Schule/Studium/Job, wann Handy, wann PC?)
2. Wo ist er sowieso ständig? (WhatsApp, Telegram, Discord, Browser, …)
3. Was vergisst er regelmäßig / muss er sich mühsam merken?
4. Was macht er mehrmals pro Woche, das nervt oder Zeit kostet?
5. Warum funktioniert das Filmroulette — was ist der Moment, in dem er es öffnet?
6. **Läuft der Mac dauerhaft oder ist er oft aus?** → entscheidet, ob Mantis auf dem Mac bleiben kann
   oder auf den Windows-PC / einen kleinen Always-on-Server umziehen muss. (Wichtigste Frage.)

## 6. Ideen-Richtung (nur Hypothesen, erst nach Discovery entscheiden)

- Andocken an das, was schon funktioniert: z. B. Filmroulette per Telegram („was schauen wir heute?").
- Ein einziger Telegram-Bot, der genau eine Sache gut macht, statt „alles".
- Wiederverwendbar aus dem bestehenden Code: Telegram-Anbindung, Memory-Layer — **nur wenn der
  Use-Case es braucht.**

## 7. Erste Schritte für Claude auf dem Mac

1. `git pull`, Backend-Status prüfen (`/health`, launchd), kurz berichten.
2. Nutzungs-Baseline aus der DB ziehen (Nachrichten/Tag nach Kanal, letzte 8 Wochen) — als
   ehrliche Startlinie.
3. Timo die Discovery-Fragen aus Abschnitt 5 stellen (kurz, im Gespräch, nicht als Formular).
4. **Erst danach** einen Use-Case vorschlagen und Timo entscheiden lassen.
5. Fortschritt der Challenge unten im Log festhalten.

## 8. Log

- **2026-10-13** — Challenge gestartet (Windows). Diagnose + Regeln festgelegt, Handoff geschrieben.
  Backend vom PC aus nicht erreichbar.
2026-10-13 — Phase 0 (Mac). Backend läuft (launchd `com.mantis.assistant`, KeepAlive, seit 03.10.; `/health` antwortet, verlangt aber Auth). Mac seit 12 Tagen an, Sleep durch Assertions verhindert; Tailscale-IP 100.107.172.123 aktiv → die Nichterreichbarkeit vom PC lag vermutlich an Auth/Port, nicht am Mac. Baseline: `chat_messages` ist komplett leer (Chat wird nicht persistiert!), `llm_usage` purpose=chat-agent nur an 3 Tagen in 8 Wochen (09.09., 22.09.), seitdem 0. Hintergrundjobs laufen dagegen weiter (~1000 LLM-Calls, 1986 Fehler, v. a. `agent_fallback: api down` bis 06.10., llama-server-Timeouts). Fazit: Nutzung = 0, Mantis beschäftigt sich hauptsächlich mit sich selbst.
2026-10-13 — Discovery (Mac). Alltag: 8 Uhr Handy, 9–12 / 16–18 / 20–23 am PC/Mac. Kanäle: WhatsApp ständig, Telegram nur für den Bot. Schmerzpunkte: (a) Mac/PC-Stand von Claude/Codex auseinander, häufige Handoffs → kein Mantis-Thema, separat lösen; (b) Küche: vergessen einzukaufen, vergessen mitzukochen, Sachen verschimmeln; kocht 2–3×/Woche, kauft 2×/Woche ein, Haushalt mit Eltern (mehrere Köche → voller Vorratsbestand nicht pflegbar), Rezepte von KI. Vorschlag „Rezept → Einkaufsliste“ war Timo zu wenig („krasser“). Küchen-Use-Case auf Wunsch von Timo GEPARKT; Fokus vorerst auf Mantis selbst.
2026-10-13 — Phase 3a live (Mac). „Mantis gesund“ + Discord gemergt (main 572a8af) und neu gestartet:
- Chats 365 Tage gespeichert, Nutzung messbar über `GET /api/usage/daily` (Europe/Berlin, Kanal).
- 8 Hintergrundjobs per Flag aus (Insights, Patterns, Trainingsplan, News, Reflexion, Konsolidierung, Task-Deep, Background-Review), Accountability aus. Extra-Dienste gestoppt (launchctl bootout): forge, forge-bot, typesafe-console-watch, gods-eye-view.
- Chat über Cloud: OpenRouter `openai/gpt-6-luna` → `google/gemini-3.1-flash-lite` → lokal. Modellwahl per Benchmark (Branch bench/openrouter-chat: Luna Judge 8,0, ~0,01 $/100 Nachrichten, 2,6 s Median). Timo: voller Kontext darf in die Cloud, Hintergrund-Verarbeitung privater Daten bleibt lokal.
- Kanal: Discord (DM, nur Timo), Reminder per Discord; Telegram läuft parallel. WhatsApp verworfen (Sperr-Risiko/Ausweis-SIM).
- Erster echter Turn 22:47 via Discord, Antwort in 3 s, 0,0004 $.
- In Arbeit: Chat-Kette v2 (OpenRouter-Luna → Anthropic Haiku 5.5 → ChatGPT-Abo-Luna via „ChatGPT plan usage“ → lokal), Branch feat/chat-chain-v2.
- Ideen-Parkplatz: Küche (Rezept→Einkaufsliste, Schimmel), Mantis anrufen (Discord-Sprachkanal), OpenRouter-Guthaben-Warnung.
- Nächster Schritt Challenge: 1 Woche nutzen, dann `/api/usage/daily` + Timo fragen, was genervt hat.
2026-10-13 23:39 — Kette v2 live: OpenRouter-Luna → Anthropic Haiku 5.5 → ChatGPT-Abo-Luna (Login erfolgt) → lokal. Test-Suite isoliert gegen mantis_test. Start-Warnungen behoben (COROS querySleepOverview, Discord-Intent, Skill-Entwürfe archiviert, Telegram-Shutdown, Jev-422-Fallback).
2026-10-14 Nacht — Timo hat beauftragt: 1 Gym-Coach → 3 Merk-Inbox → 2 Agenten-Fernbedienung (nachts mergen + Neustart erlaubt, nur mit Sol-APPROVE). Stand beim Abbruch (Claude-Kontingent leer): Spec-Läufe gestartet. Merk-Inbox-Spec fertig: .worktrees/merk-inbox/docs/specs/2026-10-14-merk-inbox.md (Branch feat/merk-inbox, uncommittet). Gym-Coach-Spec lief noch: .worktrees/gym-coach/docs/specs/2026-10-14-gym-coach.md (Branch feat/gym-coach). Nächste Schritte: Specs prüfen → Implementierung per codex-task (Luna) → Review Sol → Merge/Neustart. Gym-Coach-Annahme: Gym, Kraft, kg.
2026-10-14 00:55 — Nachtstand: Alle drei Features implementiert und committet (feat/gym-coach, feat/merk-inbox, feat/agent-remote). Codex-Kontingent um 00:49 erschöpft (Reset 03:03). Merk-Inbox: Sol-Review CHANGES_REQUESTED (10 Befunde + 2 Security-Check), Fix-Lauf abgebrochen mit Teiländerungen in metadata.py (uncommittet, Review in .review-sol-1.md) → ab 03:07 Luna neu starten. Agent-Remote: Sol-Sicherheitsreview lief bei Kontingent-Ende (Ergebnis prüfen) + 2 Security-Check-Befunde (git-Härtung, Ergebnistexte nicht als Assistant-Turn). Gym-Coach: Review durch Claude-Subagent (Sonnet) als Ersatz.
2026-10-14 05:00 — Merk-Inbox LIVE (main 5bd689e, nach 4 Sol-Runden; Live-Fix: große Artikel-Seiten nur anlesen, Wikipedia-Titel aus URL; SSRF-Sperre live geprüft). Codex-Kontingent erneut leer bis 08:14 → Gym-Coach Runde 4 (Altdaten-Zuordnung: Timos 5654 Sätze aus AlphaProgression mit englischen Namen wie 'Bench Press' + 9 Sol-Befunde) und Agent-Remote Runde 4 (instabiler Test, git-lfs-Ausnahme) laufen per Claude-Subagent; Reviews danach wieder Sol.
