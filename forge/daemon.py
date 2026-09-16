"""Hauptschleife der Forge.

Plan 1 (Fundament): ein Task, ein Worktree, ein Claude-Lauf, Journal, parken.
Plan 2 (dieser Stand): der Trockenlauf ist der vollen fünfstufigen Pipeline
(forge/pipeline.py) und dem deterministischen Gate (forge/gate.py) gewichen.
`tick()` bringt einen Task pro Aufruf genau eine Stufe weiter; meldet die
Pipeline "fertig" (Zustand GATING erreicht), lässt der Daemon selbst das Gate
laufen. Ein grünes Gate bringt den Task nach `awaiting_approval`, wo er auf
Timos Freigabe wartet (forge.cli approve, Plan 3). Gemerged wird hier noch
nichts.

Der Daemon hält sich an zwei Bremsen: Not-Aus-Datei und drei Fehlschläge in
Folge. Dazu kommt ab Plan 2c das Nachtfenster (23:00-07:00, launchd startet,
der Daemon beendet sich selbst) und der weiche Stop per Halt-Datei
(forge.cli stop) — beide sind kein Not-Aus, sondern beendete Läufe mit
Exit-Code 0.

Bewusst KEINE Bremse: eine parallel laufende interaktive Claude-Sitzung. Timo
hat sich am 2026-08-14 dagegen entschieden — die Forge soll durchlaufen, auch
während er selbst arbeitet. Der Preis: beide ziehen vom selben Kontingent, und
auf 16 GB RAM konkurriert die Test-Suite mit allem anderen. Ab Plan 3 ist die
Budget-Reserve die einzige Schranke dagegen; sie muss entsprechend
konservativ ausgelegt sein.
"""
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from core import db

from forge import MANTIS_REPO, gate, journal, melden, pipeline, queue, worktree
from forge import models as m

log = logging.getLogger(__name__)

STOP_FILE = Path.home() / ".mantis-forge-stop"
MAX_CONSECUTIVE_FAILURES = 3
IDLE_SLEEP_SECONDS = 60
BLOCKED_SLEEP_SECONDS = 300
# Backoff nach einem Fehlschlag: ohne das folgen auf einen Absturz sofort
# weitere Ticks, ohne jede Bremse — bis zu drei Vollpreis-Claude-Läufe in
# Sekunden, bevor die Fehler-Spirale überhaupt greift.
FAILURE_SLEEP_SECONDS = 30
# Backoff nach einem geparkten Task. Ein Park ist kein Fortschritt, und er kann
# entstehen, ohne dass ein einziger LLM-Lauf stattfand (Reviewer-Kollision,
# fehlendes Artefakt, kaputter Worktree). Ohne Bremse zieht der nächste Tick
# sofort den nächsten Task, legt Worktree und Branch an, parkt ihn und macht
# weiter — ein ganzer Backlog ist so in Sekunden verbrannt. Kürzer als
# FAILURE_SLEEP_SECONDS, weil ein Park den Task aus der Queue nimmt und der
# nächste durchaus echte Arbeit sein kann.
PARK_SLEEP_SECONDS = 10
# Wartezeit, wenn alle Anbieter für diese Nacht leer sind. Deutlich länger als
# die anderen Bremsen: vor dem nächsten Kontingent-Fenster ändert sich nichts,
# und jeder Tick bis dahin ist eine Datenbankabfrage ohne Ergebnis.
KONTINGENT_SLEEP_SECONDS = 900
# Nachtrag 2c: launchd startet um NACHT_BEGINN_STUNDE (forge/launchd/
# com.mantis.forge.plist, StartCalendarInterval), der Daemon beendet sich
# selbst ab NACHT_ENDE_STUNDE. Ein Task, der beim Fensterende aktiv ist,
# bleibt aktiv — die nächste Nacht setzt auf derselben Stufe auf, genau wie
# nach einem Absturz.
NACHT_BEGINN_STUNDE = 23
NACHT_ENDE_STUNDE = 7
# Weicher Stop (forge.cli stop): der laufende Tick endet, dann der Daemon.
# Anders als STOP_FILE wird die Datei beim Beenden gelöscht — sie ist eine
# Bitte, kein Not-Aus.
HALT_FILE = Path.home() / ".mantis-forge-halt"
# Die API-Schlüssel der Gratis-Anbieter (NVIDIA, Google, Groq, Mistral) liegen
# hier und werden interaktiv aus .zshrc gesourct. launchd sourct keine .zshrc:
# in der ersten Nacht (2026-09-14, 23:00) startete der Daemon ohne einen
# einzigen Schlüssel, opencode meldete "Method doesn't allow unregistered
# callers", und Task 365 parkte nach drei Sekunden. Der Daemon lädt die Datei
# deshalb selbst — und loggt dabei nur die NAMEN, nie die Werte.
API_SCHLUESSEL_DATEI = Path.home() / ".config" / "ai-keys.env"
# Nachtrag 3a: TELEGRAM_CHAT_ID steht in der Mantis-.env, nicht in
# ai-keys.env. Der Daemon lädt beide (nur Namen ins Log, nie Werte); die
# .env-Werte kennt der Prozess über core.db/settings ohnehin schon.
ENV_DATEI = MANTIS_REPO / ".env"
# Korrektur nach Review 16.09.: die Mantis-.env trägt auch ANTHROPIC_API_KEY,
# den Mantis-Bot-Token, GOOGLE_CLIENT_SECRET usw. — und die Forge reicht
# os.environ ungefiltert an jeden Agenten-Subprozess weiter (runner_opencode.py:
# dict(os.environ), gate.py: {**os.environ, ...}). Ungefiltert geladen wären
# diese Geheimnisse ab dem nächsten Tick in jedem Claude-/opencode-Lauf
# sichtbar. lade_api_schluessel(ENV_DATEI, nur=ENV_NUR) lädt deshalb nur die
# beiden Namen, die der Telegram-Bot tatsächlich braucht (Task 5 nutzt
# dieselbe Konstante für seine eigene Allowlist-Prüfung).
ENV_NUR = frozenset({"TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_IDS"})


