"""Tests für den Claude-Runner. Die Erfolgs-Fixture ist eine echte Aufnahme von
`claude -p ... --output-format stream-json`, keine erfundene Struktur."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from forge import runner

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream_success.jsonl"


class TestParseEchteAufnahme:
    def test_erfolgreicher_lauf_ist_ok(self):
        ergebnis = runner.parse_stream(FIXTURE.read_text().splitlines())
        assert ergebnis.ok is True

    def test_text_ist_nicht_leer(self):
        ergebnis = runner.parse_stream(FIXTURE.read_text().splitlines())
        assert ergebnis.text.strip() != ""

    def test_tokens_werden_gezaehlt(self):
        # Ohne Verbrauchszahlen kann budget.py in Plan 3 nicht rechnen.
        ergebnis = runner.parse_stream(FIXTURE.read_text().splitlines())
        assert ergebnis.tokens_in > 0
        assert ergebnis.tokens_out > 0


class TestParseFehlerfaelle:
    def test_fehlerhafter_lauf_ist_nicht_ok(self):
        zeilen = [json.dumps({
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "boom",
        })]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is False
        assert ergebnis.error is not None

    def test_kaputte_json_zeilen_werden_uebersprungen(self):
        # Die CLI mischt gelegentlich Nicht-JSON-Zeilen dazwischen. Eine davon
        # darf nicht den gesamten Lauf als Fehlschlag erscheinen lassen.
        zeilen = [
            "kein json",
            "",
            json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "result": "fertig",
                        "usage": {"input_tokens": 10, "output_tokens": 20}}),
        ]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is True
        assert ergebnis.text == "fertig"

    def test_ohne_result_zeile_ist_es_ein_fehler(self):
        # Abgeschnittener Stream = abgebrochener Lauf.
        zeilen = [json.dumps({"type": "assistant", "message": {"content": []}})]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is False
        assert "kein Ergebnis" in ergebnis.error

    def test_leerer_stream_ist_ein_fehler(self):
        ergebnis = runner.parse_stream([])
        assert ergebnis.ok is False

    def test_fehlende_usage_ergibt_null_tokens(self):
        zeilen = [json.dumps({"type": "result", "subtype": "success",
                              "is_error": False, "result": "ok"})]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.ok is True
        assert ergebnis.tokens_in == 0

    def test_gueltiges_json_ohne_objekt_crasht_nicht(self):
        # Reproduzierbar: json.loads("null")/("3")/('"text"') liefert None/int/str
        # zurück. Vor dem Fix rief parse_stream() .get() darauf auf und crashte
        # mit AttributeError — ein sonst intakter Stream durfte daran nicht scheitern.
        ergebnis = runner.parse_stream(["null", "3", '"text"'])
        assert ergebnis.ok is False

    def test_fehlendes_is_error_gilt_als_fehlschlag(self):
        # is_error FEHLT (nicht: ist False) — für einen unbeaufsichtigten,
        # geldkostenden Prozess muss "unbekannt" als Fehlschlag gelten, nicht
        # stillschweigend als Erfolg durchgehen.
        zeile = json.dumps({"type": "result", "subtype": "success", "result": "ok"})
        ergebnis = runner.parse_stream([zeile])
        assert ergebnis.ok is False
        assert "is_error" in ergebnis.error


class TestRateLimitErkennung:
    def test_rate_limit_wird_als_solches_markiert(self):
        # Plan 3 verlässt sich auf dieses Flag, um bis zum Reset zu schlafen,
        # statt den Task zu parken.
        zeilen = [json.dumps({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "result": "Claude AI usage limit reached|1755100000",
        })]
        ergebnis = runner.parse_stream(zeilen)
        assert ergebnis.rate_limited is True

    def test_normaler_fehler_ist_kein_rate_limit(self):
        zeilen = [json.dumps({"type": "result", "subtype": "error_during_execution",
                              "is_error": True, "result": "Datei nicht gefunden"})]
        assert runner.parse_stream(zeilen).rate_limited is False


class TestRunFehlerpfade:
    """Deckt die Fehlerpfade von run() ab, die parse_stream allein nicht prüft.
    subprocess.run wird gestubbt — kein echter CLI-Lauf in der Test-Suite."""

    def test_timeout_ergibt_ok_false_ohne_exception(self):
        # Ohne diese Behandlung würde TimeoutExpired aus run() nach oben durchschlagen
        # und die gesamte Pipeline-Stufe crashen statt sauber als Fehlschlag zu enden.
        with patch("forge.runner.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=42)):
            ergebnis = runner.run("test", Path("/tmp"), timeout=42)
        assert ergebnis.ok is False
        assert "42" in ergebnis.error

    def test_claude_nicht_startbar_ergibt_ok_false_ohne_exception(self):
        # Fehlt das Binary oder fehlen Rechte, wirft subprocess.run OSError.
        # Ohne fangen würde die Forge daran crashen statt den Task zu parken.
        with patch("forge.runner.subprocess.run", side_effect=OSError("keine Berechtigung")):
            ergebnis = runner.run("test", Path("/tmp"))
        assert ergebnis.ok is False
        assert "claude nicht startbar" in ergebnis.error

    def test_stdin_wird_abgeklemmt(self):
        # Ohne DEVNULL wartet die CLI 3s auf Eingabe und schreibt eine Warnung —
        # pro Stufe, bei jedem Lauf.
        erfolg_zeile = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}})
        fake = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=erfolg_zeile, stderr="")
        with patch("forge.runner.subprocess.run", return_value=fake) as mock_run:
            runner.run("test", Path("/tmp"))
        assert mock_run.call_args.kwargs["stdin"] == subprocess.DEVNULL

    def test_stderr_wird_bei_fehlschlag_an_fehlermeldung_angehaengt(self):
        # Ohne stderr im Fehlertext fehlt der Forge/dem Menschen der eigentliche
        # Grund für einen Fehlschlag, wenn der Stream selbst keinen liefert.
        fehler_zeile = json.dumps({"type": "result", "subtype": "error_during_execution",
                                    "is_error": True, "result": "boom"})
        fake = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout=fehler_zeile, stderr="Verbindung verloren"
        )
        with patch("forge.runner.subprocess.run", return_value=fake):
            ergebnis = runner.run("test", Path("/tmp"))
        assert ergebnis.ok is False
        assert "Verbindung verloren" in ergebnis.error

    def test_rate_limit_marker_nur_in_stderr_setzt_rate_limited(self):
        # Der Stream selbst nennt den Grund nicht immer im Result-Text — manchmal
        # steht der Marker nur in stderr. Ohne diesen Fallback würde Plan 3 den
        # Task fälschlich parken statt bis zum Reset zu schlafen.
        fehler_zeile = json.dumps({"type": "result", "subtype": "error_during_execution",
                                    "is_error": True, "result": "boom"})
        fake = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout=fehler_zeile, stderr="rate limit exceeded, retry later"
        )
        with patch("forge.runner.subprocess.run", return_value=fake):
            ergebnis = runner.run("test", Path("/tmp"))
        assert ergebnis.rate_limited is True

    def test_fehlendes_claude_binary_ergibt_spezifischen_fehler(self, monkeypatch):
        # C1: unter launchd bekommt ein User-Agent nur PATH=/usr/bin:/bin:/usr/sbin:/sbin,
        # claude liegt aber unter ~/.local/bin. Vor dem Fix wäre subprocess.run(["claude", ...])
        # mit einem generischen OSError gescheitert ("claude nicht startbar: [Errno 2] ..."),
        # der sich nicht von "keine Rechte" oder "Datenträger voll" unterscheiden lässt.
        # Nach dem Fix wird das VOR jedem subprocess-Aufruf erkannt und die Meldung nennt
        # explizit Binary und die tatsächlich wirksame PATH.
        monkeypatch.setattr(runner, "CLAUDE_BIN", None)
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        ergebnis = runner.run("test", Path("/tmp"))
        assert ergebnis.ok is False
        assert "claude" in ergebnis.error
        assert "/usr/bin:/bin:/usr/sbin:/sbin" in ergebnis.error

    def test_erfolgreicher_lauf_ohne_stderr_bleibt_unveraendert(self):
        # Ein erfolgreicher Lauf soll stderr nicht in die Fehlermeldung mischen —
        # es gibt schließlich keine.
        erfolg_zeile = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}})
        fake = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=erfolg_zeile, stderr="")
        with patch("forge.runner.subprocess.run", return_value=fake):
            ergebnis = runner.run("test", Path("/tmp"))
        assert ergebnis.ok is True
        assert ergebnis.error is None
