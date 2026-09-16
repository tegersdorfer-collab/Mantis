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
| review | Claude Opus 4.6 (agy) → Gemini 3.1 Pro (agy) |
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

## Offene Vorbedingungen für Plan 2b

Aus der Umsetzung von Plan 2a (Abschluss-Review am 2026-09-10). Beide müssen
stehen, **bevor** zum ersten Mal etwas unbeaufsichtigt über Nacht läuft. Keine
kann vorher eintreten, weil vor Plan 2b nichts unbeaufsichtigt läuft.

1. **Erschöpfte Kontingente schalten die Forge dauerhaft ab.** Seit Plan 2a
   liefert eine erschöpfte Kette `"fehler"` statt zu parken — richtig, denn
   Parken verlor den Task. Aber `forge/daemon.py` zählt `"fehler"` in
   `failures`, und nach `MAX_CONSECUTIVE_FAILURES` schreibt es
   `~/.mantis-forge-stop`. `should_run` verweigert danach jede Arbeit, über
   Neustarts hinweg, bis die Datei von Hand gelöscht wird. Eine Nacht mit
   leeren Kontingenten legt die Forge also für alle folgenden Nächte still.
   Behebung: ein eigener tick()-Ausgang für „Kontingent leer", der schläft,
   ohne die Fehler-Spirale zu füttern. Ändert den pipeline/tick/main-Vertrag,
   deshalb nicht mehr in 2a.

2. **Die Fix-Stufe ist in Produktion unerreichbar.** Die Review-Stufe wechselt
   bedingungslos nach `GATING`, ohne ihr eigenes Verdikt zu lesen, und `GATING`
   hat keinen Rückweg nach `REVIEWING` oder `IMPLEMENTING`. `MAX_FIXRUNDEN`,
   `FIX_STAGE`, `_verwirf_review_artefakte` und die gesamte Fix-Runden-Logik
   regeln damit einen Pfad, der nie läuft: **das erste negative Review parkt
   den Task dauerhaft.** Vorbestehend, älter als Plan 1. Behebung ist eine
   Änderung der Zustandsmaschine in `forge/models.py` und braucht eine
   Entscheidung darüber, wie oft ein Task die Runde drehen darf.

Solange 2 offen ist, ist auch die Regel „Reviewer ≠ Implementierer" nur halb
wirksam: `implement_model` merkt sich nur den letzten Schreiber, was erst nach
einer Fix-Runde zum Problem wird — und die gibt es heute nicht.

## Erledigte Vorbedingungen aus Plan 1

Alle drei wurden in Plan 2a geschlossen (Tasks 1-3), zwei davon mit Tests gegen
echtes git. Hier belassen, weil spätere Pläne darauf verweisen — und weil die
Begründung zu Punkt 1 auch dann noch gilt, wenn niemand mehr weiss warum.

**Nachtrag zur Review-Kette:** Sie hat nur zwei Glieder, nicht drei. Der
ursprünglich vorgesehene dritte Link auf `opencode`/Kimi K3 wurde im
Abschluss-Review von Plan 2a entfernt: nur `forge/runner_agy.py` beherrscht das
Review-Protokoll (Diff in den Prompt einbetten, Verdikt-Datei schreiben).
`runner_opencode.py` hätte einen vollen Lauf verbrannt und dann geparkt — und
weil dieser Agent `edit: allow` hat, hätte er sich im schlimmeren Fall selbst
ein „pass" geschrieben, ohne den Diff je gesehen zu haben. Solange nur ein
Backend das Protokoll kann, ist eine zweigliedrige Kette die ehrliche Länge.

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

## Nachtrag 2c (2026-09-14)

Nach dem Merge von Plan 2b (Commit 81192e3). Die Vorbedingungen aus dem
Abschnitt „Offene Vorbedingungen für Plan 2b" sind geschlossen; das
Abschluss-Review von 2b hat vier Designfragen aufgeworfen, die hier entschieden
sind. Wo dieser Nachtrag der Spec oben widerspricht, gilt der Nachtrag.

### Schnitt: 2c und 2d

**2c** enthält alles, was die erste unbeaufsichtigte Nacht mit **einer** Bahn
braucht: Fix-Schleife, Zustand nach dem Gate, Freigabe-CLI, Nachtfenster,
Test-Isolation, Nachtlauf-Simulator.

