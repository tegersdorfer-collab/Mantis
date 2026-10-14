"""Tests für ProactiveTracker (proactive.py): Nacht-Modus-Fenster (22:30–06:30),
Tageswechsel-Reset und Sende-Zählung. Reine Logik, kein LLM/DB nötig.
"""

import asyncio
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proactive
from proactive import ProactiveEngine, ProactiveTracker


def _dt(h, m=0):
    return datetime(2026, 7, 9, h, m)


# ── Nacht-Modus ───────────────────────────────────────────────────────────────

def test_is_night_blocks_late_evening():
    assert ProactiveTracker._is_night(_dt(23, 0)) is True
    assert ProactiveTracker._is_night(_dt(22, 30)) is True  # Grenze inklusiv


def test_is_night_blocks_early_morning():
    assert ProactiveTracker._is_night(_dt(3, 0)) is True
    assert ProactiveTracker._is_night(_dt(6, 29)) is True


def test_is_night_allows_daytime():
    assert ProactiveTracker._is_night(_dt(6, 30)) is False  # Ende exklusiv → erlaubt
    assert ProactiveTracker._is_night(_dt(12, 0)) is False
    assert ProactiveTracker._is_night(_dt(22, 29)) is False


# ── Tageswechsel-Reset ────────────────────────────────────────────────────────

def test_count_resets_on_new_day():
    t = ProactiveTracker()
    t._count = 5
    t._today = date(2020, 1, 1)  # künstlich alter Tag
    assert t.count_today == 0     # Zugriff triggert Reset


def test_count_persists_same_day():
    t = ProactiveTracker()
    t._today = date.today()
    t._count = 3
    assert t.count_today == 3


# ── record_sent ───────────────────────────────────────────────────────────────

def test_record_sent_increments_and_stamps():
    t = ProactiveTracker()
    t._today = date.today()
    t._count = 0
    before = datetime.now()
    t.record_sent()
    assert t._count == 1
    assert t._last_sent is not None and t._last_sent >= before


# ── Intervall-Gate (can_send) ─────────────────────────────────────────────────

def test_can_send_blocked_when_recent(monkeypatch):
    t = ProactiveTracker()
    t._today = date.today()
    monkeypatch.setattr(t, "_check_stale_ignore", lambda: None)
    monkeypatch.setattr(t, "_interval", lambda: 3600)
    monkeypatch.setattr(t, "_is_night", staticmethod(lambda now: False))
    t._last_sent = datetime.now()  # gerade eben gesendet
    assert t.can_send() is False   # Mindestabstand nicht erreicht


def test_proactive_prompt_excludes_agent_remote_chat_history(monkeypatch):
    import core.db

    captured = {}

    class Lzg:
        def get_all(self, limit):
            return []

        def format_for_context(self, memories):
            return "Keine Erinnerungen"

    class Llm:
        async def chat(self, messages, **_kwargs):
            captured["prompt"] = messages[0].content
            return "Gedanke"

    rows = [
        {
            "role": "assistant", "content": "Agent-Remote remote-123456789abc: erledigt",
            "channel": "discord", "meta": {},
        },
        {
            "role": "assistant", "content": "Agent-Ergebnis: früherer Payload",
            "channel": "discord", "meta": {"use_case": "agent"},
        },
        {
            "role": "user", "content": "codex: mantis analysiere Dateien",
            "channel": "discord", "meta": {"use_case": "agent"},
        },
        {
            "role": "user", "content": "Sicherer Gesprächskontext",
            "channel": "telegram", "meta": {},
        },
    ]
    monkeypatch.setattr(proactive, "_dashboard", None)
    monkeypatch.setattr(core.db, "query", lambda *_args, **_kwargs: rows)

    result = asyncio.run(ProactiveEngine(Llm(), Lzg()).generate())

    assert result == "Gedanke"
    assert "Sicherer Gesprächskontext" in captured["prompt"]
    assert "Agent-Remote remote-123456789abc" not in captured["prompt"]
    assert "früherer Payload" not in captured["prompt"]
    assert "codex: mantis analysiere Dateien" not in captured["prompt"]
