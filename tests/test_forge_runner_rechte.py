"""Rechteprofile für headless Läufe. Kein echter CLI-Aufruf — subprocess wird
gestubbt; das gemessene Verhalten der CLI steht in tests/fixtures/permission_probe.md."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest

from forge import runner


class _Aufzeichnung:
    """Fängt den Befehl ab, statt claude wirklich zu starten."""

    def __init__(self):
        self.befehl = None

    def __call__(self, befehl, **kwargs):
        self.befehl = befehl

        class _Fertig:
            returncode = 0
            stdout = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}})
            stderr = ""
        return _Fertig()


class TestProfilWirdUebergeben:
    def test_erlaubte_tools_landen_im_befehl(self, monkeypatch, tmp_path):
        auf = _Aufzeichnung()
        monkeypatch.setattr(runner.subprocess, "run", auf)
        profil = runner.PermissionProfile(allowed=("Read", "Grep"), mode="acceptEdits")
        runner.run("egal", cwd=tmp_path, profile=profil)
        assert "--allowedTools" in auf.befehl
        i = auf.befehl.index("--allowedTools")
        assert auf.befehl[i + 1] == "Read Grep"

    def test_modus_landet_im_befehl(self, monkeypatch, tmp_path):
        auf = _Aufzeichnung()
        monkeypatch.setattr(runner.subprocess, "run", auf)
        runner.run("egal", cwd=tmp_path,
                   profile=runner.PermissionProfile(allowed=("Read",), mode="acceptEdits"))
        i = auf.befehl.index("--permission-mode")
        assert auf.befehl[i + 1] == "acceptEdits"

    def test_ohne_profil_keine_rechte_flags(self, monkeypatch, tmp_path):
        # Rückwärtskompatibel: bestehende Aufrufer aus Plan 1 ändern sich nicht.
        auf = _Aufzeichnung()
        monkeypatch.setattr(runner.subprocess, "run", auf)
        runner.run("egal", cwd=tmp_path)
        assert "--allowedTools" not in auf.befehl
        assert "--permission-mode" not in auf.befehl

    def test_bypass_wird_verweigert(self, monkeypatch, tmp_path):
        # Ein unbeaufsichtigter Daemon darf sich niemals alle Rechte geben.
        # Diese Schranke ist der Grund, warum es das Profil überhaupt gibt.
        with pytest.raises(ValueError):
            runner.PermissionProfile(allowed=("Read",), mode="bypassPermissions")

    def test_leere_tool_liste_wird_verweigert(self, monkeypatch, tmp_path):
        # Ein Profil ohne Tools ist fast immer ein Konfigurationsfehler und
        # würde als 30-Minuten-Hänger enden statt als Fehlermeldung.
        with pytest.raises(ValueError):
            runner.PermissionProfile(allowed=(), mode="acceptEdits")

    def test_stdin_bleibt_abgeklemmt(self, monkeypatch, tmp_path):
        # Regressionsschutz für den Plan-1-Befund (3s Wartezeit pro Lauf).
        gesehen = {}

        def _run(befehl, **kwargs):
            gesehen.update(kwargs)

            class _F:
                returncode = 0
                stdout = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                     "result": "ok", "usage": {}})
                stderr = ""
            return _F()

        monkeypatch.setattr(runner.subprocess, "run", _run)
        runner.run("egal", cwd=tmp_path,
                   profile=runner.PermissionProfile(allowed=("Read",), mode="acceptEdits"))
        assert gesehen["stdin"] == runner.subprocess.DEVNULL


class TestProfilValidierungUeberDenAuftragHinaus:
    """Zusätzliche Härtung über die Aufgabenliste hinaus (siehe Aufnahme-Protokoll):
    ein Profil mit kaputten Werkzeugnamen darf keine kaputte Befehlszeile erzeugen,
    sondern muss sofort mit ValueError scheitern."""

    def test_leerer_werkzeugname_wird_verweigert(self):
        # Ein leerer String in der Liste würde " ".join() zu einem doppelten
        # Leerzeichen in --allowedTools machen — eine CLI-Zeile, die niemand
        # absichtlich so geschrieben hätte und die sich schwer debuggen lässt.
        with pytest.raises(ValueError):
            runner.PermissionProfile(allowed=("Read", ""), mode="acceptEdits")

    def test_nur_leerzeichen_als_werkzeugname_wird_verweigert(self):
        # "   " ist kein leerer String, aber genauso ein kaputter Tool-Name.
        with pytest.raises(ValueError):
            runner.PermissionProfile(allowed=("Read", "   "), mode="acceptEdits")

    def test_ungueltiger_modus_wird_verweigert(self):
        # Tippfehler wie "acceptedits" (Groß/Kleinschreibung) dürfen nicht
        # stillschweigend als gültiger Modus durchgehen.
        with pytest.raises(ValueError):
            runner.PermissionProfile(allowed=("Read",), mode="acceptedits")


class TestDefaultModusNachMessung:
    """Die Aufnahme (tests/fixtures/permission_probe.md, Probe b1) zeigt: im
    Modus 'acceptEdits' wird --allowedTools für Datei-Edits ignoriert — ein Lauf
    schreibt eine Datei, obwohl 'Write' nicht erlaubt war. 'dontAsk' verweigert
    denselben Zugriff korrekt (permission_denials) und hängt dabei nicht. Deshalb
    weicht der Default hier bewusst von der Aufgabenstellung ab."""

    def test_default_modus_ist_dontask(self):
        profil = runner.PermissionProfile(allowed=("Read",))
        assert profil.mode == "dontAsk"

    def test_default_landet_im_befehl(self, monkeypatch, tmp_path):
        auf = _Aufzeichnung()
        monkeypatch.setattr(runner.subprocess, "run", auf)
        runner.run("egal", cwd=tmp_path, profile=runner.PermissionProfile(allowed=("Read",)))
        i = auf.befehl.index("--permission-mode")
        assert auf.befehl[i + 1] == "dontAsk"
