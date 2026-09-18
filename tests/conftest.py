"""Test-weite Fixtures: .env ist ein Symlink auf die echten Secrets und könnte irgendwann
JEV_ENABLED=true tragen — die Suite darf davon nie abhängen oder echtes Netz anfassen."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import decide


@pytest.fixture(autouse=True)
def _jev_aus(monkeypatch):
    monkeypatch.setattr(decide.config, "JEV_ENABLED", False)
    decide.reset_for_tests()
