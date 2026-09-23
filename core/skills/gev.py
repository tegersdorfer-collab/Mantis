"""Agent-Steuerung des lokal geöffneten God's-Eye-View-Globus."""

from core import tools as T
from core.gev_control import GEV_BUS


def _outcome(result: dict, success: str) -> str:
    if result.get("ok") is True:
        return success
    return f"FEHLER: God’s Eye View konnte den Befehl nicht ausführen: {result.get('error', 'unbekannt')}"


@T.register(
    "gev_fly_to",
    "Steuert God's Eye View: fliegt auf dem 3D-Globus zu einem Ort. Nutze dies bei "
    "'flieg nach Berlin', 'zeige Tokio im God's Eye View' und ähnlichen Befehlen.",
    {"place": {"type": "string", "description": "Stadt, Land oder Sehenswürdigkeit"}},
    ["place"], "gev",
)
async def _fly_to(place: str):
    result = await GEV_BUS.dispatch("fly_to_location", {"query": place})
    return _outcome(result, f"God’s Eye View fliegt zu {place}.")


@T.register(
    "gev_set_layer",
    "Schaltet eine Datenebene in God's Eye View ein oder aus. Layer-IDs: flights, "
    "military, satellites, earthquakes, ais-live-vessels (Schiffe), cctv, traffic, "
    "rocket-launches, local-firms (Brände).",
    {"layer": {"type": "string", "description": "Kanonische Layer-ID"},
     "enabled": {"type": "boolean", "description": "true zum Einschalten, false zum Ausschalten"}},
    ["layer", "enabled"], "gev",
)
async def _set_layer(layer: str, enabled: bool):
    result = await GEV_BUS.dispatch("set_layer_visibility", {"layerId": layer, "enabled": enabled})
    return _outcome(result, f"GEV-Ebene {layer} {'eingeschaltet' if enabled else 'ausgeschaltet'}.")


@T.register(
    "gev_set_style",
    "Ändert die Darstellung von God's Eye View: normal, retro, surveillance, "
    "thermal, anime, noir oder snow.",
    {"style": {"type": "string", "description": "Name der Darstellung"}},
    ["style"], "gev",
)
async def _set_style(style: str):
    result = await GEV_BUS.dispatch("set_visual_style", {"style": style})
    return _outcome(result, f"God’s Eye View zeigt jetzt {style}.")


@T.register(
    "gev_reset_view",
    "Setzt God’s Eye View auf die Gesamtansicht der Erde zurück.",
    {}, [], "gev",
)
async def _reset_view():
    result = await GEV_BUS.dispatch("zoom_to_globe", {})
    return _outcome(result, "God’s Eye View zeigt wieder die ganze Erde.")
