"""Die Stufenmaschine — bringt einen Task pro Tick genau eine Stufe weiter.

`eine_stufe` ist bewusst zustandslos gegenüber sich selbst: sie liest den
aktuellen Task- und Worktree-Zustand, macht GENAU EINEN Claude-Lauf und
entscheidet danach, wie es weitergeht. Das hält Ticks kurz, gibt dem Daemon
nach jedem Absturz einen sauberen Wiedereinstiegspunkt, und lässt in Plan 3
Platz für die Budget-Prüfung, die zwischen "Stufe bestimmen" und "Stufe
ausführen" treten wird.

Rückgabewerte:
  "weiter"  — ein Übergang wurde vollzogen, es geht mit der nächsten Stufe weiter
  "fertig"  — die Kette hat GATING erreicht, Plan 3 übernimmt (Merge/Restart)
  "geparkt" — der Task liegt zur manuellen Sichtung bereit
  "fehler"  — Rate-Limit; Zustand bewusst unverändert, Plan 3 hängt hier die
              Wartezeit ein. Ein Park hier würde jede Kontingentgrenze in
              einen verlorenen Task verwandeln.
"""
import json
import logging
import re
import time
from pathlib import Path

from forge import gate, gitctl, journal, models as m, queue, runner, stages

log = logging.getLogger(__name__)

# Ab dieser Fix-Runden-Zahl wird nicht mehr weiterversucht, sondern geparkt.
# Ohne Grenze könnte ein Task, dessen Fixes das Review nie überzeugen, endlos
# Läufe verbrauchen, ohne dass je ein Mensch davon erfährt.
MAX_FIXRUNDEN = 2

# Dieselbe Basis wie forge.gate.pruefe()'s Default — der Diff, den Review zu
# sehen bekommt, muss derselbe sein, den später auch das Gate beurteilt.
BASIS_BRANCH = "main"

# Welches Task-Feld ein Stufen-Artefakt persistiert. Nur spec und plan liefern
# einen Freitext-Pfad in ihrer Antwort, den es zu sichern gilt; review hat ein
# festes Ziel (VERDIKT_DATEI), implement/fix versprechen gar kein Artefakt —
# für die drei gibt es hier bewusst keinen Eintrag.
_ARTEFAKT_FELD_JE_STUFE = {"spec": "spec_path", "plan": "plan_path"}


def _artefakt_vorhanden(worktree: Path, pfad: str) -> bool:
    """Existiert das von der Stufe versprochene Artefakt im Worktree?"""
    return (Path(worktree) / pfad).is_file()


def _ist_timeout(ergebnis: runner.RunResult) -> bool:
    """Erkennt einen an der Zeitgrenze gescheiterten Lauf an forge.runner.run's
    eigenem Fehlertext ('Zeitüberschreitung nach <n>s', siehe dort). Ein Timeout
    ist der einzige Fehlschlag-Grund, bei dem sich ein Blick auf das tatsächliche
    Produkt lohnt — bei jedem anderen Fehler (Absturz, is_error) hat der Lauf gar
    nicht erst geliefert."""
    return "Zeitüberschreitung nach" in (ergebnis.error or "")


def _juengste_datei_seit(verzeichnis: Path, seit: float) -> Path | None:
    """Die zuletzt geänderte Datei in `verzeichnis`, sofern sie jünger als `seit`
    ist — Fallback für spec/plan, wenn ein Timeout keine Modellantwort (und
    damit keinen geparsten Pfad) hinterlassen hat, das Artefakt aber trotzdem
    geschrieben wurde."""
    if not verzeichnis.is_dir():
        return None
    kandidaten = [d for d in verzeichnis.iterdir() if d.is_file() and d.stat().st_mtime >= seit]
    if not kandidaten:
        return None
    return max(kandidaten, key=lambda d: d.stat().st_mtime)


def _hat_neuen_commit(worktree: Path) -> bool:
    """Trägt der Branch mindestens einen Commit, der über BASIS_BRANCH
    hinausgeht? Das ist das Produkt von implement/fix — beide versprechen kein
    Artefakt-Dokument, sondern einen committeten Codestand."""
    ergebnis = gitctl.run("rev-list", "--count", f"{BASIS_BRANCH}..HEAD", cwd=worktree)
    if ergebnis.returncode != 0:
        return False
    try:
        return int((ergebnis.stdout or "0").strip()) > 0
    except ValueError:
        return False