def im_nachtfenster(jetzt: datetime | None = None) -> bool:
    """23:00 bis 06:59 — die Stunden, in denen die Forge arbeitet."""
    jetzt = jetzt or datetime.now()
    return jetzt.hour >= NACHT_BEGINN_STUNDE or jetzt.hour < NACHT_ENDE_STUNDE


def halt_angefordert() -> bool:
    return HALT_FILE.exists()


def lade_api_schluessel(datei: Path = API_SCHLUESSEL_DATEI, nur: frozenset[str] | None = None) -> list[str]:
    """Lädt `KEY=WERT`-Zeilen (auch mit `export`, auch in Anführungszeichen)
    in os.environ — nur Variablen, die dort noch fehlen. Rückgabe: die Namen
    der geladenen Variablen. Eine fehlende Datei ist kein Fehler: dann muss
    die Umgebung die Schlüssel schon mitbringen. Mit `nur` gesetzt werden
    ausschließlich Namen aus dieser Menge geladen — Geheimnisse der
    Mantis-.env (Anthropic-Key, Bot-Token, OAuth-Secrets, ...) dürfen nicht
    ungefiltert in Agenten-Subprozesse gelangen."""
    if not datei.is_file():
        return []
    geladen: list[str] = []
    for zeile in datei.read_text().splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        if zeile.startswith("export "):
            zeile = zeile[len("export "):]
        name, wert = zeile.split("=", 1)
        name, wert = name.strip(), wert.strip().strip('"').strip("'")
        if not name or (nur is not None and name not in nur) or name in os.environ:
            continue
        os.environ[name] = wert
        geladen.append(name)
    return geladen


def should_run(failures: int) -> tuple[bool, str]:
    """Darf gerade gearbeitet werden? Zweiter Rückgabewert ist der Grund."""
    if STOP_FILE.exists():
        return False, f"Not-Aus aktiv ({STOP_FILE})"
    if failures >= MAX_CONSECUTIVE_FAILURES:
        return False, f"{failures} Fehlschläge in Folge — Daemon hält an"
    return True, "frei"


