import asyncio
import logging

import main


class _Component:
    def __init__(self, error=None):
        self.error = error
        self.stopped = False

    async def stop(self):
        if self.error:
            raise self.error
        self.stopped = True


def test_shutdown_continues_after_a_component_stop_fails(caplog):
    broken = _Component(RuntimeError("updater already stopped"))
    later = _Component()

    with caplog.at_level(logging.WARNING, logger="mantis"):
        asyncio.run(main._stop_components([("Orchestrator", broken), ("Telegram", later)]))

    assert later.stopped is True
    assert "Orchestrator" in caplog.text
    assert "RuntimeError" in caplog.text


def test_shutdown_services_stops_klon_and_signals_event_when_a_stop_fails(caplog):
    calls = []

    async def stop_tts_worker():
        calls.append("tts")
        raise RuntimeError("worker already gone")

    async def stop_gym_server():
        calls.append("gym")

    stop_event = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="mantis"):
        asyncio.run(main._shutdown_services(
            (), stop_tts_worker, stop_gym_server, stop_event,
        ))

    assert calls == ["tts", "gym"]
    assert stop_event.is_set()
    assert "Klon-TTS" in caplog.text