def _verdikt_lesbar(worktree: Path) -> bool:
    """Liegt VERDIKT_DATEI vor und lässt sie sich als JSON parsen? Das ist das
    Produkt der review-Stufe — ob das Urteil selbst 'pass' oder 'fail' ist,
    spielt hier keine Rolle, nur ob die Stufe überhaupt geliefert hat."""
    datei = Path(worktree) / stages.VERDIKT_DATEI
    if not datei.is_file():
        return False
    try:
        json.loads(datei.read_text())
        return True
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def _timeout_produkt(stufe: stages.Stage, task: dict, worktree: Path, start: float) -> tuple[bool, str | None]:
    """Akzeptanzlauf 2026-08-15, Fund 1: eine Zeitüberschreitung darf bereits
    geleistete, bezahlte Arbeit nicht wegwerfen. Prüft, ob die Stufe ihr Produkt trotz
    eigenem Timeout geliefert hat, und liefert für spec/plan zusätzlich den
    gefundenen Pfad zurück (auf dem üblichen Weg über die Modellantwort steht
    er nicht zur Verfügung — bei einem Timeout ist die Antwort leer).

    Definition 'Produkt' je Stufe (siehe Aufgabenstellung):
      spec/plan   — das Artefakt-Dokument existiert
      implement/fix — mindestens ein neuer Commit auf dem Task-Branch
      review      — VERDIKT_DATEI existiert und lässt sich parsen
    """
    if stufe.name in _ARTEFAKT_FELD_JE_STUFE:
        feld = _ARTEFAKT_FELD_JE_STUFE[stufe.name]
        bekannt = task.get(feld)
        if bekannt and _artefakt_vorhanden(worktree, bekannt):
            return True, bekannt
        verzeichnis = Path(worktree) / (stages.SPEC_VERZEICHNIS if stufe.name == "spec" else stages.PLAN_VERZEICHNIS)
        neuestes = _juengste_datei_seit(verzeichnis, start)
        if neuestes is None:
            return False, None
        return True, str(neuestes.relative_to(worktree))
    if stufe.name in ("implement", "fix"):
        return _hat_neuen_commit(worktree), None
    if stufe.name == "review":
        return _verdikt_lesbar(worktree), None
    return False, None


def _kontext(task: dict) -> dict:
    """Der Kontext, den Prompts der Folgestufen brauchen — Pfade, keine
    Inhalte. Die Übergabe läuft über Dateien im Worktree, nie über
    Gesprächsverlauf (siehe forge/stages.py)."""
    return {"spec_path": task.get("spec_path"), "plan_path": task.get("plan_path")}


def _hat_negatives_verdikt(worktree: Path) -> bool:
    """Liegt bereits ein Review-Urteil vor, UND ist es negativ?

    Bewusst NICHT dasselbe wie forge.gate.lies_verdikt: das Gate ist fail-safe
    und behandelt ein fehlendes Urteil als 'fail', weil vor einem Merge im
    Zweifel nichts durchgehen darf. Hier würde dieselbe Regel jede frische
    Review-Stufe sofort in die Fix-Runde umleiten, noch bevor Review
    überhaupt einmal gelaufen ist — deshalb zählt 'Datei fehlt' hier
    ausdrücklich NICHT als negativ.
    """
    datei = Path(worktree) / stages.VERDIKT_DATEI
    if not datei.is_file():
        return False
    verdikt_ok, _ = gate.lies_verdikt(worktree)
    return not verdikt_ok


def _waehle_stufe(state: str, worktree: Path) -> tuple[stages.Stage | None, bool]:
    """Welche Stufe für diesen Zustand läuft — und ob es die Fix-Stufe ist.

    Die Fix-Stufe steht bewusst neben der Kette (siehe forge/stages.py) und
    ist über fuer_state() nicht erreichbar; die Pipeline fordert sie hier
    gezielt an, wenn REVIEWING mit einem bereits vorliegenden negativen
    Urteil angetroffen wird.
    """
    if state == m.REVIEWING and _hat_negatives_verdikt(worktree):
        return stages.FIX_STAGE, True
    return stages.fuer_state(state), False


