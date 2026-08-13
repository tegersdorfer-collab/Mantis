# Mantis Forge — semi-autonomes Entwicklungssystem

**Datum:** 2026-08-13
**Status:** Design freigegeben, Implementierungsplan offen

---

## 1. Ziel

Mantis soll ohne Prompt-Arbeit weitergebaut werden. Ein Dauerläufer nimmt sich
Aufgaben, entwickelt sie mit Claude Code fertig, prüft sich selbst und merged
nach `main` — solange der Mac an ist. Timo steuert per Dashboard und per
Nachfrage an Mantis, nicht per Prompt.

**Abgrenzung zu `core/skill_factory.py`:** Die Skill-Factory erzeugt zur Laufzeit
einzelne, AST-sandboxed Tool-Dateien per lokalem LLM (max. 5/Tag). Die Forge
entwickelt vollwertige Features über mehrere Dateien, mit Tests und Review.
Beide bleiben bestehen und stören sich nicht.

## 2. Architektur

Eigenes Modul im Mantis-Repo, eigener launchd-Job `com.mantis.forge`, **getrennt
vom Assistant-Prozess**. Die Forge darf abstürzen, ohne den Concierge mitzureißen.

```
forge/
  daemon.py      Hauptschleife (launchd-Entry)
  queue.py       Task-Store
  ideas.py       Ideen-Generator bei leerer Queue
  pipeline.py    Stufen-Maschine pro Task
  runner.py      claude -p Wrapper + stream-json Parsing
  budget.py      Limit-Tracking, Reserve, Backoff
  gate.py        Tests/Lint/Review → Merge-Entscheidung
  restart.py     Merge- und Neustart-Etikette
  worktree.py    git-worktree Verwaltung
  journal.py     Ereignis-Log
web/routers/forge.py     API-Endpunkte
web/routers/activity.py  GET /api/activity (Aktivitätssignal)
core/skills/forge.py     Mantis-Tool forge_status
```

Gearbeitet wird nie in `~/Mantis`, sondern in `git worktree`s unter
`~/Mantis-forge/<task-id>/`. Der laufende Mantis bleibt auf `main`, bis ein
Merge das Gate passiert.

**Schnittstellen-Grenze:** Die Forge importiert keine Mantis-Interna. Sie spricht
mit dem laufenden Mantis ausschließlich über HTTP (`/api/activity`, `/health`,
Benachrichtigungs-Endpunkt). Dadurch bleiben beide Prozesse unabhängig
deploybar und die Forge testbar, indem man die HTTP-Antworten stubbt.

## 3. Arbeitsquelle

Prioritätsbasierte Queue in Postgres, gespeist aus drei Quellen:

1. **ROADMAP.md** — einmalig geparst, offene Items werden Tasks. Die als extern
   blockiert markierten Punkte (WhatsApp/Meta-API, HTTPS/Tailscale-Cert, Free
   Dictation, MLX-Fine-Tuning, Ambient Listening, Google-Takeout) werden
   übersprungen.
