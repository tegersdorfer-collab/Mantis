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
