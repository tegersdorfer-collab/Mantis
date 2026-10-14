# Spec: Agenten-Fernbedienung über Discord-DM

Stand: 2026-10-14 · Nutzungs-Challenge, Use-Case 2 · Schritt 1 von 2: Analyse und Spec

## Ziel

Timo kann Codex vom Handy aus über eine Discord-DM starten, den Laufstatus prüfen,
Ergebnisse lesen und einen Remote-Lauf abbrechen. Das funktioniert, während Mantis auf dem
Mac als LaunchAgent läuft, und bleibt nach einem Neustart von Mantis nachvollziehbar.

Der Dispatcher verursacht keine zusätzlichen Modell- oder API-Aufrufe. Die Coding-Aufgabe
läuft ausschließlich über `~/.local/bin/codex-task` und damit über die ChatGPT-Anmeldung der
gebündelten Codex-CLI.

## Harte Sicherheitsanforderungen

Diese Regeln gelten für jeden Einstiegspfad und dürfen nicht durch natürliche Sprache,
Ergebnistext, Konfigurationsfehler oder einen Mantis-Neustart umgangen werden.

1. **Discord-Identität und Kanal:** Nur User aus `DISCORD_ALLOWED_USER_IDS` dürfen Agent-Remote
   verwenden. Der Befehl muss aus einer DM kommen (`message.guild is None`). Leere Allowlist,
   unbekannter User und Serverkanal bedeuten: keine Aktion. Es gibt keine zweite, abweichende
   Agent-Allowlist.
2. **Bestätigung vor jedem Start:** Vor einem Start zeigt Mantis Repo, kanonischen Repo-Pfad,
   Ziel-Worktree, Modus (`lesen`/`schreiben`), effektives Modell und den vollständigen
   Auftragstext. Erst eine separate Nachricht `ja` oder `start` startet genau diesen Vorschlag.
   Die Bestätigung verfällt nach 10 Minuten, ist einmalig und an User, DM und unveränderten
   Vorschlag gebunden. Eine neue Aufgabe, ein `nein` oder eine andere Nachricht verwirft den
   offenen Vorschlag. Nach einem Mantis-Neustart gibt es keine ausstehenden Bestätigungen.
3. **Repo-Allowlist und Pfade:** `AGENT_REMOTE_REPOS` ist eine vertrauenswürdige Settings-
   Allowlist. Der Default enthält ausschließlich `mantis -> ~/Mantis`. Werte werden expandiert,
   mit `resolve(strict=True)` normalisiert und als Git-Root validiert. Nachrichtentext darf
   niemals als Pfad, `cwd`, Worktree-Ziel oder Git-Argument verwendet werden. Ein Repo wird nur
   über einen eindeutigen Namen/alias aus der Allowlist erkannt; fehlender, mehrfacher oder
   unbekannter Name führt zur Rückfrage. Pfadartige Textstücke zählen nicht als Repo-Alias.
4. **Schreib-Isolation:** Jeder schreibende Lauf bekommt einen neuen Worktree unter
   `<repo>/.worktrees/remote-<datum>-<slug>-<kurz-id>` und einen neuen Branch
   `remote/<datum>-<slug>-<kurz-id>`, abgezweigt von lokalem `main`. Der Worktree-Pfad muss
   nach Auflösung aller Elternverzeichnisse innerhalb des normalisierten Allowlist-Repos liegen;
   ein symlinkbasiertes `.worktrees`-Verzeichnis außerhalb des Repos wird abgelehnt. Existierende
   Branches oder Verzeichnisse werden nicht wiederverwendet. Mantis arbeitet für Schreibaufgaben
   nie im Haupt-Checkout.
   Mantis führt kein `commit`, `merge`, `push` oder automatisches Löschen von Worktrees/Branches
   aus. Der Auftrag an Codex enthält zusätzlich die Anweisung, keine Commits, Merges oder Pushes
   auszuführen.
