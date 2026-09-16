# Betriebshandbuch der Forge – Nachtbetrieb

## 1) Was passiert in einer Nacht
Die Forge arbeitet im Nachtfenster von 23:00 bis 07:00 Uhr (siehe `forge/daemon.py`). Der Daemon holt sich jeweils einen Task aus der Queue und bringt ihn genau eine Pipeline-Stufe weiter (speccing → planning → implementing → reviewing → gating). In der Stufe GATING läuft das deterministische Gate (`forge/gate.py`); bei Erfolg wechselt der Task nach `awaiting_approval` und wartet auf Timos Freigabe (`forge.cli approve`). Bei Gate-Fehler wird der Task mit den Gründen geparkt und kann später neu eingereiht werden. Der Daemon achtet auf drei Bremsen: Not-Aus-Datei (`~/.mantis-forge-stop`), drei aufeinanderfolgende Fehler (Fehler‑Spirale) und den weichen Halt (`~/.mantis-forge-halt`). Bei leerem Anbieter‑Kontingent schläft er 900 Sekunden bevor er erneut nachschaut.

## 2) Morgen‑Routine
0. Der Morgenbericht kommt per Telegram (Forge-Bot, `forge/melden.py`) —
   beim Fensterende, beim weichen Halt, und bei der Fehler-Spirale sofort
   mit „Forge abgeschaltet: <Grund>" in der ersten Zeile. Fehlt er, ist
   `FORGE_BOT_TOKEN` oder `TELEGRAM_CHAT_ID` nicht gesetzt (Warnung in
   `/tmp/mantis_forge_out.log`) — `forge.cli status` zeigt denselben Text.
1. Morgenbericht anzeigen: `python3.14 -m forge.cli status` (zeigt `forge/bericht.py`).
2. Im Bericht prüfen, welche Tasks unter „Zur Freigabe:“ stehen (Zustand `awaiting_approval`).
3. Für jeden Task entscheiden:
    - Freigeben: `python3.14 -m forge.cli approve <ID>`
    - Ablehnen und parken: `python3.14 -m forge.cli reject <ID> "<Grund>"`
    - Gefallenen/geparkten Task neu einreihen: `python3.14 -m forge.cli requeue <ID>`
4. Optional: geparkte Tasks ansehen (sie stehen im Bericht unter „Geparkt:“) und nach Fehlerursache suchen, bevor sie erneut in die Queue kommen.

## 3) Anhalten (Halt vs. Not‑Aus)
- **Weicher Halt** (`forge.cli stop`): schreibt `~/.mantis-forge-halt`. Der Daemon beendet sich nach dem aktuellen Tick (Exit 0). Für geplante Unterbrechung, wenn der aktuelle Lauf fertig sein soll.
- **Not‑Aus**: Datei `~/.mantis-forge-stop` existiert (per `touch` oder durch die Fehler‑Spirale gesetzt). Der Daemon prüft sie wie den Halt vor jedem Tick — ein laufender Tick läuft zu Ende. Danach arbeitet er nicht mehr, sondern wartet in 5‑Minuten‑Schritten; die Datei überlebt Neustarts und muss von Hand gelöscht werden. Ein hängender Daemon liest keine Datei — den beendet nur `launchctl bootout` oder `kill`.
- **Wann was**: Halt, wenn der laufende Daemon heute Nacht aufhören soll (die Datei verschwindet beim Beenden, die nächste Nacht läuft normal). Not‑Aus, wenn bis auf Weiteres keine Nacht mehr laufen soll — auch keine, die launchd um 23:00 startet.

## 3a) Telegram-Bot (t.me/AIMantisBot)
Läuft 24/7 (`forge/bot.py`, `com.mantis.forge-bot`). Kein LLM: Freitext wird
wörtlich zum Task, nichts wird interpretiert.