2. **Timo** — Zuruf an Mantis („bau mal die X5-Weckroutine") landet mit hoher
   Priorität in der Queue.
3. **Ideen-Generator** — läuft, wenn die Queue unter 3 offene Items fällt. Ein
   read-only Agent liest ROADMAP.md, `git log`, TODO/FIXME-Kommentare, die
   Fehlerdaten aus `core/log_observability.py` und Test-Lücken, und schlägt 3–5
   Tasks mit Begründung und Größenschätzung vor. **Deckel: max. 5 neue Ideen pro
   Tag**, sonst wächst die Queue schneller als sie abgearbeitet wird.

## 4. Pipeline

Immer nur **ein** Task gleichzeitig, immer nur **ein** `claude`-Prozess. Jede
Stufe ist ein eigener headless Run (`claude -p --output-format stream-json`) mit
frischem Kontext; die Übergabe zwischen Stufen läuft über Artefakte auf der
Platte, nicht über Gesprächsverlauf.

| Stufe | Inhalt | Artefakt |
|---|---|---|
| **Spec** | Autonome Brainstorming-Variante: keine Rückfragen; getroffene Annahmen werden explizit als „Annahme:" dokumentiert | `docs/superpowers/specs/…-design.md` |
| **Plan** | `writing-plans` zerlegt in Phasen | `docs/superpowers/plans/…-plan.md` |
| **Implement** | `executing-plans` + TDD, **ein Run pro Plan-Phase** | Commits im Worktree |
| **Review** | Frischer Agent, sieht nur Spec + Diff, nicht den Entstehungsverlauf | JSON-Verdict `{verdict, findings[]}` |
| **Fix** | Nur bei Verdict `fail`: max. 2 Nachbesserungsrunden, dann `parked` | Commits |

**Zustände:**
`queued → speccing → planning → implementing → reviewing → gating →
awaiting_restart_window → merged`
Nebenzustände: `parked`, `failed`, `paused_ratelimit`, `paused_user`.

Zustand liegt in der DB, Artefakte auf der Platte. Jeder Absturz ist auf
Stufengrenze wiederaufsetzbar — die Forge nimmt beim Start den zuletzt
gespeicherten Zustand und wiederholt höchstens die angefangene Stufe.

## 5. Gate

Rein deterministisch, kein LLM. Gemerged wird nur, wenn **alle** Bedingungen
erfüllt sind:

- `pytest` komplett grün — die volle Suite, nicht nur betroffene Tests
- `ruff check` ohne Befund
- Der Diff berührt keine Sperrzone (siehe §9)
- Diff kleiner als 800 geänderte Zeilen; darüber → `parked` zur Sichtung
  (das ist dann kein Task mehr, sondern ein Projekt)
- Review-Verdict = `pass`

Scheitert eine Bedingung, bleibt der Worktree stehen und der Task geht auf
`parked`. Nichts wird verworfen.

## 6. Merge- und Neustart-Etikette

Merge und Neustart sind **eine atomare Einheit**. Ein Merge ohne Neustart würde
`main` und den laufenden Prozess auseinanderlaufen lassen; ein späterer
Crash-Restart zöge dann ungeprüft neuen Code. Deshalb wartet auch der Merge,
wenn der Neustart warten muss.

### 6.1 Aktivitätssignal

Neuer Endpunkt `GET /api/activity` im Mantis-Backend, liefert
`{active: bool, reason: str, idle_seconds: int}`. `active` ist wahr, wenn
mindestens eines zutrifft:

- jüngste Zeile in `chat_messages` ist **jünger als 5 Minuten**
- eine Voice-Konversation läuft (`core.voice._conversation_active`)
- eine Dashboard-SSE/WebSocket-Verbindung besteht **und** hatte in den letzten
  5 Minuten Verkehr
- im Orchestrator läuft gerade ein Turn oder Tool-Call
- ein Hardware-Job läuft (X5 fährt)

Für den SSE-Fall muss die bestehende Verbindungsverwaltung um einen
Zeitstempel des letzten Ereignisses ergänzt werden.

### 6.2 Ablauf

1. **Braucht der Merge überhaupt einen Neustart?** Berührt der Diff nur `docs/`,
   `tests/`, `bench/` oder `*.md`, wird sofort gemerged, ohne Frage und ohne
   Neustart.
2. Sonst `GET /api/activity` abfragen:
   - **aktiv** → nicht mergen, nicht fragen. Alle 2 Minuten erneut prüfen.
     Nach 6 Stunden ohne Fenster → `parked` mit Hinweis im Dashboard.
   - **passiv** → weiter zu 3.
3. **Anfrage** über den zuletzt von Timo genutzten Kanal (Telegram, sonst
   Dashboard-Toast): *„‹Task› ist fertig und geprüft — kurz neustarten? Wenn du
   nichts sagst, mach ich's in 30 Sekunden."*
4. **30-Sekunden-Countdown:**
   - Timo widerspricht („nein", „warte", „später") → Verschiebung um 30 Minuten,
     danach erneut ab 2. Nach **3 Ablehnungen** → `parked`, Freigabe nur noch
     manuell über den Dashboard-Button.
   - Timo wird während des Countdowns aktiv (neue Nachricht, Voice) → Countdown
     abbrechen, zurück zu 2.
   - Keine Reaktion → weiter zu 5.
5. **Merge + Neustart:** `git merge --no-ff` nach `main`, Worktree entfernen,
   dann die etablierte Prozedur: falls `/tmp/mantis_pid.txt` existiert, den
   darin stehenden Prozess beenden (er läuft oft außerhalb von launchd — ein
   `kickstart` allein startet sonst eine zweite Instanz mit Port- und
   Telegram-Polling-Konflikt) → 3 s warten →
   `launchctl kickstart -k gui/501/com.mantis.assistant` → `/health` pollen.
6. **Sicherheitsnetz:** Antwortet `/health` nicht innerhalb von 30 Sekunden →
   automatischer `git revert` des Merges, erneuter Neustart, Task auf `parked`.
   Der Concierge ist damit nie länger als etwa eine halbe Minute weg.

Diese Anfrage ist die **einzige** Push-Nachricht im gesamten System. Alles
andere ist Pull.

## 7. Budget und Limits

`forge/budget.py`, drei Ebenen:

**Buchhaltung.** Jeder `claude -p`-Run liefert im `stream-json` seinen
Token-Verbrauch. Der Daemon schreibt ihn in die DB und kennt dadurch jederzeit
den Verbrauch der Forge im laufenden 5-Stunden-Fenster.

**Reserve für Timo: 15 %.** Diesen Anteil des Fensters rührt die Forge nicht an,
damit interaktive Claude-Code-Nutzung nie am eigenen Loop scheitert. Reserve
erreicht → schlafen bis zum Fenster-Reset.

**Degradations-Leiter statt hartem Stopp:**

1. Volles Budget → volle Pipeline, auch große Tasks
2. Unter 50 % → nur noch kleine Tasks (Tests, Docs, isolierte Bugfixes) auf dem
   schlanken Pfad ohne Spec- und Plan-Stufe
3. Reserve erreicht oder Rate-Limit-Fehler → Reset-Zeitpunkt aus der
   Fehlermeldung parsen, Task auf `paused_ratelimit`, exakt bis dahin schlafen,
   danach **auf derselben Stufe** weiter
4. Wochenlimit erschöpft → Daemon idlet, Dashboard zeigt „wartet auf Reset am
   ‹Datum›"

Weil jede Stufe ihr Artefakt auf der Platte hat, geht bei keiner dieser Pausen
Arbeit verloren.

**Zusätzlich:** Die Forge pausiert, solange eine interaktive Claude-Code-Session
läuft (`pgrep`). Sonst konkurrieren beide um dasselbe Limit und um die 16 GB RAM.

## 8. Sichtbarkeit und Steuerung

**Dashboard-View „Werkstatt"** im PWA (Port 7779): aktueller Task mit Stufe und
Live-Tail, Queue, letzte 20 Journal-Ereignisse, Budget-Anzeige (verbraucht /
Reserve / nächster Reset), Buttons für Stop, Skip, Hochpriorisieren, Verwerfen,
und Freigabe geparkter Neustarts.

**Mantis-Tool `forge_status`:** Auf „was hast du heute gebaut?" oder „woran
arbeitest du gerade?" antwortet Mantis aus dem Journal in normaler Sprache, über
Telegram oder Voice. Keine automatischen Meldungen — einzige Ausnahme ist die
Neustart-Frage aus §6.2.

**Not-Aus, drei Wege:** Datei `~/.mantis-forge-stop`, Dashboard-Button, Zuruf an
Mantis. Der Daemon hält nach der laufenden Stufe an. Zusätzlich stoppt er sich
selbst nach **3 aufeinanderfolgenden gescheiterten Tasks** (Fehler-Spirale).

## 9. Sicherheit

**Sperrzonen** — ein Diff, der eines davon berührt, wird nie automatisch
gemerged:

- `.env` und alles Secret-artige
- `data/` (Laufzeitdaten, Tokens, Caches)
- `forge/gate.py` und `forge/budget.py` — die Forge darf ihre eigenen Schranken
  nicht verschieben
- `~/Library/LaunchAgents/`
- `scripts/` mit Systemeingriff (`fix_bluetooth.sh`)

Die Forge darf sich generell nicht selbst umbauen. Tasks, die `forge/` betreffen,
laufen die Pipeline normal durch, enden aber immer auf `parked` zur manuellen
Sichtung.

## 10. Datenmodell

Zwei neue Tabellen:

**`forge_tasks`** — `id`, `title`, `description`, `source`
(roadmap/timo/generator), `priority`, `state`, `stage`, `worktree_path`,
`branch`, `spec_path`, `plan_path`, `attempts`, `refusals`, `created_at`,
`updated_at`, `parked_reason`

**`forge_journal`** — `id`, `task_id`, `ts`, `kind` (stage_start, stage_done,
gate_pass, gate_fail, merged, reverted, paused, idea_added, …), `message`,
`tokens_in`, `tokens_out`

Das Journal ist die Quelle für Dashboard-View und `forge_status`.

## 11. Fehlerbehandlung

| Fall | Verhalten |
|---|---|
| Claude-Run bricht ab / Timeout | Stufe einmal wiederholen, dann `parked` |
| Tests rot | `parked`, Worktree bleibt zur Sichtung |
| Review `fail` | max. 2 Fix-Runden, dann `parked` |
| Merge-Konflikt | `parked` (tritt selten auf, da nur ein Task läuft) |
| Health-Check nach Neustart rot | Auto-Revert, Neustart, `parked` |
| Rate-Limit | `paused_ratelimit`, Schlaf bis Reset, gleiche Stufe weiter |
| Daemon-Absturz | launchd `KeepAlive`, Wiederaufsetzen auf letzter Stufengrenze |
| 3 Fehlschläge in Folge | Daemon stoppt sich selbst, Dashboard zeigt Grund |

## 12. Nicht im Umfang

Parallele Tasks (16 GB RAM), GitHub-PR-Integration, Push-Benachrichtigungen
außer der Neustart-Frage, API-Key-Fallback bei erschöpftem Abo (kostet echtes
Geld — bleibt Timos Entscheidung), Selbstoptimierung der Forge.

## 13. Annahmen

- `claude -p --output-format stream-json` liefert pro Run verwertbare
  Usage-Daten; falls nicht, schätzt `budget.py` konservativ aus der Run-Dauer und
  fährt die Reserve entsprechend höher.
- Rate-Limit-Fehler enthalten einen Reset-Zeitpunkt. Falls nicht, wird ein
  Standard-Backoff von 60 Minuten verwendet.
- Der Mac schläft nachts nicht dauerhaft durch. Tut er es doch, verliert die
  Forge nur Laufzeit, keinen Zustand.