5. **Lesen:** Fragen, Analysen und Reviews laufen mit `codex-task -r` direkt im Allowlist-Repo.
   Es wird kein Worktree angelegt. Schreibabsicht wird konservativ bestimmt; bei unklarem Modus
   fragt Mantis nach `lesen` oder `schreiben`, statt zu raten. Der endgültige Modus steht immer
   in der Bestätigung.
6. **Prozesse und Zeit:** Höchstens zwei gleichzeitig laufende, über Agent Remote gestartete
   Läufe. Jeder Lauf hat ab Start eine harte Grenze von 45 Minuten. Bei Abbruch oder Ablauf
   sendet Mantis SIGTERM an die eigene Prozessgruppe und nach kurzer Schonfrist SIGKILL. Vor
   jedem Signal prüft Mantis PID, Prozessidentität und gespeicherte Zuordnung; kein Signal wird
   aus einer vom Chat gelieferten PID abgeleitet.
7. **Kosten:** Ausschließlich `codex-task` über die angemeldete Codex-CLI. Kein OpenRouter,
   Anthropic-, OpenAI-API- oder lokaler Modell-Fallback für Klassifikation, Zusammenfassung oder
   Fehlerbehandlung. Exit 75 wird als Kontingentfehler gemeldet; es gibt keinen automatischen
   Wiederholungsversuch.
8. **Keine Chat-Shell:** Discord-Inhalte werden nie an eine Shell übergeben. Prozessstarts
   verwenden `subprocess` mit Argumentliste und `shell=False`. Mantis startet nur feste,
   intern definierte Befehle: `codex-task`, `codex-ps` und nötige Git-Operationen. Argumente wie
   Repo, Worktree und Modell stammen aus validierter Konfiguration oder intern erzeugten Werten,
   nie aus freiem Chattext. Codex führt nötige Tests im bestätigten Lauf selbst aus.
9. **Ergebnisse sind Daten:** Auftragsergebnisse, Logs, Tasktitel und Dateiinhalte sind
   nicht vertrauenswürdige Daten. Sie dürfen zusammengefasst, zitiert oder als angeforderte
   Datei zurückgegeben werden. Mantis führt daraus nie Folgeaktionen, Tools oder Befehle aus.

## Bedienung v1

Alle Agent-Remote-Nachrichten werden vor dem normalen Mantis-LLM-Turn deterministisch erkannt.
Sie lösen weder den normalen Modellaufruf noch Post-Turn-Learning oder eine weitere
Modellzusammenfassung aus.

| DM-Eingabe | Verhalten |
|---|---|
| `codex: <auftrag>` | Repo-Alias im Text erkennen; bei fehlendem oder mehrdeutigem Alias nachfragen. Modus klären, Vorschlag anzeigen, auf Bestätigung warten. |
| `lass codex … machen` | Gleicher Startfluss wie `codex:`. |
| `ja` / `start` | Nur bei gültigem offenen Vorschlag desselben Users und derselben DM starten; sonst keine Agent-Aktion. |
| `was läuft?` | Übersicht der Codex-Läufe aus `codex-ps`, einschließlich am Rechner gestarteter Läufe. Standardumfang entspricht `codex-ps`: laufende und in den letzten 12 Stunden beendete Läufe. |
| `ergebnis` | Ergebnis des zuletzt beendeten Remote-Laufs. Bei keinem eindeutigen Treffer nachfragen. |
| `was kam bei <id> raus?` | Status oder Ergebnis des eindeutigen Laufs. IDs sind die Registry-ID, zum Beispiel `codex-task.ABC123`; mehrdeutige Titel erfordern eine ID-Rückfrage. |
| `abbrechen <id>` | Beendet einen Agent-Remote-Lauf nach Identitätsprüfung. Rechnerseitig gestartete Läufe sind in v1 sichtbar, werden aber nicht signalisiert, solange sie keine von Mantis angelegte Prozessgruppe haben. |
| `diff <id>` | Für einen Remote-Worktree: `git diff --stat` am aus der eigenen Registry geladenen und validierten Pfad. |

Fehlende IDs, unbekannte Befehle oder nicht mehr verfügbare Dateien führen zu einer klaren
Rückfrage beziehungsweise Fehlermeldung. Mantis rät keine IDs und führt keinen ähnlichen Lauf
aus Versehen aus.