def _park(task_id: int, current: str, reason: str) -> None:
    """Parkt und journalt den Grund. Wie forge.pipeline._park: ein verworfener
    park()-Aufruf darf nicht spurlos bleiben, sonst hält ein Task, den nichts
    mehr bewegen kann, die Queue fest, ohne dass irgendwo sichtbar wird warum."""
    journal.log(task_id, "stage_failed", reason)
    if queue.zaehle_fehlschlag(task_id, current=current):
        # Schwelle bereits erreicht — queue.zaehle_fehlschlag hat den Task
        # automatisch geparkt. Ein zweiter park()-Aufruf mit dem spezifischeren
        # Grund würde nur noch am inzwischen falschen Ausgangszustand scheitern.
        return
    if not queue.park(task_id, current=current, reason=reason):
        journal.log(task_id, "stage_failed",
                     f"park() hat Task {task_id} nicht angenommen (Zustand '{current}')")


def _gate_und_abschliessen(task_id: int, baum: Path) -> str:
    """Der Task steht in GATING. Lässt das deterministische Gate laufen und
    schließt ihn ab — grün bringt ihn nach `awaiting_approval`, wo er auf
    Timos Freigabe wartet (forge.cli approve, Plan 3), rot parkt ihn mit
    allen gesammelten Gründen.

    Rückgabe: 'fertig' bei grünem Gate, 'geparkt' bei rotem oder wenn der
    Zustandswechsel selbst scheitert (CAS verloren).
    """
    ergebnis = gate.pruefe(baum)
    if ergebnis.ok:
        journal.log(task_id, "gate_pass", "Gate bestanden — wartet auf Freigabe (forge.cli approve)")
        if not queue.set_state(task_id, m.AWAITING_APPROVAL, current=m.GATING):
            # CAS verloren — "fertig" zurückzugeben würde einen Fortschritt
            # vorgaukeln, der laut DB nie stattfand.
            _park(task_id, m.GATING,
                  f"Zustandswechsel {m.GATING} -> {m.AWAITING_APPROVAL} schlug fehl (Task {task_id})")
            return "geparkt"
        # Grünes Gate ist der Abschluss der Kette — der Fehlschlag-Zähler
        # beginnt neu (siehe forge/queue.py: versuche_zuruecksetzen).
        queue.versuche_zuruecksetzen(task_id)
        return "fertig"

    gruende = "; ".join(ergebnis.gruende)
    journal.log(task_id, "gate_fail", gruende)
    _park(task_id, m.GATING, f"Gate rot: {gruende}")
    return "geparkt"


