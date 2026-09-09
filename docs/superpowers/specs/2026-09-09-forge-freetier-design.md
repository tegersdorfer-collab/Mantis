# Forge auf Gratis-Anbietern: autonomer Nachtbetrieb mit zwei Agenten

**Datum:** 2026-09-09
**Status:** Design abgenommen, Plan folgt

## Ziel

Die bestehende Forge-Pipeline ([2026-08-13-mantis-forge-design.md](2026-08-13-mantis-forge-design.md))
läuft künftig nicht mehr auf Claude Code, sondern auf kostenlosen Anbietern:
NVIDIA NIM, Google AI Studio, Antigravity und — in einer eng begrenzten Nische —
Mistral. Zwei Agenten arbeiten nachts parallel, ein Scout hält die Queue gefüllt,
ein zweiter Telegram-Bot nimmt Aufgaben entgegen und holt Merge-Freigaben ein,
und ein eigenständiges Dashboard zeigt, was gerade passiert.

Anthropic- und OpenAI-Abos bleiben aussen vor — nicht aus technischen Gründen,
sondern weil Timo dafür nicht mehr zahlen will und Anthropic Abo-OAuth in
Drittwerkzeugen ohnehin untersagt.

## Warum das nicht "immer läuft"

Die Gratis-Kontingente sind klein. Gemessen am 2026-09-07/09:

| Anbieter | Grenze | Quelle |
|---|---|---|
| Groq | 1000 Anfragen/Tag, **8000 Tokens/Minute** | `x-ratelimit-*`-Header, echt |
| NVIDIA NIM | ~40 Anfragen/Minute, Credit-Pool | keine Header; Community-Angabe 1000–5000 |
| Antigravity | ~20 Agent-Anfragen/Tag | Community-Angabe, von 250 gesenkt |
| Google AI Studio | keine Header | undokumentiert |

Groqs 8000 Tokens/Minute schliessen es für Code-Arbeit praktisch aus — eine
Implement-Stufe mit Dateikontext liegt bei 15–20k Tokens. Kimi K3 braucht
gemessene **208 Sekunden** pro Antwort.

Realistisch sind **20–40 Agent-Läufe pro Nacht**, also **5–8 Tasks**. Das System
ist deshalb auf wenige saubere Tasks ausgelegt, nicht auf Durchsatz. Wer
Dauerbetrieb erwartet, bekommt halbfertige Worktrees.

## Verifizierte Vorbedingungen

Rechte-Aufnahme am 2026-09-09, opencode 1.18.20, vollständig in
[../../../tests/fixtures/permission_probe_opencode.md](../../../tests/fixtures/permission_probe_opencode.md).
Getestet gegen einen Köder ausserhalb des Arbeitsverzeichnisses; die echten
Schlüssel waren nie Ziel.

| Prüfung | Ergebnis |
|---|---|
| `edit: deny` verhindert Schreiben | **ja** — Werkzeug wird aus der Werkzeugliste entfernt |
| `external_directory: deny` gegen `read`-Werkzeug | **ja** |
| `external_directory: deny` gegen `cat ../pfad` | **ja** |
| `external_directory: deny` gegen `python3 -c "open(chr(46)+…)"` | **NEIN — Köder geleakt** |
| Bash-Whitelist bei `task: allow` | **NEIN — Umgehung über Subagent** |
| `task: deny` schliesst beide Löcher | ja |
| Erlaubtes Bash-Kommando bei `task: deny` | funktioniert nicht — Shell verschwindet ganz |
| Konfiguration (j) kann Code schreiben | ja, Köder unberührt |

**Folgerung:** `external_directory` ist Pfad-Erkennung auf Shell-Argument-Ebene,
kein Sandbox. Eine eingegrenzte Shell ist in dieser opencode-Version nicht
konfigurierbar. Für unbeaufsichtigten Betrieb ist ausschliesslich diese
Konfiguration belegt:

```json
"permission": {
  "read": "allow", "edit": "allow", "glob": "allow",
  "grep": "allow", "list": "allow",
  "bash": "deny", "task": "deny",
  "webfetch": "deny", "websearch": "deny",
  "external_directory": "deny"
}
```

Die Agenten bekommen **keine Shell**. Das ist verkraftbar, weil `forge/gate.py`
pytest und ruff ohnehin ausserhalb des Agenten ausführt. Die Implement-Stufe
verliert nur die Fähigkeit, selbst zu iterieren — das kostet Fix-Runden, nicht
Korrektheit.

