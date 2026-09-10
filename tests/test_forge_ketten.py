"""Tests für die Ausweichketten der Forge-Stufen."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import ketten


@pytest.fixture
def leer(monkeypatch):
    """Nichts ist erschöpft, sofern der Test es nicht sagt."""
    erschoepft = set()
    monkeypatch.setattr(ketten.budget, "ist_erschoepft", lambda m: m in erschoepft)
    return erschoepft


class TestKettenAufbau:
    def test_jede_stufe_hat_eine_kette(self):
        for name in ("spec", "plan", "implement", "review", "fix"):
            assert ketten.KETTEN[name], f"Stufe {name} ohne Kette"

    def test_review_beginnt_auf_antigravity(self):
        backend, model = ketten.KETTEN["review"][0]
        assert backend == "agy"
        assert model == "claude-opus-4-6-thinking"

    def test_keine_kette_nennt_ein_modell_doppelt(self):
        for name, kette in ketten.KETTEN.items():
            modelle = [m for _, m in kette]
            assert len(modelle) == len(set(modelle)), f"{name} nennt ein Modell doppelt"


class TestWahl:
    def test_erstes_glied_wenn_nichts_erschoepft(self, leer):
        assert ketten.waehle("implement") == ketten.KETTEN["implement"][0]

    def test_weicht_auf_das_naechste_glied_aus(self, leer):
        leer.add(ketten.KETTEN["implement"][0][1])
        assert ketten.waehle("implement") == ketten.KETTEN["implement"][1]

    def test_leere_kette_gibt_none(self, leer):
        for _, model in ketten.KETTEN["implement"]:
            leer.add(model)
        assert ketten.waehle("implement") is None

    def test_unbekannte_stufe_gibt_none(self, leer):
        assert ketten.waehle("gibtsnicht") is None


class TestReviewerRegel:
    def test_verbotenes_modell_wird_uebersprungen(self, leer):
        erstes = ketten.KETTEN["review"][0][1]
        gewaehlt = ketten.waehle("review", verboten=frozenset({erstes}))
        assert gewaehlt is not None
        assert gewaehlt[1] != erstes

    def test_lieber_none_als_selbstabnahme(self, leer):
        """Bleibt nur noch das Implementierer-Modell übrig, wird nicht reviewt.
        Ein Modell, das seinen eigenen Code abnimmt, sieht nur aus wie ein Review."""
        kette = ketten.KETTEN["review"]
        letztes = kette[-1][1]
        for _, model in kette[:-1]:
            leer.add(model)
        assert ketten.waehle("review", verboten=frozenset({letztes})) is None