def tick() -> str:
    """Ein Durchlauf. Rückgabe: 'leerlauf' | 'weiter' | 'fertig' | 'geparkt' | 'fehler' | 'kontingent'.

    Bringt den aktiven (oder nächsten) Task genau eine Pipeline-Stufe weiter
    (forge/pipeline.py). Meldet die Pipeline "fertig", ist der Task in GATING
    angekommen — dann läuft hier direkt im Anschluss das Gate. Ein Task, der
    bereits VOR diesem Tick in GATING steht (Wiederaufnahme nach einem
    Absturz zwischen "fertig" und dem Gate-Lauf), überspringt die Pipeline
    ganz: sie kennt den Zustand GATING nicht (deckt nur speccing..reviewing
    ab) und würde ihn sonst mit "Kein Stufen-Handler" parken, ohne dass das
    Gate je gelaufen wäre.

    Ab dem Moment, in dem `queue.claim_next()` einen Task liefert, steht dessen
    Zustand in der DB auf einer ACTIVE_STATES-Stufe. Alles danach läuft in
    einem try/except: jede Ausnahme wird geloggt (mit der echten Task-ID, nie
    None) und der Task wird bestmöglich geparkt, BEVOR die Ausnahme weiter
    nach oben gereicht wird — sonst bleibt er aktiv hängen, und der nächste
    Tick zieht genau denselben poisoned Task wieder, ohne Backoff.

    pipeline.eine_stufe() fängt ihre eigenen Ausnahmen bereits ab (nie mehr
    eine Exception aus einem Claude-Lauf) — das try/except hier bleibt trotzdem
    stehen, es sichert weiterhin echte Absturzquellen wie worktree.create()
    und einen unerwarteten Fehler im Gate-Anschluss selbst ab.
    """
    task = queue.claim_next()
    if task is None:
        return "leerlauf"

    task_id = task["id"]
    state = task["state"]
    try:
        baum = worktree.create(task_id)

        if state == m.GATING:
            return _gate_und_abschliessen(task_id, baum)

        ergebnis = pipeline.eine_stufe(task, baum)

        if ergebnis == "fertig":
            # eine_stufe() hat den Übergang bereits nach GATING vollzogen —
            # `state` muss das nachziehen, damit park() im Absturzfall (siehe
            # except unten) mit dem tatsächlichen DB-Zustand als CAS-Basis
            # arbeitet, nicht mit dem veralteten Ausgangszustand.
            state = m.GATING
            return _gate_und_abschliessen(task_id, baum)

        return ergebnis
    except Exception as exc:
        journal.log(task_id, "stage_failed", f"Tick-Absturz bei Task {task_id}: {exc}")
        try:
            # Buchführung für den Fehlschlag-Zähler — best effort, darf den
            # eigentlichen Park-Versuch gleich danach nicht verhindern.
            queue.zaehle_fehlschlag(task_id, current=state)
        except Exception:
            log.exception(f"Forge: Fehlschlag-Zählung für Task {task_id} selbst gescheitert")
        try:
            geparkt = queue.park(task_id, current=state, reason=f"Tick-Absturz: {exc}")
            if not geparkt:
                log.error(f"Forge: Task {task_id} nach Absturz nicht parkbar — bleibt aktiv")
        except Exception:
            # Das Parken selbst darf die ursprüngliche Ausnahme nicht verdecken.
            log.exception(f"Forge: Parken nach Absturz für Task {task_id} selbst gescheitert")
        raise


def _abschluss(grund: str | None = None) -> None:
    """Morgenbericht per Telegram, an jedem Ende von main() (Nachtrag 3a).
    Bei der Fehler-Spirale steht der Grund in der ersten Zeile, damit Timo
    das nicht erst um 07:00 im Bericht sucht. Nichts hier darf werfen:
    melden.sende wirft nie, und ein kaputter Bericht wird als Text gemeldet.

    Der Import ist lokal, weil forge.bericht seinerseits forge.daemon
    importiert (STOP_FILE) — ein Modul-Import wäre ein Zirkel."""
    from forge import bericht
    try:
        text = bericht.morgenbericht()
    except Exception as exc:
        # Der Bericht ist Beiwerk, die Meldung nicht — ein kaputter Bericht
        # darf die Telegram-Meldung nicht verhindern.
        log.exception("Forge: Morgenbericht nicht erstellbar")
        text = f"Morgenbericht nicht erstellbar: {exc}\n"
    if grund:
        text = f"Forge abgeschaltet: {grund}\n\n{text}"
    if not melden.sende(text):
        # melden.sende loggt Details (HTTP-Status/Ausnahme-Typ) bereits selbst,
        # nie Token oder URL — diese Zeile ist dafür da, dass das launchd-
        # stdout um 07:00 überhaupt zeigt, dass etwas fehlte.
        log.warning("Forge: Abschlussmeldung nicht zugestellt (siehe melden-Warnung)")