Damit die Fix-Runde nicht blind rät, muss die **vollständige Fehlerausgabe des
Gates in den Fix-Prompt**: Testnamen, Assertions, Tracebacks, ruff-Befunde. Ein
Agent ohne Shell kann `pytest` nicht selbst nachspielen; alles, was er über den
Fehlschlag weiss, steht im Prompt oder nirgends. Das ist die einzige Stelle, an
der die fehlende Shell echte Arbeit verursacht.

## Getroffene Entscheidungen

| Frage | Entscheidung |
|---|---|
| Nach bestandenem Gate | Branch bleibt stehen, Timo entscheidet den Merge |
| Aufgabenquelle | Scout-Agent hält die Queue gefüllt, Telegram nimmt Timos Aufgaben |
| Scout-Vorschläge | landen ohne Freigabe in der Queue; Timo kann passiv veto einlegen |
| Mistral | nur Commit-Texte aus dem Diff-Titel, kein Repo-Zugriff |
| "Sie besprechen sich" | dedizierter Review-Agent auf anderem Anbieter als der Implementierer |
| Dashboard | eigene App, eigener Port |
| Parallelität | 2 |
| Zeitfenster | nachts |
| Erlaubte Pfade | `tests/`, `docs/`, `scripts/`, `tools/`, `core/skills/` |

`web/` und `forge/` bleiben vorerst vollständig tabu, `core/` bis auf die eine
Ausnahme `core/skills/` — das ist der Ordner, in dem neue Mantis-Fähigkeiten
entstehen, und genau die Arbeit soll der Nachtbetrieb übernehmen. Die Regel ist
also **Erlaubnisliste, nicht Verbotsliste**: was nicht in `tests/`, `docs/`,
`scripts/`, `tools/` oder `core/skills/` liegt, ist gesperrt. Das gilt
zusätzlich zu den bestehenden Sperrzonen in `forge/gate.py`.

Eine Erlaubnisliste ist hier die richtige Form, weil neue Ordner sonst
automatisch offen wären. Die Öffnung weiterer Pfade ist eine spätere, bewusste
Entscheidung.

## Architektur

### Runner-Abstraktion

`forge/runner.py` bleibt unverändert. Der Claude-Code-Pfad ist gemessen und
funktionsfähig; er wird nicht ersetzt, sondern ergänzt.

Neu:

- `forge/runner_opencode.py` — ruft `opencode run --agent <stufe> --dir <worktree>
  --format json < /dev/null` mit einer je Lauf geschriebenen temporären Config
  über `OPENCODE_CONFIG`. Parst die JSON-Events zu `RunResult`, inklusive
  Token-Zählung für das Budget.
- `forge/runner_agy.py` — ruft `agy -p <prompt> --model <modell> --print-timeout`
  mit `cwd` auf den Worktree. Der Reviewer **braucht** keine Werkzeuge: er
  bekommt den Diff im Prompt und gibt sein Urteil auf stdout zurück.

  **Korrektur vom 2026-09-09, nach der Umsetzung:** Die erste Fassung dieses
  Abschnitts behauptete, der Reviewer laufe „ohne Werkzeuge" und bekomme
  „keinen Dateizugriff". Das ist **nicht erzwingbar** — `agy` hat keine Flagge,
  die Werkzeuge abschaltet (`--sandbox` und
  `--dangerously-skip-permissions` regeln etwas anderes), und sein Rechtemodell
  ist nach wie vor ungemessen. Tatsächlich läuft der Reviewer mit vollem,
  ungemessenem Werkzeugzugriff **innerhalb des Task-Worktrees**, seit
  `cwd=<worktree>` gesetzt ist (davor lief er im Arbeitsverzeichnis des
  Daemons, also im echten Haupt-Checkout — schlimmer).

  Die Eingrenzung ist damit der Worktree, nicht das Rechtemodell. Konkrete
  Folge: Dateien, die der Reviewer schreibt, sammelt der Commit-Pfad der
  nächsten Fix-Stufe mit ein. Eine Aufnahme von `agy`s Rechteverhalten nach
  dem Muster von `permission_probe_opencode.md` ist Vorbedingung dafür, hier
  wieder eine Schranke zu behaupten.

