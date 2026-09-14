"""Unit-Tests für das Zustandsmodell der Forge — reine Logik, kein I/O."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import models as m


class TestTransitions:
    def test_queued_darf_nach_speccing(self):
        assert m.can_transition(m.QUEUED, m.SPECCING) is True

    def test_queued_darf_nicht_direkt_nach_merged(self):
        # Ein Task muss durch die Pipeline — Abkürzungen sind genau der Fehler,
        # den das Gate verhindern soll.
        assert m.can_transition(m.QUEUED, m.MERGED) is False

    def test_gating_darf_nicht_direkt_nach_merged(self):
        # Nachtrag 2c: kein automatischer Merge aus GATING, auch nicht für
        # Docs (Spec, "Bewusst nicht enthalten") — grün heisst warten auf
        # Timos Freigabe (siehe TestZustandNachDemGate).
        assert m.can_transition(m.GATING, m.MERGED) is False

    def test_merged_ist_endzustand(self):
        assert m.can_transition(m.MERGED, m.QUEUED) is False

    def test_parked_darf_zurueck_in_die_queue(self):
        # Freigabe durch Timo im Dashboard.
        assert m.can_transition(m.PARKED, m.QUEUED) is True

    def test_jede_stufe_darf_parken(self):
        for state in [m.SPECCING, m.PLANNING, m.IMPLEMENTING, m.REVIEWING, m.GATING]:
            assert m.can_transition(state, m.PARKED) is True

    def test_unbekannter_zustand_ist_kein_uebergang(self):
        assert m.can_transition("voelliger_quatsch", m.QUEUED) is False


class TestFixSchleifeOhneImplementReRun:
    def test_reviewing_geht_nicht_mehr_nach_implementing(self):
        """Nachtrag 2c: der Fix bleibt in REVIEWING; der Übergang wäre ungenutzt,
        und ungenutzte Übergänge sind das, was die Fix-Schleife bis 2b
        unerreichbar gemacht hat."""
        assert not m.can_transition(m.REVIEWING, m.IMPLEMENTING)

    def test_reviewing_geht_weiterhin_nach_gating(self):
        assert m.can_transition(m.REVIEWING, m.GATING)


class TestActiveStates:
    def test_aktive_zustaende_sind_die_pipeline_stufen(self):
        assert m.ACTIVE_STATES == frozenset({
            m.SPECCING, m.PLANNING, m.IMPLEMENTING,
            m.REVIEWING, m.GATING,
        })

    def test_queued_ist_nicht_aktiv(self):
        # Sonst würde der Daemon einen wartenden Task für laufend halten.
        assert m.QUEUED not in m.ACTIVE_STATES

    def test_endzustaende_sind_nicht_aktiv(self):
        for state in [m.MERGED, m.PARKED, m.FAILED]:
            assert state not in m.ACTIVE_STATES


class TestZustandNachDemGate:
    """Nachtrag 2c: GATING -> AWAITING_APPROVAL -> MERGED | PARKED."""

    def test_gruenes_gate_wartet_auf_freigabe(self):
        assert m.can_transition(m.GATING, m.AWAITING_APPROVAL)

    def test_kein_automatischer_merge_aus_gating(self):
        """Spec, 'Bewusst nicht enthalten': kein automatischer Merge, auch
        nicht für Docs."""
        assert not m.can_transition(m.GATING, m.MERGED)

    def test_freigabe_fuehrt_zu_merged_oder_parked(self):
        assert m.can_transition(m.AWAITING_APPROVAL, m.MERGED)
        assert m.can_transition(m.AWAITING_APPROVAL, m.PARKED)
        assert not m.can_transition(m.AWAITING_APPROVAL, m.QUEUED)

    def test_wartender_task_blockiert_keine_bahn(self):
        assert m.AWAITING_APPROVAL not in m.ACTIVE_STATES

    def test_awaiting_restart_gibt_es_nicht_mehr(self):
        assert not hasattr(m, "AWAITING_RESTART")
        assert "awaiting_restart_window" not in m._TRANSITIONS
