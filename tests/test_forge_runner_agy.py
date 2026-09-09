"""Tests für das agy-Backend (Antigravity, werkzeuglos)."""
import sys, os, json, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path

from forge import runner_agy as ra


def _fake(aufzeichnung, stdout, rc=0):
    def _run(cmd, **kwargs):
        aufzeichnung["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, rc, stdout, "")
    return _run


class TestDiffEinbettung:
    def test_diff_landet_im_prompt(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("--- a/x\n+++ b/x\n+neu\n")
        auf = {}
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake(auf, '{"ok": true, "befunde": []}'))
        ra.run("Beurteile das.", cwd=tmp_path, timeout=60,
               agent="review", model="claude-opus-4-6-thinking")
        gesendet = " ".join(auf["cmd"])
        assert "+neu" in gesendet

    def test_fehlender_diff_ist_ein_fehler_kein_leeres_review(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "diff.patch" in (r.error or "")


class TestVerdikt:
    def test_runner_schreibt_die_verdikt_datei(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("egal")
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, '{"ok": false, "befunde": ["zu lang"]}'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is True
        geschrieben = json.loads((tmp_path / ".forge" / "review.json").read_text())
        assert geschrieben["befunde"] == ["zu lang"]

    def test_json_im_codeblock_wird_erkannt(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("egal")
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run",
                            _fake({}, 'Hier mein Urteil:\n```json\n{"ok": true, "befunde": []}\n```\n'))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is True
        assert json.loads((tmp_path / ".forge" / "review.json").read_text())["ok"] is True

    def test_unparsebare_ausgabe_ist_ein_klarer_fehler(self, monkeypatch, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "diff.patch").write_text("egal")
        monkeypatch.setattr(ra, "AGY_BIN", "/usr/local/bin/agy")
        monkeypatch.setattr(subprocess, "run", _fake({}, "Ich habe keine Meinung."))
        r = ra.run("x", cwd=tmp_path, timeout=60, agent="review", model="m")
        assert r.ok is False
        assert "kein JSON" in (r.error or "")
        assert not (tmp_path / ".forge" / "review.json").exists()