**Daraus folgt eine Änderung an der Review-Stufe.** Heute weist `_review_prompt`
den Agenten an, sein Urteil selbst nach `.forge/review.json` zu schreiben, und
`stages.py` prüft diese Datei als Artefakt. Ein werkzeugloser Reviewer kann das
nicht. Künftig gibt der Reviewer sein Urteil als JSON auf stdout aus, und
`runner_agy.py` schreibt `.forge/review.json` selbst. Die Artefakt-Prüfung in
`stages.py` bleibt damit unverändert gültig — nur der Schreiber wechselt vom
Modell zum Runner.

Nebeneffekt: Das Urteil kann nicht mehr durch einen fehlgeschlagenen Schreibzugriff
verlorengehen, und ein nicht parsebares JSON ist ein klarer Runner-Fehler statt
eines stillen fehlenden Artefakts.

`Stage` erhält die Felder `backend` und `model`. Die Pipeline ruft
`backend.run(...)` statt direkt einer CLI.

`OpencodePermission` ist eine eingefrorene Dataclass, die ausschliesslich die
oben belegte Konfiguration erzeugt. `bash="allow"` oder `task="allow"` werfen im
Konstruktor, mit Verweis auf Probe (d) und (e) — nach demselben Muster wie
`_ERLAUBTE_MODI` in `runner.py`.

### Modellzuordnung und Ausweichketten

| Stufe | Kette |
|---|---|
| spec | Gemini 3.6 Flash (AI Studio) → Kimi K3 → MiniMax M3 |
| plan | Kimi K3 (NVIDIA) → Gemini 3.6 Flash → MiniMax M3 |
| implement | MiniMax M3 → Nemotron 3.5 Lightning → Kimi K3 |
| review | Claude Opus 4.6 (agy) → Gemini 3.1 Pro (agy) → Kimi K3 |
| fix | wie implement |
| Commit-Text | Mistral `codestral-latest` — siehe Einschränkung unten |
| Sitzungstitel | Groq `gpt-oss-120b` als opencodes `small_model` |

Groq taucht wegen der 8000 Tokens/Minute in keiner Arbeitskette auf. Es bleibt
opencodes `small_model` für Sitzungstitel — winzige Prompts, für die das Limit
reicht, und die sonst ein Arbeitsmodell belegen würden.

**Mistral sieht ausschliesslich Tasktitel und Diffstat** — Dateipfade und
Zeilenzahlen, niemals Dateiinhalte. Das reicht für eine brauchbare
Commit-Message und hält Code aus dem Training. Fällt Mistral aus, schreibt Forge
die Message deterministisch aus Tasktitel und Diffstat, ohne Modell.

**Harte Regel:** Der Reviewer darf nie dasselbe Modell sein wie der
Implementierer desselben Tasks. Bleibt in der Review-Kette nur noch dieses
Modell übrig, wird der Task **geparkt statt reviewt**. Ein Modell, das seinen
eigenen Code abnimmt, sieht nur aus wie ein Review.

Nicht verwendet: DeepSeek V4 (Zeitüberschreitung), kimi-k2.6 und
nemotron-ultra-253b (404 für diesen Account), gemma-4-31b (Zeitüberschreitung) —
alle am 2026-09-07 geprüft.

### Scout

Eigener Job neben der Queue, kein Pipeline-Zustand. Läuft, wenn weniger als drei
Tasks warten — nicht nach Uhrzeit, damit er keinen Vorrat produziert, der nie
abgearbeitet wird.

Liest `ROADMAP.md`, `git log` der letzten 14 Tage, `TODO`/`FIXME`-Kommentare und
Testlücken in den freigegebenen Zonen. Modell: Gemini 3.6 Flash (1M Kontext für
die 39-KB-ROADMAP). Ausgabe: JSON mit Titel, Beschreibung, Priorität, Pfaden.

Jeder Vorschlag durchläuft `core.dedup.is_duplicate_title()` gegen die offene
Queue und die letzten 30 erledigten Tasks, danach eine Pfadprüfung gegen
Sperrzonen und erlaubte Zonen. Was durchfällt, wird verworfen, nicht eingereiht.

### Telegram-Bot

Zweiter `TelegramChannel` mit eigenem Token (`FORGE_BOT_TOKEN`), damit
Forge-Meldungen den Mantis-Chat nicht zumüllen. Die Klasse nimmt bereits einen
`token`-Parameter; neu ist nur die Befehlslogik in `forge/bot.py`.

- Freitext → neuer Task, Priorität über Scout-Arbeit
- `/status` → was die zwei Agenten tun, mit Stufe und Laufzeit
- `/queue` → wartende Tasks, je mit Verwerfen-Knopf
- `/stop` → Not-Aus