| Eingabe | Wirkung |
|---|---|
| Freitext | neuer Task (erste Zeile = Titel, Rest = Beschreibung), Antwort mit **[Verwerfen]** |
| `/status` | „Daemon läuft, #42 in implementing seit 23:14" bzw. „Daemon läuft nicht", darunter der Morgenbericht |
| `/queue` | wartende Tasks, je **[Verwerfen]** (= geparkt mit Grund, `/requeue` holt ihn zurück, Antwort mit **[Zurückholen]**) |
| `/requeue <id>` | wie `forge.cli requeue` |
| `/stop` | wie `forge.cli stop` (Halt-Datei nur bei laufendem Daemon) |

Freigeben (`approve`) geht bewusst **nicht** per Telegram — erst Diff im
Worktree lesen, dann `forge.cli approve` (siehe 2). Merge-Knöpfe kommen mit
Plan 3b.

Nachrichten, die geschickt wurden, während der Bot nicht lief (Neustart,
60-s-Wartezeit, DB weg), werden beim Start verworfen (`drop_pending_updates`)
— nach einem Neustart also nochmal schicken.

Bot antwortet nicht: `launchctl list | grep forge-bot`, dann
`/tmp/mantis_forge_bot_err.log`. Fremde Absender werden ignoriert und mit
ID geloggt. Exit 2 = Token oder Allowlist fehlt, Exit 3 = Datenbank nicht
erreichbar (launchd versucht es nach 60 s erneut).

## 4) Zustände eines Tasks (Tabelle)
| Zustand          | Bedeutung                                                            |
|------------------|----------------------------------------------------------------------|
| queued           | In der Warteschlange, noch nicht bearbeitet.                        |
| speccing         | Spezifikation wird erstellt.                                        |
| planning         | Planungsphase (Aufgaben‑Zerlegung).                                 |
| implementing     | Implementierung / Codierung.                                        |
| reviewing        | Review / Prüfung.                                                   |
| gating           | Gate‑Prüfung (deterministisch).                                     |
| awaiting_approval| Grünes Gate, wartet auf Timos Freigabe.                             |
| merged           | Task wurde in Hauptzweig gemerged (Endzustand).                     |
| parked           | Task vorübergehend abgelegt (Gate‑rot, Fehler, manuell).           |
| failed           | Task endgültig gescheitert, kann neu gestartet werden.             |

## 5) Was tun wenn …
- **Gate rot**: Der Task befindet sich im Zustand `parked`. Prüfe das Gate‑Log im Morgenbericht oder im Journal (`forge/journal.py`). Behebe die zugrundeliegende Spezifikation oder den fehlenden Artefakt, danach den Task mit `requeue` erneut in die Queue stellen.
- **Task geparkt**: Siehe den Grund im Bericht („Geparkt:“). Ursachen können fehlende Artefakte, Reviewer‑Kollisionen oder Worktree‑Probleme sein. Behebe die Ursache und führe `requeue` aus, um den Task erneut zu versuchen.
- **Anbieter leer**: Alle LLM‑Anbieter haben ihr Nachtkontingent aufgebraucht. Der Daemon schläft derzeit 900 Sekunden. Warte, bis das Kontingent wieder aufgefüllt ist (der Daemon wird es automatisch nach 900 Sekunden erneut versuchen) oder prüfe manuell die Anbieter‑Status in `forge/bericht.py` unter „Anbieter:“.
- **Kein Daemon-Start**: Prüfe, ob der launchd‑Job geladen ist (`launchctl list | grep com.mantis.forge`). Wenn nicht, führe den Installationsschritt aus `forge/launchd/README.md` aus (copy + bootstrap). Alternativ starte den Daemon direkt: `python3.14 -m forge.daemon`. Vorher sicherstellen, dass weder `~/.mantis-forge-stop` noch `~/.mantis-forge-halt` existieren, sonst stoppt er sofort.

---
*Hinweis: Dieses Handbuch beschreibt den Stand der Forge wie in den genannten Quelldateien zum Zeitpunkt des Schreibens.*
