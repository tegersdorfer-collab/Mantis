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
import logging
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


def _schreibe_diff(worktree: Path) -> bool:
    """Befüllt stages.DIFF_DATEI mit dem Diff des Branches gegen seine Basis.

    Die Review-Stufe hat kein Bash und kann sich keinen eigenen Diff
    erzeugen — ohne diese Datei prüfte sie eine Spec ohne jede Sicht auf die
    tatsächliche Änderung. Ein fehlgeschlagener `git diff` (z.B. kein
    passender Merge-Base) wird wie in forge/gate.py nachsichtig behandelt und
    ergibt einen leeren, aber vorhandenen Diff — erst ein tatsächlicher
    Schreibfehler (Berechtigung, Platte voll, kaputter Pfad) parkt den Task,
    denn NUR dann fehlt das Artefakt wirklich statt nur leer zu sein.
    """
    worktree = Path(worktree)
    ergebnis = gitctl.run("diff", f"{BASIS_BRANCH}...HEAD", cwd=worktree)
    ziel = worktree / stages.DIFF_DATEI
    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_text(ergebnis.stdout or "")
    except OSError as exc:
        log.warning(f"Forge-Pipeline: Diff-Artefakt konnte nicht geschrieben werden: {exc}")
        return False
    return True


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


def _erwarteter_pfad_und_feld(stufe: stages.Stage, task: dict,
                               ergebnis: runner.RunResult) -> tuple[str | None, str | None]:
    """Der Artefakt-Pfad, den diese Stufe versprochen hat, plus das Feld, in
    dem er (falls zutreffend) in der Queue landet.

    spec und plan lesen ihren Pfad noch nicht aus dem Task — der wird ja
    gerade erst in DIESEM Lauf bekannt (die Antwort des Modells). Deshalb wird
    der Task hier lokal um das frisch gelieferte Feld angereichert, bevor
    stufe.artefakt() aufgerufen wird; review und implement/fix ignorieren den
    Task ohnehin (konstantes Ziel bzw. gar keins).
    """
    feld = _ARTEFAKT_FELD_JE_STUFE.get(stufe.name)
    if feld is None:
        return stufe.artefakt(task), None
    kandidat = _letzte_zeile(ergebnis.text)
    angereichert = {**task, feld: kandidat}
    return stufe.artefakt(angereichert), feld


def _letzte_zeile(text: str) -> str:
    """Die Prompts von spec und plan verlangen 'am Ende nur den Pfad' — das
    Modell kann trotzdem Freitext davor setzen (z.B. eine 'Annahme:'-Zeile,
    die derselbe Prompt ausdrücklich erlaubt). Die letzte nicht-leere Zeile
    ist robuster als der gesamte Text."""
    zeilen = [z.strip() for z in (text or "").splitlines() if z.strip()]
    return zeilen[-1] if zeilen else ""


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
        if not _schreibe_diff(worktree):
            _park(task_id, state,
                  f"Diff-Artefakt ({stages.DIFF_DATEI}) konnte nicht geschrieben werden — "
                  f"Review würde ohne Sicht auf die Änderung laufen (Task {task_id})")
            return "geparkt"

    journal.log(task_id, "stage_start", f"Stufe '{stufe.name}' für Task {task_id}")
    prompt = stufe.baue_prompt(task, _kontext(task))
    ergebnis = runner.run(prompt, cwd=worktree, profile=stufe.profile)

    journal.log(
        task_id,
        "stage_done" if ergebnis.ok else "stage_failed",
        (ergebnis.text or ergebnis.error or "")[:2000],
        tokens_in=ergebnis.tokens_in,
        tokens_out=ergebnis.tokens_out,
    )

    if ergebnis.rate_limited:
        # Zustand bewusst unverändert lassen — siehe Modul-Docstring.
        return "fehler"

    if ergebnis.denials:
        _park(task_id, state,
              f"Verweigerte Werkzeuge in Stufe '{stufe.name}': {_denial_namen(ergebnis.denials)} "
              f"(Task {task_id}) — Rechteprofil vermutlich zu eng")
        return "geparkt"

    if not ergebnis.ok:
        _park(task_id, state, f"Stufe '{stufe.name}' fehlgeschlagen: {ergebnis.error} (Task {task_id})")
        return "geparkt"

    erwartet, feld = _erwarteter_pfad_und_feld(stufe, task, ergebnis)
    if erwartet is not None:
        if not _artefakt_vorhanden(worktree, erwartet):
            _park(task_id, state,
                  f"Erwartetes Artefakt fehlt: {erwartet} (Stufe '{stufe.name}', Task {task_id})")
            return "geparkt"
        if feld is not None:
            queue.setze_artefakt(task_id, feld, erwartet)

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