def main() -> None:
    """launchd-Einstieg. Läuft, bis das Nachtfenster endet (NACHT_ENDE_STUNDE),
    ein weicher Halt angefordert wird (forge.cli stop), der Not-Aus steht oder
    die Fehler-Spirale greift. Die ersten beiden enden mit Exit 0."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    schluessel = lade_api_schluessel(API_SCHLUESSEL_DATEI)
    log.info(f"Forge: API-Schlüssel aus {API_SCHLUESSEL_DATEI.name} geladen: {', '.join(schluessel) or 'keine'}")
    umgebung = lade_api_schluessel(ENV_DATEI, nur=ENV_NUR)
    log.info(f"Forge: aus {ENV_DATEI.name} geladen: {', '.join(umgebung) or 'nichts Neues'}")
    db.init_pool()
    # Die Forge ist bewusst unabhängig vom laufenden Mantis-Assistant (siehe
    # forge/__init__.py) — sie darf sich also nicht darauf verlassen, dass
    # irgendein anderer Prozess vor ihr migriert hat.
    db.run_migrations()
    journal.log(None, "daemon_start", "Forge gestartet")
    log.info("Forge-Daemon gestartet")
    # Ein Halt ist eine Bitte an den LAUFENDEN Daemon (forge.cli stop). Beim
    # Start läuft noch keiner — die Datei ist also veraltet: entweder ein
    # Absturz/SIGKILL zwischen dem Setzen und dem Löschen, oder ein
    # Fensterausgang, der die Datei (vor diesem Fix) überleben ließ. Timos
    # Stop am Tag darf die kommende Nacht nicht canceln — dafür gibt es die
    # Not-Aus-Datei (STOP_FILE).
    if HALT_FILE.exists():
        HALT_FILE.unlink(missing_ok=True)
        log.warning("Forge: veraltete Halt-Datei beim Start entfernt — ein Halt ist eine Bitte an den laufenden Daemon")

    failures = 0
    while True:
        if halt_angefordert():
            journal.log(None, "daemon_stop", "Weicher Stop angefordert (forge.cli stop) — Daemon beendet sich")
            log.info("Forge: weicher Stop")
            HALT_FILE.unlink(missing_ok=True)
            _abschluss()
            return
        if not im_nachtfenster():
            journal.log(None, "daemon_stop",
                        f"Nachtfenster zu Ende ({NACHT_ENDE_STUNDE}:00) — Daemon beendet sich, "
                        f"launchd startet um {NACHT_BEGINN_STUNDE}:00 neu")
            log.info("Forge: Nachtfenster zu Ende")
            _abschluss()
            return

        erlaubt, grund = should_run(failures)
        if not erlaubt:
            log.info(f"Forge pausiert: {grund}")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                # Die Bremse MUSS den launchd-Neustart überleben. launchd startet nicht
                # mehr per KeepAlive neu, sondern erst wieder um NACHT_BEGINN_STUNDE
                # Uhr (StartCalendarInterval) — ein bloßes return würde diesen Neustart
                # mit failures=0 abwarten und dieselben drei Fehlläufe in der nächsten
                # Nacht erneut verbrennen. Die Not-Aus-Datei ist der einzige Zustand,
                # den auch dieser Neustart nicht vergisst.
                # Bekannte Lücke: greift die Spirale in den letzten FAILURE_SLEEP_SECONDS
                # vor NACHT_ENDE_STUNDE, kann der Daemon durch das Nachtfenster hindurch
                # beendet werden, BEVOR diese Zeile läuft — die nächste Nacht startet
                # dann mit failures=0 und wiederholt bis zu drei Fehlläufe. Begrenzt auf
                # FAILURE_SLEEP_SECONDS und bewusst in Kauf genommen.
                STOP_FILE.write_text(f"Fehler-Spirale: {grund}\n")
                journal.log(None, "daemon_stop", f"{grund} — Not-Aus gesetzt, Freigabe durch Timo")
                _abschluss(grund)
                return
            time.sleep(BLOCKED_SLEEP_SECONDS)
            continue

        try:
            ergebnis = tick()
        except Exception as exc:  # ein Task darf den Daemon nicht töten
            log.exception("Forge-Tick abgestürzt")
            journal.log(None, "stage_failed", f"Tick-Absturz: {exc}")
            ergebnis = "fehler"

        failures = failures + 1 if ergebnis == "fehler" else 0
        if ergebnis == "leerlauf":
            time.sleep(IDLE_SLEEP_SECONDS)
        elif ergebnis == "fehler":
            time.sleep(FAILURE_SLEEP_SECONDS)
        elif ergebnis == "geparkt":
            time.sleep(PARK_SLEEP_SECONDS)
        elif ergebnis == "kontingent":
            time.sleep(KONTINGENT_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