Merge-Freigabe über `send_with_buttons` und `_handle_callback` (beide vorhanden):
Titel, Gate-Ergebnis, Diffstat, Knöpfe **[Mergen] [Diff] [Verwerfen]**.

Der Bot bekommt **keinen LLM-Zugang**. Freitext wird als Task-Titel übernommen,
nicht interpretiert. Damit ist er keine Angriffsfläche und verbraucht kein
Kontingent.

### Dashboard

`forge/dashboard.py`, FastAPI, Port 7780, eigener launchd-Agent. Liest Postgres
direkt über `core.db`; weil Postgres ein eigener Prozess ist, überlebt das
Dashboard einen Mantis-Absturz.

Es teilt sich das Repo mit Mantis, nur nicht den Prozess. Es läuft aus `main`,
und `main` bewegt sich nur durch Timos Merge — die Agenten arbeiten in
Worktrees. Vollständige Entkopplung bräuchte eine eigene DB-Schicht; das wäre
Verdopplung ohne Gewinn.

Inhalt:

- **Zwei Bahnen**, je Agent: Task, Stufe, laufendes Modell, Laufzeit,
  Token-Zähler, letzte Journal-Zeile
- **Gesprächsfaden pro Task**: Implement → Review-Verdikt im Klartext → Fix →
  erneutes Verdikt, als Thread
- **Warteschlange** mit Verwerfen-Knopf
- **Budget je Anbieter**, Reststand der Nacht
- **Wartet auf Freigabe**: Diffstat, Gate-Ergebnis, Verdikt, dieselben Aktionen
  wie im Bot

Live-Updates über SSE (einseitig, FastAPI-nativ, reconnectet selbst). Aktionen
als normales POST.

Auth: Bindung auf `127.0.0.1` und das Tailscale-Interface, ein Token aus `.env`
(`FORGE_DASHBOARD_TOKEN`), per `?token=` gesetzt und als Cookie gehalten. Das
ist ein Riegel für ein privates Netz, kein Auth-System.

### Budget

`forge/budget.py` — steht bereits in den Sperrzonen, existiert aber nicht.

Führt ein lokales Hauptbuch je Anbieter und Nacht. Wo echte Zahlen kommen,
werden sie übernommen: bei Groq aus `x-ratelimit-remaining-requests` und
`-tokens`. Sonst wird lokal gezählt, mit konservativen Obergrenzen — Antigravity
auf **18** statt der berichteten 20, weil die Zahl unbestätigt ist und ein
überzogenes Limit den Account riskiert.

Erschöpfung gilt **anbieterweit für den Rest der Nacht**, nicht pro Task.
`runner.py`s bestehende Rate-Limit-Erkennung (`_RATE_LIMIT_MARKER`) wird dafür
weiterverwendet. Die Stufe fällt dann auf das nächste Modell ihrer Kette. Erst
wenn jede Kette trocken ist, endet die Nacht.

### Kurzbahn

Tasks, die ausschliesslich `docs/`, Kommentare oder Tests anfassen, überspringen
`spec` und `plan` und gehen direkt in `implement`. Das halbiert den Verbrauch für
genau die Arbeit, die den Grossteil des Scout-Outputs ausmacht.

Die Entscheidung fällt **deterministisch anhand der Pfade** im Task, nicht durch
ein Modell.

### Parallelität

`queue.active()` macht heute `LIMIT 1`. Für zwei Bahnen:
`SELECT … FOR UPDATE SKIP LOCKED` plus eine `worker`-Spalte in `forge_tasks`.
Zwei Daemon-Prozesse greifen sich je einen Task; ein abgestürzter Worker gibt
seinen Task beim Neustart frei.

### Zustandsmodell

Neuer Zustand `AWAITING_APPROVAL` zwischen `GATING` und `MERGED`:

```
GATING → AWAITING_APPROVAL → MERGED | PARKED
```

`AWAITING_RESTART` bleibt für den bestehenden Neustartfenster-Pfad erhalten.
`AWAITING_APPROVAL` gehört zu `ACTIVE_STATES` nicht dazu — ein Task, der auf
Timo wartet, darf keine Bahn blockieren.

### Merge

Beim Mergen prüft Forge, ob `main` sauber ist und ob es sich seit Anlage des
Worktrees bewegt hat. Bei Konflikt geht der Task mit Hinweis an Timo zurück,
statt zu raten. Nach erfolgreichem Merge wird der Worktree entfernt.

