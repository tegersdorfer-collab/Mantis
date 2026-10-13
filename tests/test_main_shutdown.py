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