def _denial_namen(denials: list[dict]) -> str:
    """Sortierte, deduplizierte Werkzeugnamen aus permission_denials — sonst
    sieht eine zu eng berechtigte Stufe wie ein fehlendes Artefakt aus (siehe
    Modul-Docstring von forge/runner.py, Probe b2)."""
    namen = sorted({d.get("tool_name", "?") for d in denials if isinstance(d, dict)})
    return ", ".join(namen) or "(unbenannt)"


def _schreibe_diff(worktree: Path) -> str | None:
    """Befüllt stages.DIFF_DATEI mit dem Diff des Branches gegen seine Basis.
    Gibt bei Erfolg None zurück, sonst eine Fehlermeldung.

    Die Review-Stufe hat kein Bash und kann sich keinen eigenen Diff
    erzeugen — ohne diese Datei prüfte sie eine Spec ohne jede Sicht auf die
    tatsächliche Änderung. Die Analogie zu forge.gate.py's Nachsicht bei
    fehlendem Diff trägt hier NICHT: gate.py's Nachsicht landet immer noch auf
    einem harten Blocker ("keine Änderungen im Branch"), aber ein leerer,
    fälschlich als Erfolg durchgereichter Diff hier gäbe einem LLM die
    Möglichkeit, ein vertrauenswürdig aussehendes 'pass'-Urteil über eine
    Änderung zu fällen, die es nie gesehen hat — und genau dieses Urteil
    füttert das Gate. Deshalb gilt die strikte Lesart: schon ein
    fehlgeschlagenes `git diff` (returncode != 0 — kaputter Ref, kaputtes
    Worktree, exakt das, wovor forge/gitctl.py sich sorgt) parkt den Task,
    statt einen leeren Diff als Erfolg auszugeben. Ein tatsächlicher
    Schreibfehler (Berechtigung, Platte voll, kaputter Pfad) ist die zweite,
    unabhängige Fehlerursache.
    """
    worktree = Path(worktree)
    ergebnis = gitctl.run("diff", f"{BASIS_BRANCH}...HEAD", cwd=worktree)
    if ergebnis.returncode != 0:
        fehler = (f"git diff fehlgeschlagen (returncode {ergebnis.returncode}): "
                  f"{(ergebnis.stderr or '').strip()[:300]}")
        log.warning(f"Forge-Pipeline: {fehler}")
        return fehler
    ziel = worktree / stages.DIFF_DATEI
    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_text(ergebnis.stdout or "")
    except OSError as exc:
        fehler = f"Diff-Artefakt konnte nicht geschrieben werden: {exc}"
        log.warning(f"Forge-Pipeline: {fehler}")
        return fehler
    return None


def _verwirf_review_artefakte(worktree: Path) -> None:
    """Löscht das (jetzt veraltete) Review-Urteil und den zugehörigen Diff,
    nachdem eine Fix-Runde erfolgreich abgeschlossen wurde.

    Ohne das läse die nächste REVIEWING-Stufe (_hat_negatives_verdikt) exakt
    dasselbe Vor-Fix-Urteil erneut und würde den Task sofort wieder in die
    Fix-Stufe zurückschicken, OHNE dass die eigentliche Review-Stufe je
    gelaufen wäre — das ist Kritischer Fund 1. `missing_ok=True`, weil ein
    fehlgeschlagener Fix-Lauf (parkt vorher) oder ein bereits aufgeräumter
    Worktree hier nichts vorfinden muss.
    """
    for pfad in (stages.VERDIKT_DATEI, stages.DIFF_DATEI):
        datei = Path(worktree) / pfad
        try:
            datei.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(f"Forge-Pipeline: veraltetes Review-Artefakt konnte nicht gelöscht werden "
                        f"({datei}): {exc}")


