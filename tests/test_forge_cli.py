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
        monkeypatch.setattr(cli.freigabe.daemon, "HALT_FILE", halt)
        monkeypatch.setattr(cli.freigabe, "daemon_laeuft", lambda: True)
        assert cli.main(["stop"]) == 0
        assert halt.exists()


class TestStopOhneDaemon:
    """Abschluss-Review 2c, I3: ohne laufenden Daemon schrieb `stop` die
    Halt-Datei und meldete "der Daemon beendet sich" — beim nächsten Start
    räumt daemon.main() genau diese Datei als veraltet weg (Halt ist eine
    Bitte an den LAUFENDEN Daemon). Timo glaubte, die Nacht sei abgesagt, und
    sie lief trotzdem. `_daemon_laeuft` ist gepatcht, nicht pgrep: ein
    `pgrep -f forge.daemon` würde auch diesen pytest-Prozess treffen, sobald
    sein argv den String enthält."""

    def test_ohne_daemon_keine_halt_datei_und_exit_1(self, monkeypatch, tmp_path, capsys):
        _stumm(monkeypatch)
        halt = tmp_path / "halt"
        monkeypatch.setattr(cli.freigabe.daemon, "HALT_FILE", halt)
        monkeypatch.setattr(cli.freigabe, "daemon_laeuft", lambda: False)
        assert cli.main(["stop"]) == 1
        assert not halt.exists()

    def test_ohne_daemon_nennt_den_not_aus_und_launchctl(self, monkeypatch, tmp_path, capsys):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.freigabe.daemon, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(cli.freigabe, "daemon_laeuft", lambda: False)
        cli.main(["stop"])
        out = capsys.readouterr().out
        assert "Kein Daemon läuft" in out
        assert "touch ~/.mantis-forge-stop" in out
        assert "launchctl bootout gui/$(id -u)/com.mantis.forge" in out

    def test_mit_daemon_meldet_den_halt(self, monkeypatch, tmp_path, capsys):
        _stumm(monkeypatch)
        monkeypatch.setattr(cli.freigabe.daemon, "HALT_FILE", tmp_path / "halt")
        monkeypatch.setattr(cli.freigabe, "daemon_laeuft", lambda: True)
        assert cli.main(["stop"]) == 0
        assert "Halt angefordert" in capsys.readouterr().out


class TestAblehnungsgrundErreichtDasTerminal:
    """Abschluss-Review 2c, I6: `freigeben` meldet "abgelehnt" und nennt den
    Grund nur per log.warning. Das CLI konfiguriert logging, damit der Grund
    im Terminal steht — der String-Vertrag der Funktion bleibt für Plan 3."""

    def test_main_konfiguriert_logging_auf_warning(self, monkeypatch):
        _stumm(monkeypatch)
        konfiguriert = []
        monkeypatch.setattr(cli.logging, "basicConfig", lambda **kw: konfiguriert.append(kw))
        monkeypatch.setattr(cli.freigabe, "freigeben", lambda tid: "abgelehnt")
        assert cli.main(["approve", "7"]) == 1
        assert konfiguriert and konfiguriert[0]["level"] == cli.logging.WARNING
