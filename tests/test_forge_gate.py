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


class TestPruefe:
    """Deckt pruefe() selbst ab: subprocess.run und gitctl.run werden gestubbt,
    damit hier nie die echte Suite oder echtes ruff läuft (das würde rekursieren)."""

    def _stub_sauberer_git(self, monkeypatch, dateien="core/foo.py\n", numstat="10\t2\tcore/foo.py\n"):
        def fake_gitctl_run(*args, cwd=None, timeout=300):
            if "--name-only" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=dateien, stderr="")
            if "--numstat" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=numstat, stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gate.gitctl, "run", fake_gitctl_run)

    def _stub_subprocess(self, monkeypatch, pytest_rc=0, pytest_stdout="1 passed", pytest_exc=None,
                          ruff_rc=0, ruff_stdout="", ruff_exc=None):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "python3.14":
                if pytest_exc is not None:
                    raise pytest_exc
                return subprocess.CompletedProcess(args=cmd, returncode=pytest_rc, stdout=pytest_stdout, stderr="")
            if cmd[0] == "ruff":
                if ruff_exc is not None:
                    raise ruff_exc
                return subprocess.CompletedProcess(args=cmd, returncode=ruff_rc, stdout=ruff_stdout, stderr="")
            raise AssertionError(f"unerwarteter subprocess-Aufruf: {cmd}")

        monkeypatch.setattr(subprocess, "run", fake_run)

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
        self._stub_sauberer_git(
            monkeypatch,
            dateien="forge/gate.py\ncore/foo.py\n",
            numstat="500\t0\tforge/gate.py\n400\t0\tcore/foo.py\n",
        )
        self._stub_subprocess(monkeypatch, pytest_rc=1, pytest_stdout="1 failed")
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is False
        assert len(ergebnis.gruende) == 3
        gruende_text = " ".join(ergebnis.gruende)
        assert "Sperrzone" in gruende_text
        assert "Diff zu groß" in gruende_text
        assert "Tests rot" in gruende_text

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
        self._stub_sauberer_git(monkeypatch, numstat=f"{gate.MAX_DIFF_ZEILEN}\t0\tcore/foo.py\n")
        self._stub_subprocess(monkeypatch)
        self._schreibe_pass_verdikt(tmp_path)
        ergebnis = gate.pruefe(tmp_path)
        assert ergebnis.ok is True
        assert not any("Diff zu groß" in g for g in ergebnis.gruende)

    def test_diff_ein_ueber_der_grenze_ist_nicht_ok(self, tmp_path, monkeypatch):
        self._stub_sauberer_git(monkeypatch, numstat=f"{gate.MAX_DIFF_ZEILEN + 1}\t0\tcore/foo.py\n")
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
