import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import bericht as b
from forge import models as m


def _welt(monkeypatch, tmp_path, freigabe=(), geparkt=(), stand=(), daemon_ereignisse=None):
    def _nach_zustand(state, quelle=None):
        return {m.AWAITING_APPROVAL: list(freigabe), m.PARKED: list(geparkt)}.get(state, [])
    monkeypatch.setattr(b.queue, "nach_zustand", _nach_zustand)
    monkeypatch.setattr(b.budget, "stand", lambda: list(stand))
    # Abschluss-Review 2c, I4: Not-Aus-Datei und Daemon-Ereignisse gehören in
    # den Bericht. Beide Ränder gestubbt — kein Test hier fasst ~ oder die DB an.
    monkeypatch.setattr(b.daemon, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(b.journal, "letzte_daemon_ereignisse",
                        lambda: dict(daemon_ereignisse or {}))


class TestMorgenbericht:
    def test_leere_nacht(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path)
        text = b.morgenbericht()
        assert "Zur Freigabe: keine" in text
        assert "Geparkt: keine" in text

    def test_nennt_freigaben_mit_id_und_titel(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path, freigabe=[{"id": 7, "title": "Docs für X", "branch": "forge/task-7"}])
        text = b.morgenbericht()
        assert "#7 Docs für X" in text
        assert "forge.cli approve 7" in text

    def test_nennt_park_gruende(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path, geparkt=[{"id": 8, "title": "Y", "parked_reason": "Gate rot: tests"}])
        assert "#8 Y — Gate rot: tests" in b.morgenbericht()

    def test_nennt_leere_anbieter_und_verbrauch(self, monkeypatch, tmp_path):
        # I4 (c): der Nachtrag verlangt "Verbrauch je Anbieter" — die Zeile
        # trägt jetzt Tokens rein/raus; die String-Form von erschoepft_seit
        # (wie sie ein Fixture liefert) bleibt unverändert lesbar.
        _welt(monkeypatch, tmp_path, stand=[
            {"provider": "nvidia", "laeufe": 12, "tokens_in": 1000, "tokens_out": 200,
             "erschoepft_seit": "2026-09-15 02:10", "grund": "429"},
            {"provider": "antigravity", "laeufe": 3, "tokens_in": 0, "tokens_out": 0,
             "erschoepft_seit": None, "grund": None},
        ])
        text = b.morgenbericht()
        assert "nvidia: leer seit 2026-09-15 02:10 (429), 12 Läufe, 1000 rein / 200 raus" in text
        assert "antigravity: 3 Läufe, 0 rein / 0 raus" in text

    def test_erschoepft_seit_als_datetime_wird_zur_uhrzeit(self, monkeypatch, tmp_path):
        # budget.stand() liefert aus Postgres ein TIMESTAMPTZ, also datetime —
        # im Bericht reicht die Uhrzeit, die Nacht ist ohnehin bekannt.
        _welt(monkeypatch, tmp_path, stand=[
            {"provider": "nvidia", "laeufe": 1, "tokens_in": 5, "tokens_out": 6,
             "erschoepft_seit": datetime(2026, 9, 15, 2, 10, 33), "grund": "429"},
        ])
        assert "nvidia: leer seit 02:10 (429), 1 Läufe, 5 rein / 6 raus" in b.morgenbericht()


class TestNotAusUndDaemonImBericht:
    """Abschluss-Review 2c, I4: der Bericht konnte nicht sagen, ob die Nacht
    überhaupt stattgefunden hat oder ob der Not-Aus steht — "Zur Freigabe:
    keine" sah in beiden Fällen gleich aus."""

    def test_ohne_not_aus_datei(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path)
        assert "Not-Aus: nein" in b.morgenbericht()

    def test_not_aus_datei_mit_inhalt(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path)
        (tmp_path / "stop").write_text("Fehler-Spirale: 3 Fehlschläge in Folge\n")
        text = b.morgenbericht()
        assert "Not-Aus: aktiv — Fehler-Spirale: 3 Fehlschläge in Folge" in text
        assert "Not-Aus: nein" not in text

    def test_leere_not_aus_datei_ist_trotzdem_aktiv(self, monkeypatch, tmp_path):
        # `touch ~/.mantis-forge-stop` (siehe forge.cli stop) hat keinen Inhalt.
        _welt(monkeypatch, tmp_path)
        (tmp_path / "stop").write_text("")
        assert "Not-Aus: aktiv" in b.morgenbericht()

    def test_daemon_start_und_stop_aus_dem_journal(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path, daemon_ereignisse={
            "daemon_start": {"ts": datetime(2026, 9, 14, 23, 0, 4), "message": "Forge gestartet"},
            "daemon_stop": {"ts": datetime(2026, 9, 15, 7, 0, 12),
                            "message": "Nachtfenster zu Ende (7:00) — Daemon beendet sich"},
        })
        text = b.morgenbericht()
        assert "Daemon: gestartet 2026-09-14 23:00, beendet 2026-09-15 07:00 (Nachtfenster zu Ende" in text

    def test_daemon_ohne_stop_ist_noch_am_laufen_oder_abgestuerzt(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path, daemon_ereignisse={
            "daemon_start": {"ts": datetime(2026, 9, 14, 23, 0, 4), "message": "Forge gestartet"},
        })
        text = b.morgenbericht()
        assert "Daemon: gestartet 2026-09-14 23:00, kein Ende im Journal" in text

    def test_daemon_stop_aelter_als_start_zaehlt_nicht_als_ende(self, monkeypatch, tmp_path):
        # Der Stop der VORLETZTEN Nacht darf nicht als Ende des letzten Starts
        # gelten — sonst sähe ein Absturz wie ein sauberes Ende aus.
        _welt(monkeypatch, tmp_path, daemon_ereignisse={
            "daemon_start": {"ts": datetime(2026, 9, 14, 23, 0, 4), "message": "Forge gestartet"},
            "daemon_stop": {"ts": datetime(2026, 9, 14, 7, 0, 12), "message": "Nachtfenster zu Ende"},
        })
        assert "kein Ende im Journal" in b.morgenbericht()

    def test_ohne_journal_eintraege(self, monkeypatch, tmp_path):
        _welt(monkeypatch, tmp_path, daemon_ereignisse={})
        assert "Daemon: kein Start im Journal" in b.morgenbericht()
