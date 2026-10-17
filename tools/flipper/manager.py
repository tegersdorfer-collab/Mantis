"""Flipper-Manager — high-level IR-Steuerung für Mantis.

Kapselt den Serial-Treiber, serialisiert Zugriffe (ein Lock, der Port ist exklusiv)
und löst benannte Fernbedienungs-Signale aus `remotes.json` in konkrete `ir tx`-
Befehle auf. So kann Mantis "Lampe an" sagen, ohne Protokoll/Adresse/Befehl zu kennen.
Neue Geräte werden einfach in remotes.json ergänzt (kein Code-Change, kein Neustart).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from . import driver

log = logging.getLogger("tools.flipper")

_REMOTES_PATH = os.path.join(os.path.dirname(__file__), "remotes.json")


def load_remotes() -> dict:
    """remotes.json bei jedem Aufruf frisch lesen (Live-Edit ohne Neustart)."""
    try:
        with open(_REMOTES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("remotes.json nicht lesbar: %s", e)
        return {}


def _app_blocks_cli(out: str) -> bool:
    return "other application is running" in out.lower()


class FlipperManager:
    """Prozessweiter Halter für die Flipper-IR-Steuerung."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        return driver.find_port() is not None

    async def send_ir(self, protocol: str, address: str, command: str) -> str:
        async with self._lock:
            out = await driver.ir_tx(protocol, address, command)
            if _app_blocks_cli(out):
                # Eine offene Flipper-App (z.B. "Passport" nach Tastendruck am Gerät)
                # blockiert IR. Der Flipper gehört Mantis: App schließen, einmal neu senden.
                log.info("Flipper-App blockiert IR, schließe sie: %s", out.strip())
                await driver.talk("loader close", 1.0)
                out = await driver.ir_tx(protocol, address, command)
        low = out.lower()
        if (
            _app_blocks_cli(out)
            or "error" in low or "usage:" in low or "not found" in low
        ):
            raise RuntimeError(out.strip() or "IR-Sendefehler")
        return out

    async def send_named(self, remote: str, signal: str) -> None:
        """Ein benanntes Signal (z.B. remote='schreibtischlampe', signal='an') senden."""
        remotes = load_remotes()
        r = remotes.get(remote)
        if not r:
            raise RuntimeError(f"Unbekannte Fernbedienung '{remote}'.")
        sig = (r.get("signals") or {}).get(signal)
        if not sig:
            raise RuntimeError(f"Signal '{signal}' für '{remote}' nicht hinterlegt.")
        await self.send_ir(sig["protocol"], sig["address"], sig["command"])


# Prozessweiter Singleton — von den Mantis-Tools importiert.
MANAGER = FlipperManager()