**2d** enthält Parallelität (`FOR UPDATE SKIP LOCKED`, Spalte `worker`),
Kurzbahn und Mistral-Commit-Texte. Zwei Bahnen verdoppeln jede
Wechselwirkungsklasse, die das 2b-Review gefunden hat (Absturzfenster,
veraltete Artefakte, Zähler über Ticks). Erst muss eine Bahn eine Nacht
bewiesen haben.

### Fix-Schleife

Ein erfolgreicher Fix-Lauf wechselt **keinen** Zustand. Der Task bleibt in
`REVIEWING`, `_verwirf_review_artefakte` entfernt das alte Urteil und den Diff,
die Stufe liefert `"weiter"`. Der nächste Tick trifft `REVIEWING` ohne Urteil an
und lässt die echte Review-Stufe laufen. Die Implement-Stufe läuft nach einem
Fix **nicht** erneut — das war das Verhalten seit August, kostete je Runde
einen zusätzlichen opencode-Lauf und liess ein zweites Modell die Fix-Arbeit
überschreiben.

Der Übergang `REVIEWING → IMPLEMENTING` wird aus `forge/models.py` entfernt.
Er wird ungenutzt, und ungenutzte Übergänge sind genau das, was die
Fix-Schleife bis 2b unerreichbar gemacht hat.

Eine Fix-Runde ist ein **abgeschlossener** Fix-Lauf. Die Grenzprüfung vor dem
Lauf liest nur (`queue.fixrunden(task_id) -> int`); gezählt wird
(`queue.zaehle_fixrunde`) erst nach `ok=True`. Rate-Limit, Timeout ohne
Produkt, Absturz und verweigerte Werkzeuge verbrauchen keine Runde — ein Task,
dessen Fix zweimal am Kontingent scheiterte, darf nicht mit „Fix-Runden-Grenze
erreicht" parken.

`"kontingent"` bekommt den eigenen Journal-Kind `kontingent` statt
`stage_failed`. Der Morgenbericht zählt Kontingent nicht als Fehler.

### Zustand nach dem Gate

```
GATING → AWAITING_APPROVAL → MERGED | PARKED
```

`AWAITING_APPROVAL` gehört nicht zu `ACTIVE_STATES` — ein Task, der auf Timo
wartet, blockiert keine Bahn. `AWAITING_RESTART` wird nicht mehr angesteuert:
die Agenten dürfen `forge/` nicht anfassen, also braucht kein Merge je einen
Daemon-Neustart. Die Konstante wird samt ihren Übergängen entfernt — am
2026-09-14 steht keine Zeile in diesem Zustand (geprüft: 4 Tasks, alle
`parked`), es gibt nichts zu migrieren.

### Freigabe ohne Telegram

Plan 3 bringt den Bot; `FORGE_BOT_TOKEN` fehlt noch. Damit die Nacht ohne Bot
betreibbar ist, bekommt 2c `forge/cli.py`:

```
python3.14 -m forge.cli status
python3.14 -m forge.cli approve <id>
python3.14 -m forge.cli reject <id> "<grund>"
python3.14 -m forge.cli requeue <id>
python3.14 -m forge.cli stop
```

Plan 3 ruft dieselben Funktionen aus dem Bot auf; das CLI ist die Referenz.

- `approve` merged wie im Abschnitt „Merge": prüft, ob `main` sauber ist und
  ob es sich seit Anlage des Worktrees bewegt hat (dann `git merge` im
  Worktree-Branch, bei Konflikt `PARKED` mit Hinweis statt raten). Erfolg →
  `MERGED`, Worktree und Branch entfernt.
- `reject` → `PARKED` mit Grund.
- `requeue` (aus `PARKED` oder `FAILED`) setzt `refusals` und `attempts` auf 0
  und behält den Worktree — die Arbeit darin ist der Grund, warum der Task
  erneut laufen soll.
- `status` druckt den Morgenbericht: was durch ist (`AWAITING_APPROVAL`), was
  geparkt wurde und warum, welche Anbieter leer sind, Verbrauch je Anbieter.
  Plan 3 schickt genau diesen Text per Telegram.
- `stop` schreibt `~/.mantis-forge-halt` (siehe Nachtfenster).

### Nachtfenster

launchd startet den Daemon per `StartCalendarInterval` um 23:00 statt mit
`RunAtLoad` + `KeepAlive`. `daemon.main` prüft vor jedem Tick
`im_nachtfenster()` (23:00–07:00, beide Grenzen Konstanten); ausserhalb endet
der Prozess mit Code 0, und launchd startet ihn nicht neu. Ein Task, der beim
Fensterende aktiv ist, bleibt aktiv — die nächste Nacht setzt auf derselben
Stufe auf, wie nach einem Absturz.

