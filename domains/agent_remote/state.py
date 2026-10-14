"""Gesperrte Lese- und Schreibzugriffe auf den Agent-Remote-Status."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def locked_state(path: Path, *, create: bool = False) -> Iterator[tuple[object, dict]]:
    """Öffnet und sperrt state.json; der Inode bleibt während Updates erhalten."""
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    file = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        os.fchmod(file.fileno(), 0o600)
        file.seek(0)
        raw = file.read()
        if raw:
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("Agent-Remote-Status ist kein Objekt")
        else:
            state = {}
        yield file, state
    finally:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()


def write_locked_state(file, state: dict) -> None:
    """Schreibt den Status in-place, solange der Aufrufer den Dateilock hält."""
    file.seek(0)
    json.dump(state, file, ensure_ascii=False, sort_keys=True)
    file.write("\n")
    file.truncate()
    file.flush()
    os.fsync(file.fileno())

