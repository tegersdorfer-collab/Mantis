"""Tests für die Rechteschranke der opencode-Backends."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from forge.backends import OpencodePermission


class TestOpencodePermission:
    def test_standard_entspricht_der_aufnahme(self):
        """Probe (j) aus tests/fixtures/permission_probe_opencode.md."""
        d = OpencodePermission().als_dict()
        assert d == {
            "read": "allow", "edit": "allow", "glob": "allow",
            "grep": "allow", "list": "allow",
            "bash": "deny", "task": "deny",
            "webfetch": "deny", "websearch": "deny",
            "external_directory": "deny",
        }

    def test_bash_erlauben_wird_verweigert(self):
        with pytest.raises(ValueError, match="Probe \\(d\\)"):
            OpencodePermission(bash="allow")

    def test_task_erlauben_wird_verweigert(self):
        with pytest.raises(ValueError, match="Probe \\(e\\)"):
            OpencodePermission(task="allow")

    def test_external_directory_erlauben_wird_verweigert(self):
        with pytest.raises(ValueError, match="Probe \\(d\\)"):
            OpencodePermission(external_directory="allow")

    def test_unbekannter_wert_wird_verweigert(self):
        with pytest.raises(ValueError, match="allow.*ask.*deny"):
            OpencodePermission(read="vielleicht")
