"""Befehle für den lokal eingebundenen God's-Eye-View-Globus."""

import asyncio

import pytest

from core.gev_control import GevCommandBus, normalize_command


def test_normalize_command_beschraenkt_aktionen_und_argumente():
    assert normalize_command("fly_to_location", {"query": " Berlin "}) == (
        "fly_to_location", {"query": "Berlin"})
    assert normalize_command("set_layer_visibility", {"layerId": "flights", "enabled": True}) == (
        "set_layer_visibility", {"layerId": "flights", "enabled": True})
    with pytest.raises(ValueError):
        normalize_command("set_layer_visibility", {"layerId": "../../api", "enabled": True})
    with pytest.raises(ValueError):
        normalize_command("fly_to_location", {"query": ""})
    with pytest.raises(ValueError):
        normalize_command("set_visual_style", {"style": "unknown"})


def test_dispatch_wartet_auf_bestaetigte_ausfuehrung():
    async def run():
        bus = GevCommandBus()
        q = bus.subscribe()
        task = asyncio.create_task(bus.dispatch("zoom_to_globe", {}, timeout=0.5))
        command = await asyncio.wait_for(q.get(), 0.2)
        assert command["action"] == "zoom_to_globe"
        assert bus.ack(command["id"], {"ok": True, "action": "zoom_to_globe"})
        assert (await task)["ok"] is True
        assert not bus.ack(command["id"], {"ok": True})
        bus.unsubscribe(q)

    asyncio.run(run())


def test_dispatch_ohne_aktiven_client_scheitert():
    async def run():
        bus = GevCommandBus()
        with pytest.raises(RuntimeError, match="Dashboard"):
            await bus.dispatch("zoom_to_globe", {})

    asyncio.run(run())


def test_dispatch_laeuft_bei_fehlender_quittierung_ab():
    async def run():
        bus = GevCommandBus()
        q = bus.subscribe()
        with pytest.raises(TimeoutError):
            await bus.dispatch("zoom_to_globe", {}, timeout=0.01)
        bus.unsubscribe(q)

    asyncio.run(run())