Weicher Stop: `~/.mantis-forge-halt`. Der Daemon prüft die Datei vor jedem
Tick; ist sie da, endet er nach dem laufenden Tick (die aktuelle Stufe läuft
zu Ende) und löscht die Datei beim Exit. Der Not-Aus `~/.mantis-forge-stop`
bleibt unverändert: sofort, überlebt Neustarts, nur von Hand zu entfernen.

Leere Kontingente beenden die Nacht nicht vorzeitig: `"kontingent"` schläft
900 s, bis 07:00 sind das höchstens 32 Ticks à eine Datenbankabfrage.

### Test-Isolation gegen die Produktion

Zeilen mit `forge_tasks.source = 'test'` sind für den Daemon unsichtbar:
`queue.claim_next()` und `queue.active()` filtern `AND source <> 'test'`.
Tests rufen beide mit `quelle="test"` und sehen ausschliesslich ihre eigenen
Zeilen. Das ersetzt die zehnjährige Pause aus Plan 2b (eine Regel statt zwei)
und hält auch dann, wenn das Gate der Forge diese Tests im Worktree eines Tasks
ausführt — was es tut, weil es `pytest -q` über die ganze Suite laufen lässt.
Jede Test-Fixture räumt am Anfang Leichen früherer, abgebrochener Läufe weg
(`DELETE … WHERE source='test'`).

### Nachtlauf-Simulator

`tests/test_forge_nachtlauf.py` treibt das echte `daemon.main()` gegen die
echte Datenbank. Gemockt sind nur die Ränder: `worktree.create` (→
`tmp_path`), `gate.pruefe`, `time.sleep` (zählt Ticks, wartet nicht), die
Fenster-Uhr, und die LLM-Backends durch ein **Drehbuch**: eine Liste von
Antworten je Aufruf — `ok`, `rate_limited`, `ok=False`, „Verdikt schreiben",
oder **Absturz** (eine `BaseException` aus einem Hook nach einem realen
`set_state`, die den Tick an genau dieser Zeile beendet). Nach N Ticks hält
eine `BaseException` das `main()` an.

Danach werden Invarianten geprüft, nicht Einzelzustände:

- keine Not-Aus-Datei;
- `refusals ≤ MAX_FIXRUNDEN` für jeden Task;
- kein Task in `REVIEWING` mit `review.json`, ohne dass im Journal ein
  Review-Lauf nach dem letzten Implement/Fix steht;
- jeder Task in genau einem Zustand, und jeder aktive Task hat einen
  Journal-Eintrag aus dem letzten Tick, der ihn berührt hat.

Szenarien: gute Nacht (zwei Tasks → `AWAITING_APPROVAL`); zwei negative
Reviews (→ `PARKED`, `refusals = 2`, kein dritter Fix-Lauf); Rate-Limit-Kaskade
über beide Anbieter der Implement-Kette (→ `"kontingent"`, Zustand steht, keine
Not-Aus-Datei, Fortsetzung nach Budget-Reset); Absturz nach
`_verwirf_review_artefakte`, vor der Rundenzählung (→ nächster Tick reviewt,
fixt nicht; ein Absturz *vor* `_verwirf` fixt einmal erneut, ungezählt und
begrenzt);
Fensterende mitten in einer Stufe (→ Exit 0, Task aktiv, nächste Nacht setzt
auf). Mutationsnachweis: die vier Importants aus dem 2b-Abschlussreview
(Rate-Limit-Spirale, Implement-Re-Run, veraltetes Verdikt, verbrauchte Runde)
müssen einzeln zurückgedreht den Simulator rot machen.

### Review-Regel für 2c

Drei Pläne in Folge haben Task-Reviews grün gesehen, was das Abschluss-Review
als Critical fand. Das Modell war nie der Engpass (2b lief auf Opus); der
Zuschnitt war es: ein Diff-Review stellt Systemfragen nicht von selbst. Jedes
Task-Review-Briefing in 2c enthält deshalb fünf feste Fragen, jede mit einer
Codestelle zu beantworten:

