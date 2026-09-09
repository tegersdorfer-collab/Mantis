"""Tests für das opencode-Backend."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pathlib
import json
import subprocess
from pathlib import Path

from forge import runner_opencode as ro
from forge.runner_opencode import baue_config, parse_events

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Ein vollständig durchgelaufener opencode-Lauf endet mit einem step_finish,
# dessen reason "stop" ist — belegt in fixtures/opencode_stream_success.jsonl.
# Tests, die den Aufruf selbst prüfen (nicht den Abbruchfall), brauchen diesen
# Abschluss, sonst wertet parse_events den Strom zu Recht als abgeschnitten.
_ABSCHLUSS = ('{"type":"text","part":{"text":"ok"}}\n'
              '{"type":"step_finish","part":{"reason":"stop"}}')


class TestConfig:
    def test_agent_und_modell_stehen_drin(self):
        c = baue_config("implement", "nvidia/minimaxai/minimax-m3")
        assert c["agent"]["implement"]["model"] == "nvidia/minimaxai/minimax-m3"
        assert c["model"] == "nvidia/minimaxai/minimax-m3"

    def test_rechte_kommen_aus_der_schranke(self):
        c = baue_config("spec", "google/gemini-3.6-flash")
        rechte = c["agent"]["spec"]["permission"]
        assert rechte["bash"] == "deny"
        assert rechte["task"] == "deny"
        assert rechte["external_directory"] == "deny"
        assert rechte["edit"] == "allow"

    def test_schluessel_kommen_aus_der_umgebung_nicht_im_klartext(self):
        """Keys stehen als {env:...}-Verweis drin, nie als Wert."""
        c = baue_config("plan", "nvidia/moonshotai/kimi-k3")
        for provider in c["provider"].values():
            assert provider["options"]["apiKey"].startswith("{env:")

    def test_alle_genutzten_provider_sind_konfiguriert(self):
        c = baue_config("spec", "google/gemini-3.6-flash")
        assert set(c["provider"]) == {"nvidia", "google", "groq", "mistral"}


class TestEreignisse:
    def test_erfolgreicher_lauf(self):
        zeilen = (FIXTURES / "opencode_stream_success.jsonl").read_text().splitlines()
        r = parse_events(zeilen)
        assert r.ok is True
        assert r.error is None
        assert r.denials == []

    def test_tokens_werden_ueber_alle_schritte_summiert(self):
        """Die Aufnahme hat zwei step_finish: 4215+4397 ein, 74+6 aus."""
        zeilen = (FIXTURES / "opencode_stream_success.jsonl").read_text().splitlines()
        r = parse_events(zeilen)
        assert r.tokens_in == 8612
        assert r.tokens_out == 80

    def test_abgelehntes_werkzeug_wird_erkannt(self):
        zeilen = (FIXTURES / "opencode_stream_denied.jsonl").read_text().splitlines()
        r = parse_events(zeilen)
        assert r.ok is False
        assert len(r.denials) == 1
        assert "request.tools" in r.denials[0]["message"]

    def test_nicht_json_zeilen_kippen_den_lauf_nicht(self):
        zeilen = ["kein json", '{"type":"text","part":{"text":"hi"}}', ""]
        r = parse_events(zeilen)
        assert r.ok is True

    def test_leerer_strom_ist_kein_erfolg(self):
        r = parse_events([])
        assert r.ok is False
        assert "keine Ereignisse" in (r.error or "")

    def test_cache_wird_ueber_alle_schritte_summiert(self):
        """Cache-Lese- und Schreibzähler werden über mehrere step_finish-Ereignisse summiert.

        Mit zwei step_finish-Ereignissen:
        - Ereignis 1: cache.read=100, cache.write=50
        - Ereignis 2: cache.read=200, cache.write=75
        Erwartet: cache_read=300, cache_creation=125
        """
        zeilen = [
            '{"type":"step_start"}',
            '{"type":"step_finish","part":{"tokens":{"input":1000,"output":100,"cache":{"read":100,"write":50}}}}',
            '{"type":"step_start"}',
            '{"type":"step_finish","part":{"tokens":{"input":2000,"output":200,"cache":{"read":200,"write":75}}}}',
        ]
        r = parse_events(zeilen)
        assert r.ok is True
        assert r.cache_read == 300
        assert r.cache_creation == 125


class TestAufruf:
    def _fake_run(self, aufzeichnung, stdout="", rc=0):
        def _run(cmd, **kwargs):
            aufzeichnung["cmd"] = cmd
            aufzeichnung["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, rc, stdout, "")
        return _run

    def test_kommando_setzt_agent_dir_und_format(self, monkeypatch, tmp_path):
        auf = {}
        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", self._fake_run(auf, '{"type":"text","part":{"text":"ok"}}'))
        ro.run("mach was", cwd=tmp_path, timeout=60, agent="spec", model="google/gemini-3.6-flash")
        assert "--agent" in auf["cmd"] and "spec" in auf["cmd"]
        assert "--format" in auf["cmd"] and "json" in auf["cmd"]
        assert "--dir" in auf["cmd"] and str(tmp_path) in auf["cmd"]

    def test_config_wird_ueber_umgebung_gesetzt_und_enthaelt_das_modell(self, monkeypatch, tmp_path):
        """Der Lauf darf nicht von ~/.config/opencode/opencode.json abhängen."""
        auf = {}
        inhalt = {}

        def _run(cmd, **kwargs):
            # Während des Laufs muss die Datei existieren — danach wird sie
            # entfernt, deshalb hier lesen und nicht hinterher.
            inhalt.update(json.loads(Path(kwargs["env"]["OPENCODE_CONFIG"]).read_text()))
            auf["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, '{"type":"text","part":{"text":"ok"}}', "")

        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", _run)
        ro.run("x", cwd=tmp_path, timeout=60, agent="plan", model="nvidia/moonshotai/kimi-k3")

        assert "OPENCODE_CONFIG" in auf["kwargs"]["env"]
        assert inhalt["agent"]["plan"]["model"] == "nvidia/moonshotai/kimi-k3"
        assert inhalt["agent"]["plan"]["permission"]["bash"] == "deny"

    def test_projekt_configs_sind_abgeschaltet(self, monkeypatch, tmp_path):
        """C2 (Abschluss-Review): opencode sucht Projekt-Configs von --dir
        aufwärts, und die schlagen OPENCODE_CONFIG. Eine Stufe mit
        `edit: allow` könnte sich damit ein <worktree>/opencode.json mit
        `bash: allow` schreiben und ihre eigene Einhegung aufheben — laut
        Probe (d) ist eine Shell der vollständige Ausbruch."""
        auf = {}
        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", self._fake_run(auf, _ABSCHLUSS))
        ro.run("x", cwd=tmp_path, timeout=60, agent="implement", model="m")
        assert auf["kwargs"]["env"][ro.PROJEKT_CONFIG_AUS] == "1"

    def test_temporaere_config_wird_hinterher_entfernt(self, monkeypatch, tmp_path):
        pfade = {}

        def _run(cmd, **kwargs):
            pfade["config"] = kwargs["env"]["OPENCODE_CONFIG"]
            return subprocess.CompletedProcess(cmd, 0, '{"type":"text","part":{"text":"ok"}}', "")

        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", _run)
        ro.run("x", cwd=tmp_path, timeout=60, agent="spec", model="m")
        assert not Path(pfade["config"]).exists()

    def test_fehlendes_binary_ist_kein_absturz(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ro, "OPENCODE_BIN", None)
        r = ro.run("x", cwd=tmp_path, timeout=60, agent="spec", model="m")
        assert r.ok is False
        assert "opencode" in (r.error or "")

    def test_zeitueberschreitung_wird_als_fehler_gemeldet(self, monkeypatch, tmp_path):
        def _timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 60)
        monkeypatch.setattr(ro, "OPENCODE_BIN", "/usr/local/bin/opencode")
        monkeypatch.setattr(subprocess, "run", _timeout)
        r = ro.run("x", cwd=tmp_path, timeout=60, agent="spec", model="m")
        assert r.ok is False
        assert "Zeitüberschreitung" in (r.error or "")