def _committe_artefakt(worktree: Path, stufe_name: str, task_id: int, pfad: str) -> str | None:
    """Akzeptanzlauf 2026-08-15, Fund 3: committet das Artefakt einer spec- oder
    plan-Stufe mit explizitem Pfad. Gibt bei Erfolg None zurück, sonst eine
    Fehlermeldung.

    Das Gate (forge/gate.py) und die Review-Stufe urteilen ausschließlich über
    `git diff main...HEAD` — ein unkommittetes Dokument im Worktree ist für
    beide unsichtbar, obwohl die Stufe ihre Aufgabe vollständig erfüllt hat.
    Bewusst ZWEI git-Aufrufe mit demselben expliziten Pfad (`add` dann
    `commit`), nie `git add -A` oder `git commit -a`: der Worktree kann noch
    andere, nicht zu dieser Stufe gehörende Änderungen enthalten (z.B. ein
    Artefakt einer vorherigen, geparkten Stufe), und die dürfen hier nicht
    versehentlich mit hineinrutschen.
    """
    hinzugefuegt = gitctl.run("add", "--", pfad, cwd=worktree)
    if hinzugefuegt.returncode != 0:
        return (f"git add fehlgeschlagen (returncode {hinzugefuegt.returncode}): "
                f"{(hinzugefuegt.stderr or '').strip()[:300]}")
    nachricht = f"docs(forge): Artefakt der Stufe '{stufe_name}' für Task {task_id} committet"
    committet = gitctl.run("commit", "-m", nachricht, "--", pfad, cwd=worktree)
    if committet.returncode != 0:
        return (f"git commit fehlgeschlagen (returncode {committet.returncode}): "
                f"{(committet.stderr or '').strip()[:300]}")
    return None


class _PfadUngueltig(Exception):
    """Der von einer Stufe gelieferte Pfad-Kandidat übersteht die Normalisierung
    nicht (leer, absolut, oder mit einer '..'-Komponente). Trägt bewusst den
    ROHEN, unbereinigten Text: die bereinigte Fassung ist ja gerade als
    unbrauchbar verworfen worden, und wenn diese Prüfung selbst einmal auf
    eine neue, hier nicht bedachte Formatierung trifft, ist der rohe Modelltext
    das Einzige, was den Fund noch diagnostizierbar macht (siehe Akzeptanzlauf
    2026-08-14: `Erwartetes Artefakt fehlt: ...` allein verriet nicht, dass die
    Antwort in Backticks steckte)."""

    def __init__(self, roh: str):
        self.roh = roh
        super().__init__(roh)


# Eine Codezaun-Zeile für sich (```, ```python, ~~~) ist niemals der Pfad
# selbst — sie markiert nur den Rand eines Blocks, in den ein Modell den Pfad
# trotz Anweisung setzen kann.
_ZAUN_ZEILE = re.compile(r"^(`{3,}|~{3,})\s*[\w.+-]*$")

# Markdown-Link-Form: [irgendein Text](der/eigentliche/pfad.md)
_MARKDOWN_LINK = re.compile(r"^\[[^\]]*\]\(([^)]+)\)$")


def _erwarteter_pfad_und_feld(stufe: stages.Stage, task: dict,
                               ergebnis: runner.RunResult) -> tuple[str | None, str | None]:
    """Der Artefakt-Pfad, den diese Stufe versprochen hat, plus das Feld, in
    dem er (falls zutreffend) in der Queue landet.

    spec und plan lesen ihren Pfad noch nicht aus dem Task — der wird ja
    gerade erst in DIESEM Lauf bekannt (die Antwort des Modells). Deshalb wird
    der Task hier lokal um das frisch gelieferte Feld angereichert, bevor
    stufe.artefakt() aufgerufen wird; review und implement/fix ignorieren den
    Task ohnehin (konstantes Ziel bzw. gar keins).

    Wirft _PfadUngueltig, wenn der Kandidat auch nach Normalisierung nicht
    wie ein Repo-relativer Pfad aussieht — siehe _bereinige_pfad.
    """
    feld = _ARTEFAKT_FELD_JE_STUFE.get(stufe.name)
    if feld is None:
        return stufe.artefakt(task), None
    roh = _letzte_zeile(ergebnis.text)
    kandidat = _bereinige_pfad(roh)
    if not _ist_gueltiger_relativer_pfad(kandidat):
        raise _PfadUngueltig(roh)
    angereichert = {**task, feld: kandidat}
    return stufe.artefakt(angereichert), feld