### Zeitfenster

launchd startet um 23:00. Ende bei leeren Budgets, um 07:00, oder auf `/stop`.
Beim Stoppen läuft die **aktuelle Stufe zu Ende** — ein halb geschriebener
Worktree kostet mehr als fünf Minuten Wartezeit.

Morgens eine Zusammenfassung per Telegram: was durch ist, woran es hakte, welche
Anbieter leer sind.

## Datenbank-Änderungen

Additiv, bestehende Zeilen bleiben gültig:

- `forge_journal`: Spalten `stage` und `model`. Ohne sie kann das Dashboard nicht
  zeigen, welches Modell an welcher Stufe sitzt — heute steht das nur
  unstrukturiert in `message`.
- `forge_tasks`: Spalte `worker` für die Slot-Vergabe.
- `forge_budget`: neue Tabelle, je Nacht und Anbieter Zähler und Reststand.

## Testbarkeit

Die Rechte-Aufnahme ist eine Momentaufnahme einer CLI-Version, kein Test — sie
kann bei einem opencode-Update ungültig werden. Deshalb:

- Ein Testfall, der `OpencodePermission` mit `bash="allow"` und `task="allow"`
  konstruiert und erwartet, dass es wirft. Er schützt die Schranke gegen
  versehentliche Lockerung im Code.
- Ein Testfall, der die Reviewer-≠-Implementierer-Regel gegen eine erschöpfte
  Kette prüft und erwartet, dass geparkt statt reviewt wird.
- Die Kurzbahn-Pfadentscheidung ist reine Logik und wird ohne DB getestet.
- Budget-Buchführung wird gegen erfundene Header und Zähler getestet, ohne echte
  Anbieter.

Ein erneuter Lauf der Rechte-Aufnahme gehört vor jedes opencode-Update, das in
den Nachtbetrieb geht. Das ist eine Betriebsregel, kein Test.

## Offene Vorbedingungen für Plan 2

Aus der Umsetzung von Plan 1 (Abschluss-Review am 2026-09-09). Alle drei müssen
stehen, **bevor** zum ersten Mal etwas unbeaufsichtigt über Nacht läuft.

1. **Erlaubnisliste ins Gate.** `forge/gate.py` hat heute nur die Verbotsliste
   `SPERRZONEN` mit sechs Präfixen. Die in „Getroffene Entscheidungen"
   beschlossenen erlaubten Pfade (`tests/`, `docs/`, `scripts/`, `tools/`,
   `core/skills/`) existieren nirgends als Prüfung. Solange das so ist, können
   die Stufen mit `edit: allow` Dateien ausserhalb der gedachten Zonen ändern —
   die Begründung, mit der `edit: allow` akzeptiert wurde, trägt erst mit
   dieser Liste.

2. **Fehlgeschlagenes `git status` darf nicht als „nichts zu tun" durchgehen.**
   `_stufenarbeit_pfade` in `forge/pipeline.py` gibt bei nicht-null returncode
   eine leere Liste zurück; `_committe_stufenarbeit` liest das als Erfolg und
   lässt die Stufe mit uncommitteter Arbeit weiterrücken. `_schreibe_diff`
   behandelt denselben Fehlerfall zwei Funktionen weiter richtig, nämlich mit
   Parken.

3. **Gestagter Rename bricht `git add` ab.** Steht nach einem fehlgeschlagenen
   Commit ein Rename im Index, matcht der Quellpfad auf nichts mehr, und ein
   einziger schlechter Pathspec bricht das gesamte `git add` ab (returncode
   128). Der Task hängt dann dauerhaft und braucht ein manuelles `git reset`.
   Behebung: bei Status `R`/`C` den Quellpfad weglassen.

Dazu die Aufnahme von `agy`s Rechteverhalten (siehe oben), falls die
Review-Stufe je wieder als eingegrenzt gelten soll.

## Bewusst nicht enthalten

- **Kein automatischer Merge.** Auch nicht für Docs. Der Gewinn wäre klein, das
  Risiko unbegrenzt.
- **Keine Shell für Agenten.** Bis eine echte Betriebssystem-Sandbox davorsteht.
- **Kein Mistral-Repo-Zugriff.** Der Gratis-Tarif trainiert auf den Daten.
- **Kein lokales Modell.** Ausdrücklich ausgeschlossen.
- **Keine Selbstmodifikation.** `forge/` ist für die Agenten tabu.
