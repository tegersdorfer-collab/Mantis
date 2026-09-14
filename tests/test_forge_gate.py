"""Das Gate. Deterministisch, ohne LLM — es ist die einzige Instanz, die die
Forge nicht mit einem gut formulierten Argument überzeugen kann."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import subprocess
from pathlib import Path

from forge import gate


class TestSperrzonen:
    def test_env_ist_gesperrt(self):
        assert gate.beruehrt_sperrzone([".env"]) == [".env"]

    def test_data_verzeichnis_ist_gesperrt(self):
        assert gate.beruehrt_sperrzone(["data/gmail_token.json"])

    def test_gate_selbst_ist_gesperrt(self):
        # Sonst könnte ein Lauf seine eigene Schranke verschieben.
        assert gate.beruehrt_sperrzone(["forge/gate.py"])

    def test_budget_ist_gesperrt(self):
        assert gate.beruehrt_sperrzone(["forge/budget.py"])

    def test_launchagents_sind_gesperrt(self):
        assert gate.beruehrt_sperrzone(["Library/LaunchAgents/com.mantis.forge.plist"])

    def test_normale_datei_ist_frei(self):
        assert gate.beruehrt_sperrzone(["core/skills/spotify.py"]) == []

    def test_mehrere_treffer_werden_alle_gemeldet(self):
        treffer = gate.beruehrt_sperrzone([".env", "core/db.py", "data/x.json"])
        assert len(treffer) == 2

    def test_teiltreffer_im_namen_zaehlt_nicht(self):
        # "data/" ist gesperrt, "datastore.py" nicht — sonst sperrt das Gate
        # willkürlich harmlose Dateien.
        assert gate.beruehrt_sperrzone(["datastore.py"]) == []

    def test_pfad_mit_punkt_slash_praefix_matcht_trotzdem(self):
        # Ein Stufen-Diff könnte Pfade als "./forge/gate.py" ausgeben statt
        # normalisiert — die Sperrzone darf davon nicht ausgehebelt werden.
        assert gate.beruehrt_sperrzone(["./forge/gate.py"]) == ["./forge/gate.py"]

    def test_pfad_mit_fuehrendem_slash_matcht_trotzdem(self):
        assert gate.beruehrt_sperrzone(["/data/geheim.json"]) == ["/data/geheim.json"]


class TestVerdikt:
    def test_pass_wird_gelesen(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps({"verdict": "pass", "findings": []}))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is True and befunde == []

    def test_fail_wird_gelesen(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps(
            {"verdict": "fail", "findings": [{"severity": "important", "what": "x"}]}))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False and len(befunde) == 1

    def test_fehlende_datei_ist_kein_pass(self, tmp_path):
        # Der Review-Lauf könnte abgestürzt sein. Kein Urteil heißt nicht "gut".
        ok, _ = gate.lies_verdikt(tmp_path)
        assert ok is False

    def test_kaputtes_json_ist_kein_pass(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text("{kaputt")
        ok, _ = gate.lies_verdikt(tmp_path)
        assert ok is False

    def test_critical_befund_ueberstimmt_ein_pass(self, tmp_path):
        # Wenn der Agent sich selbst 'pass' gibt und trotzdem einen critical
        # Befund auflistet, gilt der Befund.
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps(
            {"verdict": "pass", "findings": [{"severity": "critical", "what": "boom"}]}))
        ok, _ = gate.lies_verdikt(tmp_path)
        assert ok is False

    def test_minor_befund_kippt_ein_pass_nicht(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps(
            {"verdict": "pass", "findings": [{"severity": "minor", "what": "stil"}]}))
        ok, _ = gate.lies_verdikt(tmp_path)
        assert ok is True

    def test_findings_als_liste_von_strings_crasht_nicht(self, tmp_path):
        # Ein Review-Agent könnte "findings" als Liste von Strings statt Dicts
        # schreiben (z.B. bei einem Formatfehler im Prompt-Gehorsam). lies_verdikt
        # darf daran nicht mit AttributeError sterben — b.get() auf einem str
        # existiert nicht.
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps(
            {"verdict": "pass", "findings": ["irgendwas ist komisch"]}))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is True and befunde == []

    def test_top_level_null_ist_kein_pass(self, tmp_path):
        # json.loads("null") ist gültiges JSON, aber daten.get(...) würde auf
        # None mit AttributeError sterben, wenn hier nicht abgefangen wird.
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text("null")
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False
        assert befunde and befunde[0]["severity"] == "critical"

    def test_top_level_liste_ist_kein_pass(self, tmp_path):
        # Genauso gültiges JSON: eine bloße Liste statt eines Objekts.
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps([1, 2, 3]))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False
        assert befunde and befunde[0]["severity"] == "critical"

    def test_top_level_string_ist_kein_pass(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps("pass"))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False
        assert befunde and befunde[0]["severity"] == "critical"

    def test_top_level_zahl_ist_kein_pass(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text("42")
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False
        assert befunde and befunde[0]["severity"] == "critical"

    def test_unlesbare_datei_ist_kein_pass(self, tmp_path, monkeypatch):
        # Datei existiert und ist gültiges JSON, aber das Lesen selbst schlägt
        # fehl (z.B. Rechteproblem) — muss denselben fail-closed-Pfad wie
        # kaputtes JSON nehmen, nicht die Exception durchreichen.
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps({"verdict": "pass"}))

        def kaputtes_lesen(self, *args, **kwargs):
            raise PermissionError("keine Leserechte")

        monkeypatch.setattr(Path, "read_text", kaputtes_lesen)
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False
        assert befunde and befunde[0]["severity"] == "critical"

    def test_fehlendes_findings_feld_gilt_als_leer(self, tmp_path):
        # "findings" fehlt komplett (kein leeres [], sondern gar kein Key) —
        # daten.get("findings") or [] fängt das ab.
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps({"verdict": "pass"}))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is True and befunde == []

    def test_findings_als_dict_statt_liste_ist_kein_pass(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps(
            {"verdict": "pass", "findings": {"severity": "critical"}}))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False
        assert befunde and befunde[0]["severity"] == "critical"

    def test_fehlendes_verdict_feld_ist_kein_pass(self, tmp_path):
        # Kein "verdict"-Key, keine harten Befunde — daten.get("verdict") == "pass"
        # ist False für None, nicht True durch irgendeine Sonderbehandlung.
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "review.json").write_text(json.dumps({"findings": []}))
        ok, befunde = gate.lies_verdikt(tmp_path)
        assert ok is False and befunde == []


class TestSchwellen:
    def test_diff_grenze_ist_achthundert(self):
        assert gate.MAX_DIFF_ZEILEN == 800


def _stub_sauberer_git(monkeypatch, dateien="tests/foo.py\n", numstat="10\t2\ttests/foo.py\n", roh=""):
    """gitctl.run nach Drehbuch: Dateiliste, numstat und --raw-Ausgabe."""
    def fake_gitctl_run(*args, cwd=None, timeout=300):
        if "--name-only" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=dateien, stderr="")
        if "--numstat" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=numstat, stderr="")
        if "--raw" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=roh, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate.gitctl, "run", fake_gitctl_run)


class TestPruefe:
    """Deckt pruefe() selbst ab: subprocess.run und gitctl.run werden gestubbt,
    damit hier nie die echte Suite oder echtes ruff läuft (das würde rekursieren)."""

    def _stub_sauberer_git(self, monkeypatch, dateien="tests/foo.py\n", numstat="10\t2\ttests/foo.py\n",
                           roh=""):
        _stub_sauberer_git(monkeypatch, dateien=dateien, numstat=numstat, roh=roh)

    def _stub_subprocess(self, monkeypatch, pytest_rc=0, pytest_stdout="1 passed", pytest_exc=None,
                          ruff_rc=0, ruff_stdout="", ruff_exc=None):
        """Stubbt subprocess.run und zeichnet jeden Aufruf als (argv, kwargs) auf.

        Verteilt nach dem Modul hinter `-m`, nicht nach argv[0]: seit dem
        Abschluss-Review 2c (C1) ruft das Gate pytest und ruff über
        `sys.executable -m …` auf. Ein nackter Binärname ("python3.14",
        "ruff") ist unter dem launchd-PATH nicht auflösbar (Journal
        2026-08-15) und gilt hier als unerwarteter Aufruf."""
        aufrufe: list[tuple[list, dict]] = []

        def fake_run(cmd, **kwargs):
            aufrufe.append((list(cmd), kwargs))
            if cmd[0] != sys.executable:
                raise AssertionError(f"Gate ruft nicht den eigenen Interpreter auf: {cmd}")
            modul = cmd[2] if len(cmd) > 2 and cmd[1] == "-m" else None
            if modul == "pytest":
                if pytest_exc is not None:
                    raise pytest_exc
                return subprocess.CompletedProcess(args=cmd, returncode=pytest_rc, stdout=pytest_stdout, stderr="")
            if modul == "ruff":
                if ruff_exc is not None:
                    raise ruff_exc
                return subprocess.CompletedProcess(args=cmd, returncode=ruff_rc, stdout=ruff_stdout, stderr="")
            raise AssertionError(f"unerwarteter subprocess-Aufruf: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return aufrufe

    def _schreibe_pass_verdikt(self, tmp_path):
        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".forge" / "review.json").write_text(json.dumps({"verdict": "pass", "findings": []}))

    def test_sauberer_durchlauf_ist_ok_ohne_gruende(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is True
        assert ergebnis.gruende == []

    def test_rote_tests_ergeben_ok_false_mit_passendem_grund(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(monkeypatch, pytest_rc=1, pytest_stdout="1 failed, 4 passed")
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("Tests rot" in g for g in ergebnis.gruende)

    def test_ruff_befund_ergibt_ok_false(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(monkeypatch, ruff_rc=1, ruff_stdout="core/foo.py:1:1: F401 unused import")
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("ruff" in g.lower() for g in ergebnis.gruende)

    def test_drei_gleichzeitige_verstoesse_ergeben_drei_gruende(self, tmp_path, monkeypatch):
        # Ein Gate, das nur den ersten Grund meldet, führt zu einer Fix-Runde,
        # die den Rest erst danach entdeckt — teuer bei jedem einzelnen Fund.
        # Bis zum Abschluss-Review 2c (I1) war der dritte Grund hier eine
        # Sperrzone; seitdem beendet ein Zonenverstoss das Gate VOR den Tests
        # (siehe TestZonenverstossKurzschluss). Drei Gründe, die zusammen
        # auftreten können, sind jetzt: zu grosser Diff, rote Tests, ruff.
        self._stub_sauberer_git(
            monkeypatch,
            dateien="tests/foo.py\n",
            numstat="900\t0\ttests/foo.py\n",
        )
        self._stub_subprocess(monkeypatch, pytest_rc=1, pytest_stdout="1 failed",
                              ruff_rc=1, ruff_stdout="tests/foo.py:1:1: F401 unused import")
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert len(ergebnis.gruende) == 3
        gruende_text = " ".join(ergebnis.gruende)
        assert "Diff zu groß" in gruende_text
        assert "Tests rot" in gruende_text
        assert "ruff" in gruende_text

    # --- Abschluss-Review 2c, C1: Aufruf über den eigenen Interpreter --------
    # Unter dem launchd-PATH (forge/launchd/com.mantis.forge.plist) sind weder
    # `python3.14` noch `ruff` auflösbar — das Journal vom 2026-08-15 zeigt
    # genau diesen Ausfall ("Tests rot: Aufruf fehlgeschlagen"). Der Daemon
    # selbst läuft aber, also existiert sein Interpreter: sys.executable.

    def test_pytest_und_ruff_laufen_ueber_den_eigenen_interpreter(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch)
        aufrufe = self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        assert gate.pruefe(tmp_path).ok is True
        argvs = [cmd for cmd, _ in aufrufe]
        assert [sys.executable, "-m", "pytest", "-q"] in argvs, argvs
        assert [sys.executable, "-m", "ruff", "check", "."] in argvs, argvs
        assert all(cmd[0] == sys.executable for cmd in argvs), \
            f"nackter Binärname im Gate-Aufruf: {argvs}"

    # --- Abschluss-Review 2c, I1: die Suite darf die Produktions-DB nicht sehen --
    # Das Gate lässt `pytest -q` über die GANZE Suite im Worktree eines Tasks
    # laufen — inklusive tests/, das der Agent selbst editieren darf. Ohne
    # Umleitung sähe jeder Test dort die echte Postgres (settings.cfg liest
    # DATABASE_URL aus Umgebung > .env > Default).

    def test_suite_laeuft_gegen_eine_unerreichbare_datenbank(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch)
        aufrufe = self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        gate.pruefe(tmp_path)
        pytest_aufruf = next(kw for cmd, kw in aufrufe if cmd[2] == "pytest")
        env = pytest_aufruf.get("env")
        assert env is not None, "pytest erbt die Umgebung des Daemons samt echter DATABASE_URL"
        assert env["DATABASE_URL"] == gate.GATE_DATABASE_URL
        assert ":1/" in env["DATABASE_URL"], "Port 1 — dort hört kein Postgres"
        # Alles andere aus der Umgebung bleibt erhalten (PATH, HOME, …).
        assert env.get("PATH") == os.environ.get("PATH")

    def test_umleitung_ueberschreibt_eine_gesetzte_datenbank_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost:5432/mantis")
        self._stub_sauberer_git(monkeypatch)
        aufrufe = self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        gate.pruefe(tmp_path)
        pytest_aufruf = next(kw for cmd, kw in aufrufe if cmd[2] == "pytest")
        assert pytest_aufruf["env"]["DATABASE_URL"] != "postgresql://localhost:5432/mantis"

    def test_timeout_der_test_suite_wird_zu_grund_nicht_zu_exception(self, tmp_path, monkeypatch):
        # Das Wichtigste an diesem Modul: pruefe() läuft im Daemon-Tick. Eine
        # durchschlagende TimeoutExpired würde den ganzen Tick mitreißen.
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(
            monkeypatch,
            pytest_exc=subprocess.TimeoutExpired(cmd=["python3.14", "-m", "pytest", "-q"], timeout=1800),
        )
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)   # darf nicht werfen
        assert ergebnis.ok is False
        assert any("Tests rot" in g for g in ergebnis.gruende)

    def test_timeout_von_ruff_wird_zu_grund_nicht_zu_exception(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(
            monkeypatch,
            ruff_exc=subprocess.TimeoutExpired(cmd=["ruff", "check", "."], timeout=300),
        )
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("ruff" in g.lower() for g in ergebnis.gruende)

    def test_fehlendes_pytest_binary_wird_zu_grund_nicht_zu_exception(self, tmp_path, monkeypatch):
        # Unter launchd ist PATH nicht die interaktive Shell — python3.14 oder
        # ruff können fehlen. Genau das hat dieses Projekt in Plan 1 schon
        # einmal einen Critical gekostet (stiller Crash statt Gate-Ablehnung).
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(monkeypatch, pytest_exc=FileNotFoundError("python3.14 nicht gefunden"))
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("Tests rot" in g for g in ergebnis.gruende)

    def test_fehlendes_ruff_binary_wird_zu_grund_nicht_zu_exception(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(monkeypatch, ruff_exc=FileNotFoundError("ruff nicht gefunden"))
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("ruff" in g.lower() for g in ergebnis.gruende)

    def test_pruefe_wirft_nicht_bei_nicht_objekt_verdikt(self, tmp_path, monkeypatch):
        # Reproduktion des Critical-Befunds: ein review.json mit "null" statt
        # eines Objekts darf pruefe() nicht mit AttributeError zum Absturz
        # bringen (das würde den Daemon-Tick mitreißen), sondern muss fail-closed
        # als Gate-Grund landen.
        self._stub_sauberer_git(monkeypatch)
        self._stub_subprocess(monkeypatch)
        (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".forge" / "review.json").write_text("null")
        ergebnis = gate.pruefe(tmp_path)  # darf nicht werfen
        assert ergebnis.ok is False
        assert any("Review-Verdikt negativ" in g for g in ergebnis.gruende)

    def test_diff_genau_an_der_grenze_ist_ok(self, tmp_path, monkeypatch):
        # Grenzwerttest: exakt MAX_DIFF_ZEILEN darf noch durchgehen (">" nicht
        # ">="). Ohne diesen Test würde ein künftiges Vertippen von ">" zu ">="
        # unbemerkt bleiben.
        self._stub_sauberer_git(monkeypatch, numstat=f"{gate.MAX_DIFF_ZEILEN}\t0\ttests/foo.py\n")
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is True
        assert not any("Diff zu groß" in g for g in ergebnis.gruende)

    def test_diff_ein_ueber_der_grenze_ist_nicht_ok(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch, numstat=f"{gate.MAX_DIFF_ZEILEN + 1}\t0\ttests/foo.py\n")
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("Diff zu groß" in g for g in ergebnis.gruende)

    def test_binaere_datei_ergibt_eigenen_gate_grund(self, tmp_path, monkeypatch):
        # git diff --numstat meldet binäre Dateien als "-\t-\t<pfad>". Ohne
        # explizite Behandlung zählt das als 0 Zeilen und könnte einen großen
        # Blob unbemerkt an MAX_DIFF_ZEILEN vorbeischleusen.
        self._stub_sauberer_git(monkeypatch, numstat="-\t-\tassets/logo.png\n")
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        gruende_text = " ".join(ergebnis.gruende)
        assert "binär" in gruende_text.lower()
        assert "assets/logo.png" in gruende_text

    def test_binaere_datei_zaehlt_nicht_faelschlich_als_diff_groesse(self, tmp_path, monkeypatch):
        # Die binäre Zeile darf nicht in die Zeilensumme einfließen (0 statt
        # einer geratenen Zahl) — der eigene Grund ist die richtige Meldung,
        # nicht ein aufgeblähter "Diff zu groß"-Text.
        self._stub_sauberer_git(monkeypatch, numstat="-\t-\tassets/logo.png\n")
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert not any("Diff zu groß" in g for g in ergebnis.gruende)

    # --- I4: die Erlaubnisliste, geprüft DURCH pruefe() -------------------
    # Bis zum Abschluss-Review (2026-09-10) prüfte nur TestErlaubteZonen die
    # reine Funktion. Ob pruefe() sie überhaupt aufruft, sagte kein Test: der
    # Reviewer hat die drei Verdrahtungszeilen entfernt und die volle Suite
    # blieb grün. Eine Prüfung, die nicht verdrahtet ist, ist keine Prüfung.

    def test_datei_ausserhalb_der_erlaubten_zonen_ergibt_gate_grund(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch, dateien="core/db.py\n",
                                numstat="10\t2\tcore/db.py\n")
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("ausserhalb der erlaubten Zonen" in g and "core/db.py" in g
                   for g in ergebnis.gruende), \
            f"pruefe() ruft die Erlaubnisliste nicht auf: {ergebnis.gruende}"

    def test_beide_listen_laufen_im_selben_aufruf_gegen_dieselben_dateien(self, tmp_path, monkeypatch):
        """Sperrzonen UND Erlaubnisliste, ein Aufruf, eine Dateiliste.

        `scripts/fix_bluetooth.sh` liegt INNERHALB der erlaubten Zone
        "scripts/" und steht trotzdem in SPERRZONEN; `core/db.py` ist genau
        umgekehrt gelagert. Beide Gründe müssen aus demselben pruefe()-Aufruf
        kommen, und jeder muss seine eigene Datei nennen — ein Gate, das nur
        eine der beiden Listen verdrahtet hat, fällt hier durch."""
        self._stub_sauberer_git(
            monkeypatch,
            dateien="scripts/fix_bluetooth.sh\ncore/db.py\n",
            numstat="1\t0\tscripts/fix_bluetooth.sh\n1\t0\tcore/db.py\n",
        )
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        sperr = [g for g in ergebnis.gruende if "Sperrzone" in g]
        zonen = [g for g in ergebnis.gruende if "ausserhalb der erlaubten Zonen" in g]
        assert sperr and "scripts/fix_bluetooth.sh" in sperr[0], ergebnis.gruende
        assert "core/db.py" not in sperr[0], f"Sperrzonen-Grund nennt die falsche Datei: {sperr[0]}"
        assert zonen and "core/db.py" in zonen[0], ergebnis.gruende
        assert "fix_bluetooth" not in zonen[0], f"Zonen-Grund nennt die falsche Datei: {zonen[0]}"

    # --- I6: Symlinks -----------------------------------------------------

    def test_symlink_im_diff_ergibt_eigenen_gate_grund(self, tmp_path, monkeypatch):
        """Ein Symlink besteht beide Pfadlisten und zeigt trotzdem, wohin er will."""
        self._stub_sauberer_git(
            monkeypatch,
            dateien="tests/link\n",
            numstat="1\t0\ttests/link\n",
            roh=":000000 120000 0000000 7637430 A\ttests/link\n",
        )
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("Symlink" in g and "tests/link" in g for g in ergebnis.gruende), \
            f"kein Symlink-Grund: {ergebnis.gruende}"

    def test_gewoehnliche_datei_ergibt_keinen_symlink_grund(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(
            monkeypatch,
            roh=":000000 100644 0000000 7898192 A\ttests/foo.py\n",
        )
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.gruende == []


class TestZonenverstossKurzschluss:
    """Abschluss-Review 2c, I1: ein Diff, der eine Sperrzone berührt oder die
    erlaubten Zonen verlässt, ist durch nichts mergefähig, was die Tests
    beweisen könnten. Bis zu 30 Minuten pytest für einen ohnehin verlorenen
    Task sind dann reine Verschwendung — und jeder dieser Läufe führt
    agentengeschriebene Tests aus."""

    def _stub_git(self, monkeypatch, dateien, numstat):
        _stub_sauberer_git(monkeypatch, dateien=dateien, numstat=numstat)

    def _kein_subprocess(self, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kwargs):
            aufrufe.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return aufrufe

    def test_datei_ausserhalb_der_zonen_erspart_tests_und_ruff(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch, dateien="core/db.py\n", numstat="1\t0\tcore/db.py\n")
        aufrufe = self._kein_subprocess(monkeypatch)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("ausserhalb der erlaubten Zonen" in g for g in ergebnis.gruende)
        assert aufrufe == [], f"Tests/ruff liefen trotz Zonenverstoss: {aufrufe}"

    def test_sperrzone_erspart_tests_und_ruff(self, tmp_path, monkeypatch):
        self._stub_git(monkeypatch, dateien="scripts/fix_bluetooth.sh\n",
                       numstat="1\t0\tscripts/fix_bluetooth.sh\n")
        aufrufe = self._kein_subprocess(monkeypatch)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert any("Sperrzone" in g for g in ergebnis.gruende)
        assert aufrufe == []

    def test_kurzschluss_sammelt_die_billigen_gruende_trotzdem(self, tmp_path, monkeypatch):
        # Der Kurzschluss spart nur die teuren Läufe. Diff-Grösse und Verdikt
        # kosten nichts und gehören weiter in den Bericht — sonst findet die
        # nächste Runde sie erst nach dem Zonen-Fix.
        self._stub_git(monkeypatch, dateien="core/db.py\n",
                       numstat=f"{gate.MAX_DIFF_ZEILEN + 1}\t0\tcore/db.py\n")
        self._kein_subprocess(monkeypatch)
        ergebnis = gate.pruefe(tmp_path)   # kein Verdikt geschrieben
        gruende_text = " ".join(ergebnis.gruende)
        assert "ausserhalb der erlaubten Zonen" in gruende_text
        assert "Diff zu groß" in gruende_text
        assert "Review-Verdikt negativ" in gruende_text

    def test_sauberer_diff_laesst_tests_und_ruff_laufen(self, tmp_path, monkeypatch):
        # Gegenprobe: ohne Zonenverstoss laufen beide teuren Prüfungen.
        self._stub_git(monkeypatch, dateien="tests/foo.py\n", numstat="1\t0\ttests/foo.py\n")
        aufrufe = self._kein_subprocess(monkeypatch)
        gate.pruefe(tmp_path)
        module = [cmd[2] for cmd in aufrufe if len(cmd) > 2]
        assert module == ["pytest", "ruff"]


class TestGegenEchtesGit:
    """I5/I6, gemessen statt geglaubt. Alle anderen Tests dieses Moduls stubben
    gitctl — genau darin konnten sich Rename-Blindheit und Symlinks verstecken.
    Hier läuft echtes git in einem Wegwerf-Repo unter tmp_path."""

    @staticmethod
    def _repo(tmp_path):
        ort = tmp_path / "repo"
        ort.mkdir()

        def g(*args):
            subprocess.run(["git", *args], cwd=str(ort), check=True, capture_output=True)

        g("init", "-b", "main")
        g("config", "user.email", "test@example.com")
        g("config", "user.name", "Test")
        (ort / "core").mkdir()
        (ort / "core" / "db.py").write_text("x\ny\nz\n")
        # Muss im ersten Commit liegen, also in `main`: `git diff main...HEAD`
        # sieht nur, was der Branch gegenüber main ändert. Eine Datei, die es
        # in main nie gab, kann der Branch auch nicht aus einer Sperrzone
        # heraustragen.
        (ort / "data").mkdir()
        (ort / "data" / "geheim.txt").write_text("wichtig\n")
        g("add", "-A")
        g("commit", "-m", "erster Commit")
        g("checkout", "-b", "forge/task-1")
        return ort, g

    def test_umbenennung_zeigt_dem_gate_auch_den_quellpfad(self, tmp_path):
        """core/db.py -> tools/db.py: ohne --no-renames sieht das Gate nur das
        Ziel in der erlaubten Zone, und der Kern-Pfad taucht nirgends auf."""
        ort, g = self._repo(tmp_path)
        (ort / "tools").mkdir()
        g("mv", "core/db.py", "tools/db.py")
        g("add", "-A")
        g("commit", "-m", "verschoben")

        dateien = gate._geaenderte_dateien(ort, "main")
        assert "core/db.py" in dateien, \
            f"Quellpfad der Umbenennung fehlt — Gate ist rename-blind: {dateien}"
        assert "tools/db.py" in dateien
        assert gate.ausserhalb_erlaubter_zonen(dateien) == ["core/db.py"]

    def test_umbenennung_aus_der_sperrzone_wird_gesehen(self, tmp_path):
        """Der eigentliche Angriff: eine Sperrzonen-Datei unsichtbar heraustragen."""
        ort, g = self._repo(tmp_path)
        (ort / "tools").mkdir()
        g("mv", "data/geheim.txt", "tools/geheim.txt")
        g("add", "-A")
        g("commit", "-m", "raus damit")

        dateien = gate._geaenderte_dateien(ort, "main")
        assert gate.beruehrt_sperrzone(dateien) == ["data/geheim.txt"], \
            f"Sperrzonen-Quelle der Umbenennung nicht gesehen: {dateien}"

    def test_umbenennung_zaehlt_ihre_zeilen(self, tmp_path):
        """Mit Rename-Erkennung meldet --numstat 0 Zeilen — damit liesse sich
        MAX_DIFF_ZEILEN mit beliebig viel verschobenem Code umgehen."""
        ort, g = self._repo(tmp_path)
        (ort / "tools").mkdir()
        g("mv", "core/db.py", "tools/db.py")
        g("add", "-A")
        g("commit", "-m", "verschoben")

        groesse, binaere = gate._diff_groesse(ort, "main")
        assert groesse == 6, f"Umbenennung zählt {groesse} statt 3 gelöschte + 3 neue Zeilen"
        assert binaere == []

    def test_symlink_wird_als_symlink_erkannt(self, tmp_path):
        ort, g = self._repo(tmp_path)
        os.symlink("../core/db.py", ort / "tests_link")
        g("add", "-A")
        g("commit", "-m", "link")

        # Der Pfad selbst ist unauffällig: keine Sperrzone, und er läge in
        # einer erlaubten Zone. Nur der Dateimodus verrät ihn.
        assert gate._symlinks_im_diff(ort, "main") == ["tests_link"]

    def test_geloeschter_symlink_ist_kein_befund(self, tmp_path):
        """Zielmodus 000000 — der Link ist weg, es gibt nichts abzulehnen."""
        ort, g = self._repo(tmp_path)
        os.symlink("../core/db.py", ort / "tests_link")
        g("add", "-A")
        g("commit", "-m", "link")
        g("rm", "tests_link")
        g("commit", "-m", "link weg")

        assert gate._symlinks_im_diff(ort, "main") == []

    def test_gewoehnliche_aenderung_meldet_keinen_symlink(self, tmp_path):
        ort, g = self._repo(tmp_path)
        (ort / "core" / "db.py").write_text("x\ny\nz\nneu\n")
        g("add", "-A")
        g("commit", "-m", "geaendert")

        assert gate._symlinks_im_diff(ort, "main") == []


from forge.gate import ausserhalb_erlaubter_zonen


class TestErlaubteZonen:
    def test_erlaubte_pfade_gehen_durch(self):
        erlaubt = [
            "tests/test_neu.py",
            "docs/superpowers/specs/2026-01-01-x-design.md",
            "scripts/hilfe.sh",
            "tools/spotify/x.py",
            "core/skills/neu.py",
        ]
        assert ausserhalb_erlaubter_zonen(erlaubt) == []

    def test_kern_und_web_sind_draussen(self):
        assert ausserhalb_erlaubter_zonen(["core/db.py"]) == ["core/db.py"]
        assert ausserhalb_erlaubter_zonen(["web/api.py"]) == ["web/api.py"]

    def test_forge_darf_sich_nicht_selbst_umbauen(self):
        assert ausserhalb_erlaubter_zonen(["forge/pipeline.py"]) == ["forge/pipeline.py"]

    def test_core_skills_ist_die_ausnahme_in_core(self):
        gemischt = ["core/skills/gut.py", "core/agent.py"]
        assert ausserhalb_erlaubter_zonen(gemischt) == ["core/agent.py"]

    def test_praefix_matcht_nicht_ueber_die_verzeichnisgrenze(self):
        """'tests/' darf nicht 'testsuite.py' im Wurzelverzeichnis erlauben."""
        assert ausserhalb_erlaubter_zonen(["testsuite.py"]) == ["testsuite.py"]
        assert ausserhalb_erlaubter_zonen(["core/skillsammlung.py"]) == ["core/skillsammlung.py"]

    def test_fuehrendes_punktslash_wird_normalisiert(self):
        assert ausserhalb_erlaubter_zonen(["./tests/x.py"]) == []

    def test_wurzeldateien_sind_draussen(self):
        assert ausserhalb_erlaubter_zonen(["main.py", "README.md"]) == ["main.py", "README.md"]
