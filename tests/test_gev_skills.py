"""Agent-Tools für den GEV-Globus."""

import asyncio
from unittest.mock import AsyncMock, patch

from core import tools
from core.skills import gev


def test_gev_tools_sind_registriert():
    assert {"gev_fly_to", "gev_set_layer", "gev_set_style", "gev_reset_view"} <= tools.REGISTRY.keys()


def test_routing_waehlt_gev_fuer_sprachbefehl():
    assert "gev_fly_to" in tools.select_tools("Flieg nach Berlin im 3D-Globus")
    assert "gev_set_layer" in tools.select_tools("Zeige Satelliten im Globus")


def test_gev_fly_to_meldet_ausfuehrungsfehler_ehrlich():
    with patch.object(gev.GEV_BUS, "dispatch", new_callable=AsyncMock) as dispatch:
        dispatch.return_value = {"ok": False, "error": "Ort nicht gefunden"}
        result = asyncio.run(gev._fly_to("Atlantis"))
    assert "nicht" in result.lower() or "fehler" in result.lower()


def test_gev_set_layer_nutzt_validierten_befehl():
    with patch.object(gev.GEV_BUS, "dispatch", new_callable=AsyncMock) as dispatch:
        dispatch.return_value = {"ok": True}
        result = asyncio.run(gev._set_layer("flights", True))
    dispatch.assert_awaited_once_with("set_layer_visibility", {"layerId": "flights", "enabled": True})
    assert "flights" in result
