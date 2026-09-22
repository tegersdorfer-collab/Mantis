"""Tests für die UI-Automatik-Engine (tools/uiauto/engine.py) — ohne echtes atomacos.

Gemockt wird die atomacos-Grenze (_get_app/_raw_elements/_ax_is_trusted), wie bei
tools/flipper der Serial-Treiber. Geprüft werden Rollen-Whitelist, Ref-Zuordnung,
dass act() das richtige Element trifft, und der Rechte-fehlt-Pfad.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.uiauto import engine


class FakeEl:
    def __init__(self, role, title="", value="", enabled=True):
        self.AXRole = role
        self.AXTitle = title
        self.AXValue = value
        self.AXEnabled = enabled
        self.pressed = 0
        self.last_action = None

    def Press(self):
        self.pressed += 1
        self.last_action = "Press"


def _patch(monkey_els, trusted=True):
    engine._ax_is_trusted = lambda: trusted
    engine._get_app = lambda app=None: ("APP", app)
    engine._raw_elements = lambda appobj: monkey_els
    engine._wake = lambda appobj: None


def test_is_trusted_delegates():
    engine._ax_is_trusted = lambda: False
    assert engine.is_trusted() is False
    engine._ax_is_trusted = lambda: True
    assert engine.is_trusted() is True


def test_snapshot_filters_roles_and_assigns_refs():
    els = [
        FakeEl("AXButton", "OK"),
        FakeEl("AXUnknown", "ignore me"),       # nicht in Whitelist → raus
        FakeEl("AXTextField", "Suche", value="hallo"),
        FakeEl("AXStaticText", "nur Text"),      # nicht aktionabel → raus
    ]
    _patch(els)
    snap = engine.snapshot("Notizen")
    assert [e["ref"] for e in snap] == [0, 1]
    assert snap[0] == {
        "ref": 0,
        "role": "AXButton",
        "title": "OK",
        "value": "",
        "enabled": True,
        "visible": True,
    }
    assert snap[1]["role"] == "AXTextField" and snap[1]["value"] == "hallo"


def test_snapshot_exposes_visibility_and_treats_unknown_as_visible():
    visible = FakeEl("AXButton", "Save")
    visible.AXVisible = True
    hidden = FakeEl("AXButton", "Send")
    hidden.AXVisible = False
    unknown = FakeEl("AXButton", "Publish")
    _patch([visible, hidden, unknown])

    snapshot = engine.snapshot()

    assert [element["visible"] for element in snapshot] == [True, False, True]


def test_act_targets_correct_element():
    els = [FakeEl("AXButton", "A"), FakeEl("AXButton", "B")]
    _patch(els)
    engine.snapshot()          # füllt internen Speicher
    engine.act(1)              # zweiter Button
    assert els[1].pressed == 1 and els[0].pressed == 0


def test_act_invalid_ref_raises():
    els = [FakeEl("AXButton", "A")]
    _patch(els)
    engine.snapshot()
    try:
        engine.act(5)
        assert False, "sollte werfen"
    except engine.UIAutoError:
        pass


def test_snapshot_without_trust_raises():
    _patch([], trusted=False)
    try:
        engine.snapshot()
        assert False, "sollte ohne Recht werfen"
    except engine.UIAutoError as e:
        assert "bedienungshilfe" in str(e).lower() or "recht" in str(e).lower()


def test_type_and_key_call_helpers():
    calls = []
    engine._do_type = lambda t: calls.append(("type", t))
    engine._do_key = lambda c: calls.append(("key", c))
    engine.type_text("hallo")
    engine.press_key("cmd+k")
    assert calls == [("type", "hallo"), ("key", "cmd+k")]
