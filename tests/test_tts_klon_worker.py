"""Lifecycle tests for the isolated Klon worker process."""
import io
import queue
import sys

import pytest

from services.tts_klon import worker


def test_worker_watchdog_exits_when_mantis_parent_disappears(monkeypatch):
    sleeps = []
    parent_ids = iter((1234, 1))

    class _ParentExited(BaseException):
        pass

    monkeypatch.setattr(worker.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(worker.os, "getppid", lambda: next(parent_ids))

    def exit_process(status):
        assert status == 0
        raise _ParentExited

    monkeypatch.setattr(worker.os, "_exit", exit_process)

    with pytest.raises(_ParentExited):
        worker._watch_parent(1234)

    assert sleeps == [2, 2]


def test_worker_exits_cleanly_when_stdin_is_already_eof(monkeypatch):
    class _Model:
        def generate(self, **_kwargs):
            pytest.fail("EOF must exit without starting synthesis")

    monkeypatch.setattr(sys, "argv", ["worker.py"])
    monkeypatch.setattr(worker, "_PROTOCOL_OUT", io.StringIO())
    monkeypatch.setattr(worker, "_start_parent_watchdog", lambda: None)
    requests = queue.Queue()
    requests.put(None)
    monkeypatch.setattr(worker, "_start_stdin_reader", lambda: requests)
    monkeypatch.setattr(
        worker,
        "_load_runtime",
        lambda _ref_dir: (object(), _Model(), "reference.wav", "reference text"),
    )

    assert worker.main() == 0


def test_stdin_reader_exits_process_on_eof(monkeypatch):
    class _WorkerExited(BaseException):
        pass

    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(""))

    def exit_process(status):
        assert status == 0
        raise _WorkerExited

    monkeypatch.setattr(worker.os, "_exit", exit_process)

    with pytest.raises(_WorkerExited):
        worker._read_stdin(queue.Queue())