def _letzte_zeile(text: str) -> str:
    """Die Prompts von spec und plan verlangen 'am Ende nur den Pfad' — das
    Modell kann trotzdem Freitext davor setzen (z.B. eine 'Annahme:'-Zeile,
    die derselbe Prompt ausdrücklich erlaubt). Die letzte nicht-leere Zeile
    ist robuster als der gesamte Text.

    Reine Codezaun-Zeilen zählen dabei nicht als Kandidat: setzt ein Modell
    den Pfad in einen ```-Block, ist sonst die SCHLIESSENDE Zaun-Zeile die
    'letzte nicht-leere Zeile', nicht der Pfad selbst."""
    zeilen = [z.strip() for z in (text or "").splitlines() if z.strip()]
    kandidaten = [z for z in zeilen if not _ZAUN_ZEILE.match(z)]
    return kandidaten[-1] if kandidaten else ""


def _bereinige_pfad(roh: str) -> str:
    """Entfernt die Verpackung, mit der ein Modell einen Pfad trotz expliziter
    Anweisung ('antworte am Ende nur mit dem Pfad') umgibt: Markdown-Link-
    Klammern, Backticks, Anführungszeichen, ein schließender Satzpunkt und ein
    führendes './'. Bewusst konservativ — der Pfad selbst wird nie
    umgeschrieben, nur seine Verpackung entfernt.

    Belegt durch den Akzeptanzlauf vom 2026-08-14: die echte Modellantwort war
    '`docs/superpowers/specs/2026-08-14-ist-wochenende-design.md`' (der sonst
    korrekte Pfad in Backticks) — der bis dahin unbereinigte Pfad-Check verfehlte
    das existierende Artefakt, weil er wortwörtlich nach einer Datei suchte, deren
    Name die Backticks mit einschließt.
    """
    pfad = (roh or "").strip()

    link = _MARKDOWN_LINK.match(pfad)
    if link:
        pfad = link.group(1).strip()

    # Backticks und Anführungszeichen können ineinander verschachtelt sein
    # (z.B. Anführungszeichen um Backticks) — deshalb wird abwechselnd
    # gestrippt, bis eine Runde nichts mehr ändert.
    vorher = None
    while vorher != pfad:
        vorher = pfad
        pfad = pfad.strip().strip("`").strip("'\"").strip()

    if pfad.endswith(".") and not pfad.endswith(".."):
        pfad = pfad[:-1]

    while pfad.startswith("./"):
        pfad = pfad[2:]

    return pfad.strip()


def _ist_gueltiger_relativer_pfad(pfad: str) -> bool:
    """Sieht das nach der Bereinigung noch wie ein Repo-relativer Pfad aus?

    Weder ein absoluter Pfad noch eine '..'-Komponente lässt sich sinnvoll
    unter dem Worktree einordnen — beides muss zum Park führen statt zu einem
    Dateisystem-Zugriff außerhalb des vorgesehenen Bereichs."""
    if not pfad:
        return False
    if pfad.startswith("/"):
        return False
    return ".." not in pfad.split("/")


def _park(task_id: int, state: str, grund: str) -> None:
    journal.log(task_id, "stage_failed", grund)
    # Wie forge.daemon.tick(): ein verworfener park()-Aufruf darf nicht
    # spurlos bleiben, sonst hält ein Task, den weder set_state noch park
    # bewegen konnten, die Queue fest, ohne dass irgendwo sichtbar wird warum.
    if not queue.park(task_id, current=state, reason=grund):
        journal.log(task_id, "stage_failed",
                     f"park() hat Task {task_id} nicht angenommen (Zustand '{state}')")


def eine_stufe(task: dict, worktree: Path) -> str:
    """Bringt `task` genau eine Stufe weiter. Siehe Modul-Docstring für die
    Rückgabewerte."""
    task_id = task["id"]
    state = task["state"]
    try:
        return _eine_stufe_intern(task, task_id, state, Path(worktree))
    except Exception as exc:
        # Eine Ausnahme aus irgendeinem verdrahteten Modul (Queue, Journal,
        # Gate, gitctl, ...) darf den Task niemals aktiv und spurlos hängen
        # lassen — der nächste Tick zöge sonst denselben Task ohne jede Spur
        # im Journal wieder. Der Park-Versuch selbst darf dabei scheitern
        # (z.B. dieselbe kaputte DB-Verbindung), ohne die Ausnahme erneut zu
        # werfen — sonst würde genau dieser Sicherheitsnetz-Pfad zur zweiten
        # Absturzursache.
        grund = f"Unerwarteter Fehler in der Pipeline (Task {task_id}): {exc}"
        log.error(f"Forge-Pipeline: {grund}")
        try:
            journal.log(task_id, "stage_failed", grund)
        except Exception:
            pass
        try:
            queue.park(task_id, current=state, reason=grund)
        except Exception:
            pass
        return "geparkt"


