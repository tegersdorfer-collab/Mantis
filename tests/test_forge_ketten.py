"""Tests für die Ausweichketten der Forge-Stufen."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import backends, ketten


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


def _kann_review_protokoll(backend_name: str) -> bool:
    """Implementiert das Backend hinter diesem Namen das Review-Protokoll?

    Das Protokoll besteht aus zwei Dateien: der Runner LIEST den Diff aus
    DIFF_DATEI und bettet ihn in den Prompt ein, und er SCHREIBT das Urteil
    nach VERDIKT_DATEI. Ein Runner, der beide Konstanten gar nicht kennt, kann
    beides nicht — und der Review-Prompt behauptet trotzdem beides.

    Geprüft wird über das Modul der run-Funktion, nicht über den Backend-Namen:
    so hängt die Zusicherung an der Fähigkeit, nicht an einer zweiten Liste,
    die mit der ersten aus dem Takt geraten kann.
    """
    modul = sys.modules[backends.hole(backend_name).__module__]
    return hasattr(modul, "DIFF_DATEI") and hasattr(modul, "VERDIKT_DATEI")


class TestReviewProtokoll:
    """Abschluss-Review C1 (2026-09-10): die review-Kette hatte als drittes Glied
    ein opencode-Modell. Sobald Antigravity erschöpft ist, lief Review damit gegen
    einen Runner ohne Diff im Prompt und ohne Verdikt-Datei — ein voller Lauf für
    nichts, und ein Modell mit `edit: allow` könnte sich selbst ein "pass"
    schreiben, ohne den Diff je gesehen zu haben."""

    def test_jedes_glied_der_reviewkette_beherrscht_das_protokoll(self):
        for backend, model in ketten.KETTEN["review"]:
            assert _kann_review_protokoll(backend), (
                f"review-Glied {backend}/{model} nutzt ein Backend, das weder DIFF_DATEI "
                f"noch VERDIKT_DATEI kennt — es kann das Review-Protokoll nicht erfüllen"
            )

    def test_die_pruefung_hat_zaehne(self):
        """Gegenprobe: mindestens ein Backend fällt bei _kann_review_protokoll durch.

        Ohne diese Zusicherung könnte die Prüfung oben stillschweigend zu einer
        Tautologie werden (etwa weil beide Runner die Konstanten irgendwann
        importieren) und niemandem fiele es auf."""
        assert not _kann_review_protokoll("opencode")


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


class TestKetteErschoepft:
    """Die beiden Ursachen für ein None aus `waehle` müssen unterscheidbar sein:
    Reviewer-Kollision (Spec 183: parken) und Kontingent-Erschöpfung
    (Spec 264: die Nacht endet, der Task bleibt liegen)."""

    def test_reviewer_kollision_ist_keine_erschoepfung(self, leer):
        kette = ketten.KETTEN["review"]
        letztes = kette[-1][1]
        for _, model in kette[:-1]:
            leer.add(model)
        assert ketten.waehle("review", verboten=frozenset({letztes})) is None
        assert ketten.kette_erschoepft("review") is False

    def test_trockene_kette_meldet_erschoepft(self, leer):
        for _, model in ketten.KETTEN["review"]:
            leer.add(model)
        assert ketten.kette_erschoepft("review") is True

    def test_volle_kette_ist_nicht_erschoepft(self, leer):
        assert ketten.kette_erschoepft("implement") is False
