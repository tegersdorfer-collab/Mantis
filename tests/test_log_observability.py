"""Tests für die Fehler-Observability-Brücke (core/log_observability.py):
WARNING+ landet im events_log, mit Dedup, Rekursionsschutz und Crash-Sicherheit.
db.log_event ist gemockt.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import log_observability
from core.log_observability import DBLogHandler

pytestmark = pytest.mark.db


def _record(name="mantis.test", level=logging.WARNING, msg="etwas kaputt"):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def _capture(monkeypatch):
    events = []
    monkeypatch.setattr("core.db.log_event", lambda t, s, d=None: events.append((t, s, d)))
    return events


def test_warning_written(monkeypatch):
    events = _capture(monkeypatch)
    DBLogHandler().emit(_record(level=logging.WARNING))
    assert len(events) == 1 and events[0][0] == "error"


def test_error_written(monkeypatch):
    events = _capture(monkeypatch)
    DBLogHandler().emit(_record(level=logging.ERROR, msg="fatal"))
    assert len(events) == 1


def test_info_ignored(monkeypatch):
    events = _capture(monkeypatch)
    DBLogHandler().emit(_record(level=logging.INFO))
    assert events == []


def test_db_layer_logs_not_written_back(monkeypatch):
    events = _capture(monkeypatch)
    DBLogHandler().emit(_record(name="core.db", level=logging.ERROR))
    assert events == []  # Rekursionsschutz


def test_dedup_same_signature(monkeypatch):
    events = _capture(monkeypatch)
    h = DBLogHandler()
    h.emit(_record(msg="wiederholt"))
    h.emit(_record(msg="wiederholt"))
    h.emit(_record(msg="wiederholt"))
    assert len(events) == 1  # nur einmal trotz 3 gleicher Fehler


def test_different_signatures_both_written(monkeypatch):
    events = _capture(monkeypatch)
    h = DBLogHandler()
    h.emit(_record(msg="fehler A"))
    h.emit(_record(msg="fehler B"))
    assert len(events) == 2


def test_handler_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("DB weg")
    monkeypatch.setattr("core.db.log_event", boom)
    # darf NICHT propagieren
    DBLogHandler().emit(_record(level=logging.ERROR))


def test_install_is_idempotent():
    root = logging.getLogger()
    before = len(root.handlers)
    h1 = log_observability.install()
    h2 = log_observability.install()
    try:
        assert h1 is h2
        assert sum(isinstance(h, DBLogHandler) for h in root.handlers) == 1
    finally:
        root.removeHandler(h1)
    assert len(root.handlers) == before