## Start- und Bestätigungsablauf

1. Der Discord-Handler lässt nur erlaubte DMs in den Agent-Remote-Dispatcher.
2. Der Dispatcher parst den festen Befehlspräfix, den eindeutigen Repo-Alias und den Auftrag.
   Er nutzt dafür keinen LLM-Aufruf. Eingebettete Pfade und Shell-Syntax haben keine
   Steuerwirkung.
3. Der Dispatcher löst Settings-Alias und Pfad auf, prüft den Git-Root, bestimmt den Modus und
   berechnet Modell, Branch und Zielpfad. Noch wird kein Worktree und kein Prozess angelegt.
4. Mantis sendet den vollständigen Vorschlag als DM. Bei langen Aufträgen wird der Auftragstext
   vollständig und eindeutig abgegrenzt angezeigt. Der Vorschlag enthält eine interne
   Zufalls-ID und läuft nach 10 Minuten ab. Die Vorschau kennzeichnet, dass `codex-task` danach
   seinen festen Worker-Präfix und optionale `jev-route`-Gedächtnisverweise ergänzt.
5. Nur die isolierte Nachricht `ja` oder `start` bestätigt. Mantis verbraucht den Vorschlag
   atomar vor dem Start; doppelte Discord-Events können dadurch keinen zweiten Lauf erzeugen.
6. Für Schreiben legt Mantis den Worktree/Branch mit gehärteten Git-Argumenten ab `main` an:
   `worktree add --no-checkout`, danach ein Checkout ohne Hooks und externe Filter. Vor jedem
   Git-Aufruf im Worktree prüft Mantis, dass dessen `.git`-Datei auf
   `<repo>/.git/worktrees/<name>` zeigt. Danach startet ein kleiner Launcher, der PID,
   Prozessgruppe und Prozessstart-ID atomar im bereits gespeicherten `starting`-Datensatz ablegt,
   bevor er sich durch `codex-task` ersetzt. Ein Fehler in einem Schritt meldet den Fehler und
   startet nichts weiter. Angelegte Worktrees bleiben zur manuellen Prüfung erhalten.
7. Mantis meldet den Start an denselben erlaubten User. Bei Ende darf es ebenfalls eine DM
   schicken, weil der Lauf explizit von diesem User bestätigt wurde.

Der Modellwert in der Bestätigung ist das tatsächliche `CODEX_TASK_MODEL`, andernfalls der
Skript-Default `gpt-6-luna`. `CODEX_TASK_EFFORT` fällt derzeit auf `max` zurück und soll in der
Vorschau ebenfalls ausgewiesen werden. Leseläufe übergeben `-r`; Schreibläufe verwenden den
Skript-Default `workspace-write`.

## Architekturentscheidung

| Ansatz | Folge |
|---|---|
| Agent-Remote-Steuerung innerhalb von Mantis; je Auftrag ein abgekoppelter `codex-task`-Prozess | Nutzt vorhandene Kosten-, Status- und Ergebnisdateien. Ein kleiner persistenter Mantis-Datensatz ergänzt Remote-ID, Repo/Worktree und Deadline. |
| Eigener LaunchAgent pro Lauf | Prozesslebenszyklus wäre unabhängig; benötigt zusätzliche Plists, Start-/Stop-Verwaltung und Bereinigung. |
| Lauf als Task im asyncio-Handler von Mantis | Wenig zusätzlicher Prozesscode; Mantis-Neustart unterbricht oder verliert aber die Zuordnung. |

**Empfehlung:** erster Ansatz. Ein eigener Dispatcher/Service (vorgeschlagener Ort
`core/agent_remote.py`) wird vor dem regulären Modellpfad in den Nachrichteneingang integriert.
`communication/discord.py` bleibt für Discord-Identität, DM-Erkennung und Antwortweg zuständig.
Settings enthalten Feature-Schalter und Repo-Aliase. Der Dispatcher nutzt den vorhandenen
`_persist_msg`-Pfad für Messbarkeit. Agent-Remote-Antworten werden in `chat_messages` mit
`role="assistant"` und `meta={"use_case":"agent","untrusted":true}` protokolliert, aber
nicht als KZG-Turn gespeichert. Die KZG-Wiederherstellung überspringt Einträge mit
`untrusted=true`.

