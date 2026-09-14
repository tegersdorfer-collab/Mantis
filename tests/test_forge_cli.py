import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import cli


def _stumm(monkeypatch):
    monkeypatch.setattr(cli.db, "init_pool", lambda *a, **k: None)


class TestCli:
    def test_status_druckt_den_morgenbericht(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.bericht, "morgenbericht", lambda: "BERICHT")
        assert cli.main(["status"]) == 0
        assert "BERICHT" in capsys.readouterr().out

    def test_approve_ruft_freigeben_und_meldet(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        gerufen = []
        monkeypatch.setattr(cli.freigabe, "freigeben", lambda tid: gerufen.append(tid) or "gemerged")
        assert cli.main(["approve", "7"]) == 0
        assert gerufen == [7]
        assert "gemerged" in capsys.readouterr().out

    def test_approve_konflikt_ist_exit_1(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.freigabe, "freigeben", lambda tid: "konflikt")
        assert cli.main(["approve", "7"]) == 1

    def test_reject_braucht_grund(self, monkeypatch, capsys):
        _stumm(monkeypatch)
        gerufen = []
        monkeypatch.setattr(cli.freigabe, "ablehnen", lambda tid, grund: gerufen.append((tid, grund)) or True)
        assert cli.main(["reject", "7", "zu gross"]) == 0
        assert gerufen == [(7, "zu gross")]

    def test_requeue(self, monkeypatch):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.freigabe, "neu_einreihen", lambda tid: True)
        assert cli.main(["requeue", "7"]) == 0

    def test_stop_schreibt_die_halt_datei(self, monkeypatch, tmp_path):
        _stumm(monkeypatch)
        halt = tmp_path / "halt"
        monkeypatch.setattr(cli.daemon, "HALT_FILE", halt)
        assert cli.main(["stop"]) == 0
        assert halt.exists()
