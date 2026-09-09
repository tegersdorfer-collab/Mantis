"""Tests für das opencode-Backend."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pathlib

from forge.runner_opencode import baue_config, parse_events

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


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
