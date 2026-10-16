"""Tests für die Flipper-IR-Steuerung (tools/flipper/) — ohne echten Flipper.

Der Serial-Treiber wird gemockt; geprüft wird die Auflösung benannter Signale
aus remotes.json in konkrete `ir tx`-Argumente sowie die Fehlerbehandlung.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.flipper import driver
from tools.flipper.manager import FlipperManager, load_remotes


def _patch_tx(monkey_calls):
    async def fake_ir_tx(protocol, address, command):
        monkey_calls.append((protocol, address, command))
        return ">: "  # sauberer Prompt, kein Fehler
    driver.ir_tx = fake_ir_tx  # type: ignore[assignment]


def test_lamp_remote_is_configured():
    remotes = load_remotes()
    assert "schreibtischlampe" in remotes
    sig = remotes["schreibtischlampe"]["signals"]
    assert sig["an"] == {"protocol": "NECext", "address": "EF00", "command": "FC03"}
    assert sig["aus"] == {"protocol": "NECext", "address": "EF00", "command": "FD02"}


def test_all_lamp_signals_present():
    sig = load_remotes()["schreibtischlampe"]["signals"]
    expected = {
        "heller": "FF00", "dunkler": "FE01", "weiss": "F807", "warmweiss": "F40B",
        "rot": "FB04", "gruen": "FA05", "blau": "F906",
    }
    for name, cmd in expected.items():
        assert sig[name] == {"protocol": "NECext", "address": "EF00", "command": cmd}


def test_lamp_tool_aliases_and_steps():
    """Das registrierte lampe-Tool: Umlaut-Alias + heller/dunkler-Wiederholung."""
    calls: list = []
    _patch_tx(calls)
    from core.skills.flipper import _lampe

    asyncio.run(_lampe("grün"))          # Alias → gruen
    assert calls[-1] == ("NECext", "EF00", "FA05")

    calls.clear()
    asyncio.run(_lampe("heller", schritte=3))
    assert calls == [("NECext", "EF00", "FF00")] * 3   # 3 Dimm-Stufen

    calls.clear()
    asyncio.run(_lampe("heller", schritte=10))   # Lampe hat nur 3 Stufen
    assert len(calls) == 3

    calls.clear()
    asyncio.run(_lampe("weiß"))          # Alias → weiss
    assert calls[-1] == ("NECext", "EF00", "F807")


def test_send_named_resolves_signal():
    calls: list = []
    _patch_tx(calls)
    m = FlipperManager()
    asyncio.run(m.send_named("schreibtischlampe", "an"))
    assert calls == [("NECext", "EF00", "FC03")]
    asyncio.run(m.send_named("schreibtischlampe", "aus"))
    assert calls[-1] == ("NECext", "EF00", "FD02")


def test_unknown_remote_raises():
    calls: list = []
    _patch_tx(calls)
    m = FlipperManager()
    try:
        asyncio.run(m.send_named("nixgibtsnicht", "an"))
        assert False, "sollte werfen"
    except RuntimeError as e:
        assert "Unbekannte" in str(e)


def test_unknown_signal_raises():
    calls: list = []
    _patch_tx(calls)
    m = FlipperManager()
    try:
        asyncio.run(m.send_named("schreibtischlampe", "blinken"))
        assert False, "sollte werfen"
    except RuntimeError as e:
        assert "nicht hinterlegt" in str(e)


def test_send_ir_detects_error_output():
    async def bad_tx(protocol, address, command):
        return "Usage:\n\tir tx <protocol> <address> <command>"
    driver.ir_tx = bad_tx  # type: ignore[assignment]
    m = FlipperManager()
    try:
        asyncio.run(m.send_ir("NECext", "EF00", "FC03"))
        assert False, "sollte werfen"
    except RuntimeError:
        pass
