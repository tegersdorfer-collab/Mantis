"""Stufen-Fortschritt und Fix-Runden in der Queue. DB gestubbt."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge import queue as q


class _Rec:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executes = []

    def execute(self, sql, params=()):
        self.executes.append((sql, params))
        return 1

    def query_one(self, sql, params=()):
        self.executes.append((sql, params))
        return self.rows[0] if self.rows else None


def _patch(monkeypatch, rows=None):
    rec = _Rec(rows)
    monkeypatch.setattr(q.db, "execute", rec.execute)
    monkeypatch.setattr(q.db, "query_one", rec.query_one)
    return rec


class TestArtefakt:
    def test_spec_pfad_wird_geschrieben(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.setze_artefakt(3, "spec_path", "docs/superpowers/specs/x-design.md")
        assert rec.executes[-1][1] == ("docs/superpowers/specs/x-design.md", 3)

    def test_plan_pfad_wird_geschrieben(self, monkeypatch):
        rec = _patch(monkeypatch)
        q.setze_artefakt(3, "plan_path", "docs/superpowers/plans/x-plan.md")
        assert "plan_path" in rec.executes[-1][0]

    def test_worktree_pfad_wird_geschrieben(self, monkeypatch):
        # Stelle sicher, dass alle Whitelist-Felder tatsächlich geschrieben werden.
        rec = _patch(monkeypatch)
        q.setze_artefakt(3, "worktree_path", "/tmp/worktree")
        assert "worktree_path" in rec.executes[-1][0]
        assert rec.executes[-1][1] == ("/tmp/worktree", 3)

    def test_branch_wird_geschrieben(self, monkeypatch):
        # Stelle sicher, dass alle Whitelist-Felder tatsächlich geschrieben werden.
        rec = _patch(monkeypatch)
        q.setze_artefakt(3, "branch", "forge/pipeline")
        assert "branch" in rec.executes[-1][0]
        assert rec.executes[-1][1] == ("forge/pipeline", 3)

    def test_fremdes_feld_wird_verweigert(self, monkeypatch):
        # Der Feldname geht in den SQL-Text — ohne Whitelist wäre das eine
        # Injection-Stelle mitten in der Datenschicht.
        rec = _patch(monkeypatch)
        with pytest.raises(ValueError):
            q.setze_artefakt(3, "state; DROP TABLE forge_tasks", "x")
        assert rec.executes == []

    def test_feldname_prefix_wird_verweigert(self, monkeypatch):
        # Präfixe der Whitelist-Felder dürfen nicht durchgehen.
        # Das würde z.B. spec_path bei einer Eingabe von 'spec' nicht finden.
        rec = _patch(monkeypatch)
        with pytest.raises(ValueError):
            q.setze_artefakt(3, "spec", "/some/path")
        assert rec.executes == []

    def test_feldname_superstring_wird_verweigert(self, monkeypatch):
        # Superstrings (z.B. spec_path_x) dürfen nicht durchgehen —
        # das würde bedeuten, dass Typos in der Whitelist-Definition
        # erst zur Laufzeit auffallen würden.
        rec = _patch(monkeypatch)
        with pytest.raises(ValueError):
            q.setze_artefakt(3, "spec_path_x", "/some/path")
        assert rec.executes == []

    def test_updated_at_wird_gesetzt(self, monkeypatch):
        # updated_at muss immer aktualisiert werden.
        rec = _patch(monkeypatch)
        q.setze_artefakt(3, "spec_path", "path/to/spec")
        assert "updated_at=NOW()" in rec.executes[-1][0]


class TestFixrunden:
    def test_erste_runde_gibt_eins(self, monkeypatch):
        _patch(monkeypatch, rows=[{"refusals": 1}])
        assert q.zaehle_fixrunde(4) == 1

    def test_zaehler_wird_erhoeht_nicht_gesetzt(self, monkeypatch):
        rec = _patch(monkeypatch, rows=[{"refusals": 2}])
        q.zaehle_fixrunde(4)
        assert "refusals+1" in rec.executes[-1][0].replace(" ", "")

    def test_zweite_runde_gibt_zwei(self, monkeypatch):
        _patch(monkeypatch, rows=[{"refusals": 2}])
        assert q.zaehle_fixrunde(4) == 2

    def test_dritte_runde_gibt_drei(self, monkeypatch):
        _patch(monkeypatch, rows=[{"refusals": 3}])
        assert q.zaehle_fixrunde(4) == 3

    def test_geloeschter_task_gibt_null(self, monkeypatch):
        # Wenn der Task zwischen read und write gelöscht wurde (UPDATE gibt keine Zeile),
        # darf zaehle_fixrunde nicht crashen, sondern gibt 0 zurück.
        _patch(monkeypatch, rows=[])
        result = q.zaehle_fixrunde(999)
        assert result == 0

    def test_updated_at_wird_mitgezogen(self, monkeypatch):
        # updated_at muss immer aktualisiert werden.
        rec = _patch(monkeypatch, rows=[{"refusals": 1}])
        q.zaehle_fixrunde(4)
        assert "updated_at=NOW()" in rec.executes[-1][0]

    def test_task_id_wird_korrekt_parametrisiert(self, monkeypatch):
        # Die task_id darf nicht im SQL-Text landen.
        rec = _patch(monkeypatch, rows=[{"refusals": 1}])
        q.zaehle_fixrunde(42)
        assert rec.executes[-1][1] == (42,)