1. Wer führt diesen Code noch aus, ausser dem Daemon? (Gate, CLI, Tests)
2. Was tut der nächste Tick mit dem Zustand, den diese Änderung hinterlässt?
3. Was passiert, wenn der Prozess zwischen zwei Zeilen dieser Änderung stirbt?
4. Was überlebt einen Neustart — Dateien, Worktrees, Zeilen, Zähler?
5. Was schreibt diese Änderung in Produktionstabellen, und wer räumt es weg?

Das Abschluss-Review auf dem stärksten Modell bleibt.

## Nachtrag 3a (2026-09-16)

Nach zwei Tasks, die die Forge komplett bis `main` gebracht hat (#365, #621,
beide mit Nacharbeit von Hand vor dem `approve`). Plan 3 wird geteilt und
vor 2d gezogen. Wo dieser Nachtrag dem Abschnitt „Telegram-Bot" oben
widerspricht, gilt der Nachtrag.

### Schnitt: 3a vor 2d, 3b später

**3a** ist der Bot ohne Merge-Knöpfe: Morgenbericht und Not-Aus per Telegram,
Freitext → Task, `/status`, `/queue`, `/requeue`, `/stop`. Er kommt vor 2d,
weil die Queue am 16.09. leer war und das Einreihen per Python-Snippet im
Terminal (zsh-Backtick-Falle) die tatsächliche Bremse ist — nicht die
fehlende zweite Bahn. 2d lohnt erst, wenn genug Tasks drin sind.

**3b** bringt die Merge-Freigabe per Knopf (**[Mergen] [Diff] [Verwerfen]**)
und die Scout-Priorisierung. Beide bisherigen Tasks brauchten Nacharbeit, die
weder Review noch Gate sahen; ein Merge-Knopf auf dem Handy lädt zum blinden
Approve ein. 3b kommt, wenn zwei, drei Tasks ohne Nacharbeit durch sind. Bis
dahin bleibt die Freigabe beim CLI mit Diff-Lesen im Worktree.

### Eigener Bot statt `TelegramChannel`

