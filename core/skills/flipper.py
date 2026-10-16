"""Flipper-Zero-Tools — Mantis steuert IR-Geräte (Schreibtischlampe etc.).

Registriert sich via @T.register beim Import (durch core/skills/__init__.py).
Der Flipper hängt per USB am Mac; Mantis schickt IR-Sendebefehle über dessen
Serial-CLI (tools/flipper/). Bekannte Fernbedienungen: tools/flipper/remotes.json.
"""

import logging

from core import tools as T
from tools.flipper.manager import MANAGER

log = logging.getLogger("core.skills")


import asyncio

# Eingabe-Normalisierung: Umlaut-/Synonym-Varianten → Signalname in remotes.json
_LAMP_ALIASES = {
    "an": "an", "ein": "an", "einschalten": "an", "licht an": "an",
    "aus": "aus", "ausschalten": "aus", "off": "aus", "licht aus": "aus",
    "heller": "heller", "heller machen": "heller", "brighter": "heller",
    "dunkler": "dunkler", "dunkler machen": "dunkler", "dimmen": "dunkler", "dimmer": "dunkler",
    "weiss": "weiss", "weiß": "weiss", "white": "weiss", "kaltweiss": "weiss",
    "warmweiss": "warmweiss", "warmweiß": "warmweiss", "warm": "warmweiss", "warmweiss ": "warmweiss",
    "rot": "rot", "red": "rot",
    "gruen": "gruen", "grün": "gruen", "green": "gruen",
    "blau": "blau", "blue": "blau",
}

_LAMP_EMOJI = {
    "an": "💡 Lampe an", "aus": "🌙 Lampe aus",
    "heller": "🔆 heller", "dunkler": "🔅 dunkler",
    "weiss": "⚪ weiß", "warmweiss": "🟠 warmweiß",
    "rot": "🔴 rot", "gruen": "🟢 grün", "blau": "🔵 blau",
}


@T.register(
    "lampe",
    "Steuert Timos Schreibtischlampe per Infrarot über den Flipper Zero: an/aus, heller/"
    "dunkler und Farbe (weiß, warmweiß, rot, grün, blau). Nutze dies wenn Timo das Licht "
    "schalten, dimmen oder die Lichtfarbe ändern will. 'schritte' wiederholt heller/dunkler; 3 Stufen reichen vom Minimum zum "
    "Maximum (\"so hell wie möglich\" = heller mit schritte=3).",
    {
        "action": {
            "type": "string",
            "enum": ["an", "aus", "heller", "dunkler", "weiss", "warmweiss", "rot", "gruen", "blau"],
            "description": "an/aus = schalten, heller/dunkler = dimmen, "
                           "weiss/warmweiss/rot/gruen/blau = Lichtfarbe",
        },
        "schritte": {
            "type": "integer",
            "description": "nur für heller/dunkler: wie viele Dimm-Stufen (Standard 1, max 3 = volle Helligkeit bzw. ganz dunkel)",
        },
    },
    ["action"],
    "flipper",
)
async def _lampe(action: str, schritte: int = 1):
    sig = _LAMP_ALIASES.get(action.strip().lower())
    if sig is None:
        return (f"❌ Unbekannte Lampen-Aktion '{action}'. Möglich: an, aus, heller, dunkler, "
                f"weiss, warmweiss, rot, gruen, blau.")
    reps = 1
    if sig in ("heller", "dunkler"):
        # Die Lampe hat nur drei Stufen; mehr Signale kosten nur Zeit.
        reps = max(1, min(3, int(schritte or 1)))
    try:
        for i in range(reps):
            await MANAGER.send_named("schreibtischlampe", sig)
            if i + 1 < reps:
                await asyncio.sleep(0.18)
    except Exception as e:
        log.warning("lampe fehlgeschlagen: %s", e)
        return f"❌ Lampe nicht erreichbar: {e}. Hängt der Flipper per USB am Mac?"
    label = _LAMP_EMOJI.get(sig, sig)
    return f"{label} (×{reps})" if reps > 1 else label


@T.register(
    "flipper_ir",
    "Sendet ein beliebiges IR-Signal über den Flipper Zero (für angelernte Geräte "
    "jenseits der Lampe). Nutze das benannte 'lampe'-Tool für die Schreibtischlampe; "
    "dieses Tool ist für andere Fernbedienungen aus remotes.json oder rohe Codes.",
    {
        "remote": {"type": "string", "description": "Name der Fernbedienung aus remotes.json (z.B. 'tv')"},
        "signal": {"type": "string", "description": "Signalname innerhalb der Fernbedienung (z.B. 'an', 'lauter')"},
        "protocol": {"type": "string", "description": "Alternativ roh: IR-Protokoll (z.B. NECext, Samsung32)"},
        "address": {"type": "string", "description": "Alternativ roh: Adresse hex (z.B. EF00)"},
        "command": {"type": "string", "description": "Alternativ roh: Befehl hex (z.B. FC03)"},
    },
    [],
    "flipper",
)
async def _flipper_ir(remote: str = "", signal: str = "",
                      protocol: str = "", address: str = "", command: str = ""):
    try:
        if remote and signal:
            await MANAGER.send_named(remote, signal)
            return f"📡 IR gesendet: {remote}/{signal}"
        if protocol and address and command:
            await MANAGER.send_ir(protocol, address, command)
            return f"📡 IR gesendet: {protocol} {address} {command}"
        return "❌ Bitte entweder remote+signal oder protocol+address+command angeben."
    except Exception as e:
        log.warning("flipper_ir fehlgeschlagen: %s", e)
        return f"❌ Flipper nicht erreichbar: {e}. Hängt er per USB am Mac?"
