"""Tests für das agy-Backend (Antigravity, Review-Stufe)."""
import sys, os, json, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import gate
from forge import runner_agy as ra


def _fake(aufzeichnung, stdout, rc=0, stderr=""):
    def _run(cmd, **kwargs):
        aufzeichnung["cmd"] = cmd
        aufzeichnung["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    return _run


def _mit_diff(tmp_path, inhalt="egal"):
    (tmp_path / ".forge").mkdir(exist_ok=True)
    (tmp_path / ".forge" / "diff.patch").write_text(inhalt)
    return tmp_path


class TestDiffEinbettung:
    def test_diff_landet_im_prompt(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path, "--- a/x\n+++ b/x\n+neu\n")
        auf = {}
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(auf, '{"verdict": "pass", "findings": []}'))
        ra.run("Beurteile das.", cwd=tmp_path, timeout=60,
               agent="review", model="claude-opus-4-6-thinking")
        gesendet = " ".join(auf["cmd"])
        assert "+neu" in gesendet

    def test_fehlender_diff_ist_ein_fehler_kein_leeres_review(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "diff.patch" in (r.error or "")


class TestArbeitsverzeichnis:
    """I1 (Abschluss-Review): agy kennt kein --dir. Ohne cwd wäre der
    Arbeitsbereich das Verzeichnis des Daemon-Prozesses — der echte
    Haupt-Checkout, nicht der Worktree des Tasks."""

    def test_lauf_bekommt_den_worktree_als_cwd(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        auf = {}
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(auf, '{"verdict": "pass", "findings": []}'))
        ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert auf["kwargs"]["cwd"] == str(tmp_path)


class TestVerdikt:
    def test_runner_schreibt_die_verdikt_datei(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(
            {}, '{"verdict": "fail", "findings": [{"severity": "critical", "file": "a.py", "what": "zu lang"}]}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is True
        geschrieben = json.loads((tmp_path / ".forge" / "review.json").read_text())
        assert geschrieben["verdict"] == "fail"
        assert geschrieben["findings"][0]["severity"] == "critical"

    def test_json_im_codeblock_wird_erkannt(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(
            {}, 'Hier mein Urteil:\n```json\n{"verdict": "pass", "findings": []}\n```\n'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is True
        assert json.loads((tmp_path / ".forge" / "review.json").read_text())["verdict"] == "pass"

    def test_unparsebare_ausgabe_ist_ein_klarer_fehler(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, "Ich habe keine Meinung."))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "kein JSON" in (r.error or "")
        assert not (tmp_path / ".forge" / "review.json").exists()

    def test_fremde_schluessel_werden_nicht_mitgeschrieben(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(
            {}, '{"verdict": "pass", "findings": [], "ok": true, "kommentar": "toll"}'))
        ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        geschrieben = json.loads((tmp_path / ".forge" / "review.json").read_text())
        assert set(geschrieben) == {"verdict", "findings"}


class TestSchemaValidierung:
    """C1 (Abschluss-Review): der Runner schrieb {"ok", "befunde"}, das Gate
    liest {"verdict", "findings"} — jedes saubere Review wurde damit zu einem
    roten Gate. Ein Verdikt, das das Gate nicht lesen kann, darf gar nicht
    erst geschrieben werden."""

    def test_fehlendes_verdict_feld_ist_ein_runner_fehler(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"ok": true, "befunde": []}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "verdict" in (r.error or "")
        assert not (tmp_path / ".forge" / "review.json").exists()

    def test_nicht_string_verdict_ist_ein_runner_fehler(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"verdict": true, "findings": []}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert not (tmp_path / ".forge" / "review.json").exists()

    def test_unbekannter_verdict_wert_ist_ein_runner_fehler(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"verdict": "vielleicht", "findings": []}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert not (tmp_path / ".forge" / "review.json").exists()

    def test_findings_muss_eine_liste_sein(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"verdict": "pass", "findings": "keine"}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "findings" in (r.error or "")
        assert not (tmp_path / ".forge" / "review.json").exists()

    def test_grossgeschriebenes_urteil_wird_normalisiert(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"verdict": "PASS", "findings": []}'))
        ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert json.loads((tmp_path / ".forge" / "review.json").read_text())["verdict"] == "pass"

    def test_fehlendes_findings_feld_gilt_als_leer(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"verdict": "pass"}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is True
        assert json.loads((tmp_path / ".forge" / "review.json").read_text())["findings"] == []


class TestGateRundlauf:
    """Genau der Test, dessen Fehlen C1 hat durchgehen lassen: was der Runner
    schreibt, muss forge.gate.lies_verdikt auch so lesen können."""

    def test_sauberes_review_kommt_im_gate_als_bestanden_an(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"verdict": "pass", "findings": []}'))
        assert ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m").ok is True

        bestanden, befunde = gate.lies_verdikt(tmp_path)
        assert bestanden is True
        assert befunde == []

    def test_harter_befund_kommt_im_gate_als_durchgefallen_an(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(
            {}, '{"verdict": "fail", "findings": [{"severity": "important", "file": "a.py", '
                '"what": "Testlücke"}]}'))
        ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")

        bestanden, befunde = gate.lies_verdikt(tmp_path)
        assert bestanden is False
        assert befunde[0]["what"] == "Testlücke"


class TestKontingent:
    """I2 (Abschluss-Review): eine erschöpfte Gratis-Quote darf den Task nicht
    parken. forge/pipeline.py hat dafür den rate_limited-Pfad — er war von
    diesem Backend aus schlicht nie erreichbar."""

    def test_kontingentgrenze_setzt_das_flag(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, "Error: usage limit reached", rc=1))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert r.rate_limited is True

    def test_kontingentgrenze_auf_stderr_zaehlt_auch(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run",
                            _fake({}, "", rc=1, stderr="rate limit exceeded, try again later"))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert r.rate_limited is True

    def test_gewoehnlicher_fehler_setzt_das_flag_nicht(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, "Ich habe keine Meinung."))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.rate_limited is False


class TestFehlermodi:
    def test_fehlendes_binary_ist_kein_absturz(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ra, "AGY_BIN", None)
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "agy" in (r.error or "")

    def test_zeitueberschreitung_wird_als_fehler_gemeldet(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)

        def _timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 60)
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _timeout)
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "Zeitüberschreitung" in (r.error or "")

    def test_oserror_wird_als_fehler_gemeldet(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)

        def _oserror(cmd, **kwargs):
            raise OSError("Permission denied")
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _oserror)
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "Permission denied" in (r.error or "")


class TestPrintTimeout:
    """Erste Nacht (2026-09-14): agy hat einen eigenen `--print-timeout`,
    Default 5m0s. Ohne den Parameter bräche agy ein Opus-Thinking-Review nach
    fünf Minuten selbst ab, obwohl die Stufe 30 Minuten hätte — die Pipeline
    sähe einen leeren Lauf ohne Verdikt und parkte."""

    def test_stufen_timeout_wird_an_agy_durchgereicht(self, monkeypatch, tmp_path):
        _mit_diff(tmp_path)
        auf = {}
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(auf, '{"verdict": "pass", "findings": []}'))
        ra.run("x", cwd=tmp_path, timeout=1800, agent="review", model="m")
        cmd = auf["cmd"]
        assert "--print-timeout" in cmd
        assert cmd[cmd.index("--print-timeout") + 1] == "1800s"