Der Feature-Schalter `AGENT_REMOTE_ENABLED` sollte standardmäßig `False` sein (fail closed).
Die vertrauenswürdige Repo-Konfiguration kann als Mapping aus Alias und Pfad geführt werden,
mit Default `{"mantis": "~/Mantis"}`. Eine Testbefehl-Konfiguration gibt es in v1 nicht.

## `codex-task`-Schnittstelle und Laufdateien

Beide Skripte wurden vollständig gelesen. Der aktuelle Vertrag ist:

| Datei/Quelle | Inhalt und Bedeutung |
|---|---|
| `~/.local/state/codex-task/runs/<id>/meta` | Zeilenformat `key=value`: `pid`, Unix-`start`, `dir`, optionale `session`, `run`, `title`. `<id>` ist der zufällige Temp-Verzeichnisname, z. B. `codex-task.ABC123`. |
| `.../status` | `running`, `ok`, `fail` oder `quota`. Wenn `running` bleibt, aber der PID weg ist, zeigt `codex-ps` den Zustand `tot` (Prozess ohne Abschluss beendet). |
| `.../end` | Unix-Zeitstempel bei normalem Script-Abschluss. |
| `.../seen` | Marker, den `codex-ps` als „neu“ darstellt; Mantis soll ihn nicht verändern. |
| `<meta.run>/prompt.txt` | Effektiver Prompt mit Worker-Präfix und gegebenenfalls von `jev-route` angehängten Gedächtnisverweisen. |
| `<meta.run>/log.txt` | Codex-CLI-Log; kann Toolausgaben und nicht vertrauenswürdige Inhalte enthalten. |
| `<meta.run>/last.txt` | Vollständige letzte Antwort bei Erfolg. Während eines Laufs und bei manchen Fehlern fehlt die Datei. |
| `<meta.run>/diff.txt` | Vom Script nur bei ausführbarem `tracelog` erzeugter Diff-Record. |

`codex-task` gibt Exit 0 bei Erfolg, 75 bei erkanntem Kontingent-/Rate-Limit und 1 bei Fehler.
Es wählt standardmäßig die gebündelte CLI unter
`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` und fällt intern auf ältere
Pfade zurück. Das Script schreibt Prompts, Logs und Ergebnisse in sein Temp-Run-Verzeichnis,
Status und Metadaten nach `~/.local/state/codex-task/runs`. Wenn `~/.local/bin/tracelog`
ausführbar ist, archiviert das Script zusätzlich Prompt, Log, Output und Diff als Tracelog-Blobs.

`codex-ps` zeigt registrierte Läufe aus genau diesem State-Verzeichnis. Ohne `-a` sind alle
laufenden und die in den letzten 12 Stunden beendeten Läufe sichtbar. Es findet zusätzlich
ältere, noch nicht registrierte `codex-task`-Prozesse via `ps`; für diese Alt-Prozesse gibt es
aber keine verlässliche Registry-ID oder garantiert abrufbare `last.txt`-Datei. Neue Läufe
sollen daher immer auf dem Registry-Pfad beruhen. IDs werden in `was läuft?` ergänzt, weil die
Konsolenausgabe von `codex-ps` selbst keine Registry-ID je Tabellenzeile ausgibt.

Mantis führt einen eigenen, restriktiv berechtigten Datensatz pro Remote-Lauf unter
`~/.local/state/mantis/agent-remote/runs/<remote-id>/`:

- Status, Requester-ID, Repo-Alias und normalisierter Repo-Pfad.
- Modus, Branch/Worktree, effektives Modell, Erstellzeit, Deadline und Prozess-PID.
- Verknüpfte `codex-task`-Registry-ID sowie `notification_sent_at`.
- Kein zweites Volltext-Log und keine Zugangsdaten. Die tatsächliche Antwort bleibt in
  `last.txt`; Dateirechte des neuen State-Baums sind auf den Benutzer zu beschränken.

