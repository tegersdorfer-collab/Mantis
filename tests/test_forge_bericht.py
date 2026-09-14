import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import bericht as b
from forge import models as m


def _welt(monkeypatch, freigabe=(), geparkt=(), stand=()):
    def _nach_zustand(state, quelle=None):
        return {m.AWAITING_APPROVAL: list(freigabe), m.PARKED: list(geparkt)}.get(state, [])
    monkeypatch.setattr(b.queue, "nach_zustand", _nach_zustand)
    monkeypatch.setattr(b.budget, "stand", lambda: list(stand))


class TestMorgenbericht:
    def test_leere_nacht(self, monkeypatch):
        _welt(monkeypatch)
        text = b.morgenbericht()
        assert "Zur Freigabe: keine" in text
        assert "Geparkt: keine" in text

    def test_nennt_freigaben_mit_id_und_titel(self, monkeypatch):
        _welt(monkeypatch, freigabe=[{"id": 7, "title": "Docs für X", "branch": "forge/task-7"}])
        text = b.morgenbericht()
        assert "#7 Docs für X" in text
        assert "forge.cli approve 7" in text

    def test_nennt_park_gruende(self, monkeypatch):
        _welt(monkeypatch, geparkt=[{"id": 8, "title": "Y", "parked_reason": "Gate rot: tests"}])
        assert "#8 Y — Gate rot: tests" in b.morgenbericht()

    def test_nennt_leere_anbieter_und_verbrauch(self, monkeypatch):
        _welt(monkeypatch, stand=[
            {"provider": "nvidia", "laeufe": 12, "tokens_in": 1000, "tokens_out": 200,
             "erschoepft_seit": "2026-09-15 02:10", "grund": "429"},
            {"provider": "antigravity", "laeufe": 3, "tokens_in": 0, "tokens_out": 0,
             "erschoepft_seit": None, "grund": None},
        ])
        text = b.morgenbericht()
        assert "nvidia: leer seit 2026-09-15 02:10 (429), 12 Läufe" in text
        assert "antigravity: 3 Läufe" in text
