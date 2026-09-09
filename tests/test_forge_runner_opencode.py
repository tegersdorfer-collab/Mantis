"""Tests für das opencode-Backend."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge.runner_opencode import baue_config


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