Für einen beständigen Ergebnisordner setzt Mantis `TMPDIR` pro Lauf auf einen vorbereiteten
Ordner innerhalb des Remote-State-Verzeichnisses. `codex-task` legt darin sein
`codex-task.XXXXXX`-Verzeichnis an; `meta.run` verweist damit weiterhin auf die echte
Ergebnisablage. So sind State und `last.txt` nach einem Mantis-Neustart wieder auffindbar.

## Prozessstart, launchd und Neustarts

### Befund auf diesem Mac

- `com.mantis.assistant.plist` startet `/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python`
  mit `main.py`, `WorkingDirectory=~/Mantis`, `RunAtLoad=true` und `KeepAlive={Crashed: true}`.
- Der LaunchAgent setzt `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`; `~/.local/bin`
  ist nicht darin enthalten. `HOME` steht nicht explizit in der Plist. `launchctl getenv`
  lieferte für `HOME` und `PATH` in der aktuellen Umgebung keine Werte. Ein direkter Abruf der
  Mantis-Prozessumgebung war in dieser Sandbox durch die Prozessinspektion gesperrt.
- `codex-task` kann trotzdem über seinen absoluten Pfad gestartet werden. Es sucht die
  gebündelte Codex-CLI ebenfalls über einen absoluten Pfad. `jev-route` wird dagegen nur über
  `PATH` gesucht und fällt bei Nichterreichbarkeit still aus; `tracelog` wird über den absoluten
  Home-Pfad gesucht.
- `codex login status` im aktuellen Benutzerkontext meldete `Logged in using ChatGPT` (mit
  einem nicht fatalen Hinweis, dass CLI-PATH-Aliase nicht angelegt werden konnten). Das beweist
  die CLI-Anmeldung für diesen Benutzer, aber keinen erfolgreichen bezahlten `codex-task`-Lauf
  aus Mantis/launchd; ein solcher Lauf wurde in Schritt 1 nicht gestartet.

### Anforderungen an Mantis

Der Kindprozess bekommt eine explizite, minimale Umgebung mit `PATH`, `HOME` und `LANG`, dazu
`TMPDIR` im dauerhaften Remote-State und feste Git-Sicherheitswerte. Mantis übernimmt keine
anderen Umgebungsvariablen und damit keine Mantis-Secrets. `cwd` stammt aus der Allowlist;
`codex-task` und `codex-ps` werden über absolute Pfade gestartet. Prompts und Logs werden nicht
über Pipes geführt, die nach einem Neustart verschwinden oder den Kindprozess blockieren könnten.

Der Prozess wird als eigenständige Session/Prozessgruppe gestartet (`start_new_session=True`,
`shell=False`, stdin auf `/dev/null`, Dateideskriptoren geschlossen, restriktive Umask). Mantis
schreibt vor dem Start einen Datensatz mit Status `starting`; der Launcher speichert PID,
Prozessgruppe und Prozessstart-ID atomar, bevor `codex-task` ausgeführt wird. Die `TMPDIR`-
Zuordnung und echte Registry-Dateien erlauben die Wiederherstellung nach einem Mantis-Neustart.

Beim Mantis-Start werden Remote-State und `codex-task`-Registry abgeglichen:

- Lebender, passender PID plus `status=running`: weiter überwachen und gespeicherte Deadline
  anwenden.
- Beendeter Prozess plus `status=ok`: Ergebnis laden und ausstehende Abschluss-DM senden.
- `status=quota` oder `fail`: als Kontingent-/Fehlerstatus anzeigen, nicht wiederholen.
- PID weg bei weiterhin `running`: als `unterbrochen` markieren; kein automatischer Neustart.
- `cancelled` und `timed_out` sind Mantis-Zustände; die bestehende `codex-task`-Registry kennt
  dafür keine eigenen Statuswerte.