def _eine_stufe_intern(task: dict, task_id: int, state: str, worktree: Path) -> str:
    stufe, ist_fix = _waehle_stufe(state, worktree)
    if stufe is None:
        _park(task_id, state, f"Kein Stufen-Handler für Zustand '{state}' (Task {task_id})")
        return "geparkt"

    if ist_fix:
        runden = queue.zaehle_fixrunde(task_id)
        if runden > MAX_FIXRUNDEN:
            _park(task_id, state,
                  f"Fix-Runden-Grenze erreicht ({runden} > {MAX_FIXRUNDEN}), Task {task_id}")
            return "geparkt"
    elif state == m.REVIEWING:
        # Nur die eigentliche Review-Stufe braucht den Diff — sie hat kein
        # Bash. Die Fix-Stufe hat Bash und kommt notfalls selbst zurecht.
        diff_fehler = _schreibe_diff(worktree)
        if diff_fehler is not None:
            _park(task_id, state,
                  f"Diff-Artefakt ({stages.DIFF_DATEI}) nicht nutzbar: {diff_fehler} — "
                  f"Review würde ohne verlässliche Sicht auf die Änderung laufen (Task {task_id})")
            return "geparkt"

    journal.log(task_id, "stage_start", f"Stufe '{stufe.name}' für Task {task_id}")
    prompt = stufe.baue_prompt(task, _kontext(task))
    # Fund 2 (Akzeptanzlauf 2026-08-15): implement/fix bekommen ein größeres
    # Zeitbudget als die drei Lese-Stufen — siehe forge/stages.py.
    start = time.time()
    ergebnis = runner.run(prompt, cwd=worktree, profile=stufe.profile, timeout=stufe.timeout)

    # Fund 1 (Akzeptanzlauf 2026-08-15): ein Timeout ist nicht automatisch ein
    # Fehlschlag. Der Lauf kann sein Produkt bereits geliefert (implement/fix
    # bereits committet) haben, bevor er über die Zeitgrenze lief — das kostet
    # echtes Geld und darf nicht weggeworfen werden, nur weil der Prozess
    # danach noch weiterlief. rate_limited wird hier bewusst ausgeklammert:
    # dessen Sonderpfad (Zustand unverändert, kein Park) gilt unabhängig
    # davon, ob zufällig auch ein Produkt vorläge.
    zeitueberschreitung_pfad = None
    zeitueberschreitung_geliefert = False
    if not ergebnis.ok and not ergebnis.rate_limited and _ist_timeout(ergebnis):
        zeitueberschreitung_geliefert, zeitueberschreitung_pfad = _timeout_produkt(stufe, task, worktree, start)

    if ergebnis.ok:
        journal.log(
            task_id, "stage_done", (ergebnis.text or "")[:2000],
            tokens_in=ergebnis.tokens_in, tokens_out=ergebnis.tokens_out,
            cache_read=ergebnis.cache_read, cache_creation=ergebnis.cache_creation,
        )
    elif zeitueberschreitung_geliefert:
        hinweis = (f"Zeitüberschreitung ({ergebnis.error}), aber Produkt der Stufe '{stufe.name}' "
                    f"trotzdem vorhanden" + (f": {zeitueberschreitung_pfad}" if zeitueberschreitung_pfad
                                              else " (neuer Commit auf dem Branch)") + " — als Erfolg gewertet.")
        journal.log(
            task_id, "stage_done", hinweis,
            tokens_in=ergebnis.tokens_in, tokens_out=ergebnis.tokens_out,
            cache_read=ergebnis.cache_read, cache_creation=ergebnis.cache_creation,
        )
    else:
        journal.log(
            task_id, "stage_failed", (ergebnis.text or ergebnis.error or "")[:2000],
            tokens_in=ergebnis.tokens_in, tokens_out=ergebnis.tokens_out,
            cache_read=ergebnis.cache_read, cache_creation=ergebnis.cache_creation,
        )

    if ergebnis.rate_limited:
        # Zustand bewusst unverändert lassen — siehe Modul-Docstring.
        return "fehler"

    if ergebnis.denials:
        _park(task_id, state,
              f"Verweigerte Werkzeuge in Stufe '{stufe.name}': {_denial_namen(ergebnis.denials)} "
              f"(Task {task_id}) — Rechteprofil vermutlich zu eng")
        return "geparkt"

    if not ergebnis.ok and not zeitueberschreitung_geliefert:
        _park(task_id, state, f"Stufe '{stufe.name}' fehlgeschlagen: {ergebnis.error} (Task {task_id})")
        return "geparkt"

    try:
        if zeitueberschreitung_geliefert and stufe.name in _ARTEFAKT_FELD_JE_STUFE:
            # Der übliche Weg liest den Pfad aus der Modellantwort — bei einem
            # Timeout ist die Antwort leer. Der Fallback-Fund aus
            # _timeout_produkt tritt an ihre Stelle.
            erwartet, feld = zeitueberschreitung_pfad, _ARTEFAKT_FELD_JE_STUFE[stufe.name]
        else:
            erwartet, feld = _erwarteter_pfad_und_feld(stufe, task, ergebnis)
    except _PfadUngueltig as exc:
        _park(task_id, state,
              f"Von Stufe '{stufe.name}' gelieferter Pfad sieht auch nach Normalisierung nicht wie "
              f"ein Repo-relativer Pfad aus — roher Modelltext: {exc.roh!r} (Task {task_id})")
        return "geparkt"
    if erwartet is not None:
        if not _artefakt_vorhanden(worktree, erwartet):
            _park(task_id, state,
                  f"Erwartetes Artefakt fehlt: {erwartet} (Stufe '{stufe.name}', Task {task_id})")
            return "geparkt"
        if feld is not None:
            queue.setze_artefakt(task_id, feld, erwartet)
            # Akzeptanzlauf 2026-08-15, Fund 3: ohne diesen Commit sieht das
            # Gate (git diff main...HEAD) das Spec-/Plan-Dokument nie — es
            # bliebe für Gate und Review unsichtbar, obwohl die Stufe ihre
            # Aufgabe erfüllt hat.
            commit_fehler = _committe_artefakt(worktree, stufe.name, task_id, erwartet)
            if commit_fehler is not None:
                _park(task_id, state,
                      f"Artefakt-Commit fehlgeschlagen (Stufe '{stufe.name}'): {commit_fehler} "
                      f"(Task {task_id})")
                return "geparkt"
            journal.log(task_id, "stage_done",
                         f"Artefakt der Stufe '{stufe.name}' committet: {erwartet}")

    # Die Fix-Stufe teilt sich REVIEWING mit der Review-Stufe, aber ihr
    # next_state (GATING, siehe forge/stages.py) gilt hier NICHT: nach einem
    # Fix muss die Kette erneut durch die Review laufen, nicht direkt zum
    # Gate. models.can_transition erlaubt REVIEWING -> IMPLEMENTING genau
    # dafür.
    ziel = m.IMPLEMENTING if ist_fix else stufe.next_state
    if not queue.set_state(task_id, ziel, current=state):
        # Compare-and-Swap ist fehlgeschlagen (verbotener Übergang oder ein
        # anderer Schreiber war schneller) — der Task steckt tatsächlich noch
        # in `state`, und "weiter"/"fertig" zurückzugeben würde dem Aufrufer
        # einen Fortschritt vorgaukeln, der nie stattfand.
        _park(task_id, state,
              f"Zustandswechsel {state} -> {ziel} schlug fehl (Task {task_id})")
        return "geparkt"
    if ist_fix:
        # Kritischer Fund 1: das Vor-Fix-Urteil (und der Diff, den es beurteilt
        # hat) sind jetzt veraltet — ohne diesen Schnitt läse die nächste
        # REVIEWING-Stufe dasselbe Urteil erneut und würde nie wirklich neu
        # reviewen.
        _verwirf_review_artefakte(worktree)
    return "fertig" if ziel == m.GATING else "weiter"
