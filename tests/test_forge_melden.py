import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forge import melden


class TestTeile:
    def test_kurzer_text_bleibt_ein_stueck(self):
        assert melden.teile("hallo") == ["hallo"]

    def test_teilt_am_letzten_zeilenumbruch_vor_der_grenze(self):
        text = "a" * 10 + "\n" + "b" * 10 + "\n" + "c" * 10
        assert melden.teile(text, max_len=25) == ["a" * 10 + "\n" + "b" * 10, "c" * 10]

    def test_ohne_zeilenumbruch_hart_an_der_grenze(self):
        assert melden.teile("x" * 30, max_len=10) == ["x" * 10] * 3

    def test_leerer_text_ist_ein_leeres_stueck(self):
        # sende() schickt dann nichts — aber teile() darf nicht abstürzen.
        assert melden.teile("") == []


class _Antwort(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSende:
    def _umgebung(self, monkeypatch, token="T", chat="42"):
        monkeypatch.setenv("FORGE_BOT_TOKEN", token)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)

    def test_ohne_token_kein_aufruf_und_false(self, monkeypatch):
        monkeypatch.delenv("FORGE_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
        aufrufe = []
        monkeypatch.setattr(melden.urllib.request, "urlopen", lambda *a, **k: aufrufe.append(a))
        assert melden.sende("hi") is False
        assert aufrufe == []

    def test_ohne_chat_id_kein_aufruf_und_false(self, monkeypatch):
        monkeypatch.setenv("FORGE_BOT_TOKEN", "T")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        aufrufe = []
        monkeypatch.setattr(melden.urllib.request, "urlopen", lambda *a, **k: aufrufe.append(a))
        assert melden.sende("hi") is False
        assert aufrufe == []

    def test_schickt_chat_id_und_text(self, monkeypatch):
        self._umgebung(monkeypatch)
        gesehen = []

        def _urlopen(req, timeout=None):
            gesehen.append((req.full_url, req.data.decode(), timeout))
            return _Antwort(json.dumps({"ok": True}).encode())

        monkeypatch.setattr(melden.urllib.request, "urlopen", _urlopen)
        assert melden.sende("Morgenbericht\nZeile 2") is True
        url, daten, timeout = gesehen[0]
        assert url == "https://api.telegram.org/botT/sendMessage"
        assert "chat_id=42" in daten
        assert "text=Morgenbericht%0AZeile+2" in daten
        assert timeout is not None

    def test_lange_texte_gehen_in_mehreren_nachrichten(self, monkeypatch):
        self._umgebung(monkeypatch)
        gesehen = []

        def _urlopen(req, timeout=None):
            gesehen.append(req.data.decode())
            return _Antwort(b'{"ok": true}')

        monkeypatch.setattr(melden.urllib.request, "urlopen", _urlopen)
        assert melden.sende("z\n" * 3000) is True
        assert len(gesehen) == 2

    def test_http_fehler_ist_false_ohne_traceback(self, monkeypatch, caplog):
        self._umgebung(monkeypatch, token="GEHEIM")

        def _urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(melden.urllib.request, "urlopen", _urlopen)
        assert melden.sende("hi") is False
        assert "401" in caplog.text
        assert "GEHEIM" not in caplog.text, "der Token darf nie ins Log"

    def test_netz_weg_ist_false(self, monkeypatch):
        self._umgebung(monkeypatch)

        def _urlopen(req, timeout=None):
            raise urllib.error.URLError("Name or service not known")

        monkeypatch.setattr(melden.urllib.request, "urlopen", _urlopen)
        assert melden.sende("hi") is False

    def test_telegram_sagt_nicht_ok_ist_false(self, monkeypatch):
        self._umgebung(monkeypatch)
        monkeypatch.setattr(
            melden.urllib.request, "urlopen",
            lambda req, timeout=None: _Antwort(b'{"ok": false, "description": "chat not found"}'),
        )
        assert melden.sende("hi") is False