Wenn Mantis vor dem Versand einer erlaubten Abschluss-DM neu startet, wird die Benachrichtigung
aus dem persistenten Marker nachgeholt. Der Marker wird nach erfolgreichem Discord-Versand
gesetzt; bei einem Absturz genau zwischen Versand und Marker kann Discord dieselbe kurze
Benachrichtigung einmal doppelt erhalten.

Die LaunchAgent-Anmeldung läuft im Benutzerkontext. Die Codex-CLI muss für denselben Benutzer
und dasselbe `HOME` angemeldet sein; ein macOS-Benutzer-Login bzw. verfügbarer Login-Keychain
bleibt Voraussetzung. Bei abgemeldetem Benutzer oder gesperrtem Auth-Speicher kann Mantis die
Anmeldung nicht ersetzen. Diese Betriebsbedingung wird im Implementierungsschritt praktisch
geprüft, ohne Credentials zu protokollieren.

## Ergebnis, Diff und Prompt-Injection

Nach Ende darf Mantis dem ursprünglichen Requester eine DM mit einer festen, modellfreien
Vorlage schicken:

- Lauf-ID und Status.
- Geänderte Dateien aus dem Remote-Worktree.
- Die Testzeile aus der Codex-Antwort. Der Startprompt verlangt, dass Codex passende Tests im
  Lauf ausführt und Befehl sowie Ergebnis nennt.
- Offene Punkte aus der Codex-Antwort, sofern klar vorhanden; andernfalls Verweis auf `ergebnis`.

Discord-Mentions aus Taskausgaben lösen keine Pings aus. Lange Antworten werden mit der
vorhandenen Discord-Nachrichtenteilung gesendet. Auf Nachfrage darf ausschließlich `last.txt`
als Datei angehängt werden; `log.txt` und `prompt.txt` werden nicht automatisch verschickt.

`was läuft?`, Status, Tasktitel, `last.txt`, `log.txt`, Git-Ausgabe und Codex-Testzeile werden nur
angezeigt oder in diese feste Vorlage eingesetzt. Sie werden niemals als neue Instruktionen
interpretiert. Die Zusammenfassung selbst erzeugt keine Aktionen und ruft kein Modell auf.

## Messbarkeit

Jede gespeicherte Nutzer- und Mantis-Nachricht des Agent-Remote-Flows erhält
`chat_messages.meta.use_case = "agent"`, einschließlich Startanfrage, Bestätigung,
Statusabfrage, Ergebnis und Abschlussmeldung. Der vorhandene `channel="discord"` bleibt
unverändert. Für Laufmetadaten werden zusätzlich Zustand und Zeitpunkte lokal geführt; Prompt-
und Ergebnisvolltexte werden nicht in den neuen State-Datensatz kopiert.

## Tests

Für die Implementierung sind mindestens folgende isolierte Tests erforderlich; in Schritt 1
wurden keine Tests ausgeführt und keine Produktdateien geändert.

- DM-only und bestehende Discord-Allowlist: unbekannte User, Bot-User, leere Allowlist und
  Servernachrichten können keinen Agent-Remote-Lauf auslösen.
- Feature aus, fehlende oder ungültige Repo-Allowlist, Pfadnormalisierung, Symlinks,
  Alias-Kollisionen und chatgelieferte Pfade.
- Vorschau zeigt Repo, Modus, Worktree, Modell und vollständigen Auftrag; kein Prozess vor
  `ja`/`start`; Ablauf nach 10 Minuten, einmalige Bestätigung, User-/Vorschlagsbindung,
  Verwerfen bei Änderung und doppelte Discord-Events.
- Prozessargumente werden als Liste ohne Shell gestartet; korrekter `-C`-Pfad, `-r` im
  Lesemodus, Workspace-Write im Schreibmodus und Modellübergabe.
- Schreiblauf erzeugt ausschließlich neuen Worktree/Branch ab `main`; Haupt-Checkout bleibt
  unberührt; existierende Ziele werden abgelehnt; Mantis führt kein Commit/Merge/Push aus.
