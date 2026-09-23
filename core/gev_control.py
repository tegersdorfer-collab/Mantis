"""Begrenzter Befehlskanal zwischen Mantis und dem lokalen GEV-Fenster."""

import asyncio
from concurrent.futures import Future
from threading import RLock
from uuid import uuid4


LAYERS = frozenset({
    "flights", "military", "earthquakes", "satellites", "rocket-launches",
    "traffic", "cctv", "radio", "bikeshare", "ais-live-vessels",
    "local-datacenters", "local-dams", "telegeography-submarine-cables",
    "local-firms", "alpr-cameras",
})
STYLES = frozenset({"normal", "retro", "surveillance", "thermal", "anime", "noir", "snow"})


def normalize_command(action: str, args: dict) -> tuple[str, dict]:
    """Nur die im Mantis-Tool freigegebenen GEV-Aktionen passieren lassen."""
    if not isinstance(args, dict):
        raise ValueError("Ungültige Argumente")
    if action == "fly_to_location":
        query = args.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
            raise ValueError("Ort fehlt oder ist zu lang")
        return action, {"query": query.strip()}
    if action == "set_layer_visibility":
        layer = args.get("layerId")
        enabled = args.get("enabled")
        if layer not in LAYERS or not isinstance(enabled, bool):
            raise ValueError("Unbekannte Ebene oder ungültiger Status")
        return action, {"layerId": layer, "enabled": enabled}
    if action == "set_visual_style":
        style = args.get("style")
        if style not in STYLES:
            raise ValueError("Unbekannte Darstellung")
        return action, {"style": style}
    if action == "zoom_to_globe":
        return action, {}
    raise ValueError("Unbekannte Globus-Aktion")


class GevCommandBus:
    def __init__(self) -> None:
        self._lock = RLock()
        self._listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._pending: dict[str, Future] = {}

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        with self._lock:
            self._listeners.append((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._listeners = [item for item in self._listeners if item[1] is not queue]

    async def dispatch(self, action: str, args: dict, timeout: float = 15) -> dict:
        action, args = normalize_command(action, args)
        command_id = uuid4().hex
        result: Future = Future()
        command = {"id": command_id, "action": action, "args": args}
        with self._lock:
            if not self._listeners:
                raise RuntimeError("Kein Mantis-Dashboard für den Globus verbunden")
            loop, queue = self._listeners[-1]
            self._pending[command_id] = result
            loop.call_soon_threadsafe(queue.put_nowait, command)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(result), timeout)
        finally:
            with self._lock:
                self._pending.pop(command_id, None)

    def ack(self, command_id: str, result: dict) -> bool:
        with self._lock:
            pending = self._pending.get(command_id)
            if pending is None or pending.done():
                return False
            pending.set_result(result)
            return True


GEV_BUS = GevCommandBus()