Der Abschnitt oben nimmt an, `TelegramChannel` liefere die Basis („neu ist
nur die Befehlslogik"). Das stimmt nicht: die Klasse hängt Voice, Fotos und
URL-Inhalte direkt an den LLM, `_handle_callback` an die Mantis-Domains, und
`/status` ist fest verdrahtet. „Kein LLM-Zugang" hieße, fünf Handler
abzuschalten. `forge/bot.py` steht deshalb direkt auf `python-telegram-bot`
(vorhanden, v22.7) und übernimmt von `TelegramChannel` nur das Muster der
Allowlist. Kein Import aus `communication/`, keiner aus den Runnern.

### Prozess und Zustellung

Zwei Wege, weil der Bot tagsüber leben muss und der Daemon nachts nicht auf
einen zweiten Prozess angewiesen sein darf:

- **`forge/bot.py`** — Polling-Bot, eigener launchd-Agent
  `com.mantis.forge-bot` mit `KeepAlive`, 24/7. Lädt
  `~/.config/ai-keys.env` über `daemon.lade_api_schluessel()`, Token aus
  `FORGE_BOT_TOKEN`. Fehlt der Token, beendet sich der Prozess mit klarer
  Meldung (launchd zieht ihn nicht in einer Schleife hoch: `ThrottleInterval`
  gesetzt).
- **`forge/melden.py`** — `sende(text) -> bool`, synchroner `urllib`-POST auf
  `sendMessage`, keine Bot-Instanz, kein Polling. Der **Daemon** ruft sie an
  jedem Ende von `main()`: Fensterende und weicher Halt mit
  `bericht.morgenbericht()`, Fehler-Spirale sofort mit dem Grund als erster
  Zeile und dem Bericht darunter. Fehlt der Token oder antwortet Telegram
  nicht, loggt `sende` (Status, nie die URL) und gibt `False` zurück — die
  Nacht darf nie an Telegram scheitern. Nachrichten über 4096 Zeichen werden
  am letzten Zeilenumbruch geteilt.

Nachts sonst Stille. Parks stehen im Morgenbericht, nicht um drei Uhr auf dem
Handy.

### Befehle

Alles läuft über die Funktionen, die `forge.cli` schon aufruft; das CLI bleibt
die Referenz (Abschnitt „Freigabe ohne Telegram").

| Eingabe | Wirkung | Antwort |
|---|---|---|
| Freitext | `queue.enqueue(title=erste Zeile, description=Rest, source="timo")` | „#42 eingereiht: <Titel>" mit Knopf **[Verwerfen]** |
| `/status` | Kopfzeile + `bericht.morgenbericht()` | „Daemon läuft, #42 in implementing seit 23:14" bzw. „Daemon läuft nicht", dann der Bericht |
| `/queue` | `queue.nach_zustand(QUEUED)` | je Task eine Zeile mit Knopf **[Verwerfen]** |
| `/requeue <id>` | `freigabe.neu_einreihen(id)` | „#42 neu eingereiht" oder „#42: nicht geparkt/gescheitert" |
| `/stop` | `freigabe.stoppen()` | Text der Funktion |
| alles andere | nichts | „Kenn ich nicht. Freitext = Task, /status /queue /requeue /stop" |

- **Verwerfen** ist `queue.park(id, "verworfen via Telegram")`, nur aus
  `QUEUED`. Kein Löschen; geparkt ist nachvollziehbar und per `/requeue`
  rückholbar. Ist der Task inzwischen aktiv, sagt der Knopf das und tut nichts.
- **`/stop`**: die Logik aus `cli._stop()` (Halt-Datei nur bei laufendem
  Daemon, sonst Hinweis auf Not-Aus) wandert nach `freigabe.stoppen() -> str`;
  CLI und Bot drucken bzw. senden den String. `_daemon_laeuft()` zieht mit
  und matcht auf `-m forge\.daemon` (Handoff 16.09.: `pgrep -f forge.daemon`
  ist zu breit).
- Der Freitext wird nicht interpretiert. Erste Zeile ist der Titel, gekürzt
  auf 200 Zeichen; der Rest ist die Beschreibung. Priorität bleibt beim
  Standard — „über Scout-Arbeit" ist Sache von 3b, den Scout gibt es noch
  nicht.
- Voice, Fotos, Dokumente: keine Handler. Telegram liefert sie, der Bot
  ignoriert sie ohne Antwort — sonst müsste er erklären, was er nicht kann.

### Sicherheit

- Allowlist strikt aus `TELEGRAM_CHAT_ID` und `TELEGRAM_ALLOWED_IDS` (die
  Chat-ID eines Privatchats ist die User-ID, sie gilt für beide Bots).
  Fehlen beide, startet der Bot nicht. Kein Trust-on-first-use: der Bot reiht
  Aufgaben ein, die Agenten im Repo ausführen.
- Fremde Absender werden geloggt (ID) und nicht beantwortet.
- Callback-Daten haben die Form `verwerfen:<int>`; alles andere wird
  verworfen. Es gibt keinen Weg von Telegram in einen Shell-Aufruf.
- Der Token steht nur in `~/.config/ai-keys.env`. `melden.sende` und der Bot
  loggen nie den Token und nie eine URL, die ihn enthält.

### Testbarkeit

- Die Befehlslogik ist eine reine Funktion `antwort_auf(text, absender_ok)
  -> Antwort` (Text plus Knopfliste) und ein `knopf_gedrueckt(daten) ->
  Antwort`; die Telegram-Handler sind Dreizeiler darum. Tests treffen die
  Funktionen mit gepatchter `queue`/`freigabe`. **Kein Telegram aus Tests**,
  dieselbe Regel wie für `opencode`/`agy`.
- `melden.sende`: `urlopen` gepatcht; ohne Token kein Aufruf und `False`;
  HTTP-Fehler → `False`, kein Traceback; Teilung bei 4096.
- Daemon: jedes Ende von `main()` ruft `melden.sende` genau einmal; die
  Fehler-Spirale schickt den Grund in der ersten Zeile.
- **Tagesprobe vor dem Merge** (Prozess-Lehre aus vier Plänen: Umgebung und
  Anbieter kommen in keinem Diff vor): Bot per launchd hochziehen, eine
  Nachricht schicken und den Task in der Queue sehen, `/status` beantworten
  lassen, `/stop` ohne Daemon, dann `melden.sende("Probe")` aus einem
  `python3.14 -m`-Aufruf im Repo mit denselben Umgebungsvariablen wie die
  plist. Der Probe-Task wird danach geparkt, nicht gelöscht.

### Review-Regel für 3a

Die fünf Fragen aus „Review-Regel für 2c" gelten, plus die sechste aus dem
Handoff vom 16.09.: *Unter welcher Umgebung läuft das wirklich, wurde die
geprobt?* Für den Bot heißt das: launchd-PATH, Schlüsseldatei, Arbeits-
verzeichnis, `ThrottleInterval` — mit Codestelle oder Probe belegt.