- Maximal zwei bestätigte Remote-Läufe, Konkurrenz zwischen gleichzeitigen Startnachrichten,
  Deadline, SIGTERM/SIGKILL und PID-Wiederverwendung ohne Signal an fremde Prozesse.
- Wiederherstellung nach Mantis-Neustart für laufende, erfolgreiche, fehlgeschlagene,
  quota-beendete, abgebrochene und unterbrochene Läufe; persistentes `last.txt`; keine
  automatische Wiederholung.
- `codex-ps`-ähnliche Liste enthält registrierte Rechnerläufe und Remote-Läufe; Alt-Prozesse
  ohne Registry sind als nur beobachtbar gekennzeichnet.
- Git-Argumente, Git-Konfigurations-Overrides und Umgebungsvariablen sind für alle Agent-Remote-
  Git-Aufrufe festgelegt; manipuliertes `core.fsmonitor` wird nicht ausgeführt. Der Worktree-
  `.git`-Verweis wird vor Git-Aufrufen geprüft; Erstanlage erfolgt ohne Checkout und mit
  deaktivierten Checkout-Filtern.
- Ergebnis, Anhang und Git-Stat; volle Antwort kommt nur auf Anfrage als `last.txt`, Logs werden
  nicht angehängt. Prompt-Injection in Titel, Log und Ergebnis startet keine Folgeaktion und
  Agent-Remote-Antworten landen nach Neustart nicht im KZG.
- Jede fremde Nachricht verwirft eine offene Startbestätigung, auch bekannte Agent-Remote-
  Befehle. Der Befehl `tests in <id>` wird nicht angeboten.
- Alle Nachrichten des Flows persistieren `meta.use_case="agent"`; der normale Modell- und
  Post-Turn-Pfad wird für diese Befehle umgangen.
- launchd-Umgebung: absolute Skript-/CLI-Pfade, korrektes `HOME`, `PATH`, `TMPDIR`, sichtbare
  Codex-Anmeldung im Benutzerkontext und redigierte Fehler bei nicht verfügbarer Auth.

## Offene Fragen an Timo

1. Soll das Limit von zwei nur Remote-Läufe zählen oder auch manuell am Rechner gestartete
   `codex-task`-Läufe? `was läuft?` zeigt in beiden Varianten alle bekannten Läufe.
2. Ist die bestehende `tracelog`-Ablage für Agent-Remote akzeptiert? Das vorhandene Script
   archiviert bei ausführbarem `~/.local/bin/tracelog` Prompt, Log, Output und Diff.
3. Sollen am Rechner gestartete Läufe in v1 nur angezeigt und ausgelesen werden, oder soll
   `abbrechen <id>` auch sie stoppen? Das aktuelle Registry-Format speichert keine sichere,
   eigene Prozessgruppe für diese Läufe.

## Entscheidungen (Claude, 14.10.2026 nachts; Timo bestätigt/ändert morgens)
1. Das Limit von 2 zählt nur Remote-Läufe; `was läuft?` zeigt alle.
2. Allowlist v1: nur `~/Mantis` (Alias „mantis“). Weitere Repos trägt Timo später ein.
3. Ja, tracelog-Ablage wie bei manuellen Läufen.
4. Am Rechner gestartete Läufe nur anzeigen/auslesen; `abbrechen` nur für Remote-Läufe mit eigener, von Mantis gestarteter Prozessgruppe.
5. Umgebung: Die LaunchAgent-Plist wird NICHT geändert. Mantis setzt für den codex-task-Subprozess selbst eine explizite Umgebung (HOME, PATH inkl. ~/.local/bin und der gebündelten Codex-CLI, dauerhaftes TMPDIR unter ~/Library/Caches/mantis/agent-remote). Live-Verifikation des Starts aus dem laufenden Mantis macht Claude nach dem Merge mit einem kleinen read-only-Auftrag.
6. `tests in <id>` entfällt in v1 vollständig. Codex führt erforderliche Tests innerhalb des bestätigten Laufs aus; die Antwort enthält eine Testzeile mit Befehl und Ergebnis. Agent Remote startet keine separaten Testprozesse und speichert dafür keine Registry-Felder oder Watchdogs.
