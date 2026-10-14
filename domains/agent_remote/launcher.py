"""Speichert die Prozessidentität und ersetzt sich dann durch codex-task."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from process import process_identity


_REMOTE_ID = re.compile(r"^remote-[a-f0-9]{12}$")


def _write_state(path: Path, data: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        return 2
    raw_state, remote_id, binary, *task_args = args
    if not _REMOTE_ID.fullmatch(remote_id) or not Path(binary).is_absolute():
        return 2
    state_path = Path(raw_state)
    if (
        state_path.name != "state.json"
        or state_path.is_symlink()
        or state_path.parent.name != remote_id
        or not state_path.is_file()
    ):
        return 2
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("remote_id") != remote_id or state.get("status") not in {"starting", "running"}:
            return 2
        if state.get("pid") not in {None, os.getpid()}:
            return 2
        identity = process_identity(os.getpid())
        if not identity:
            return 2
        state.update(
            status="running",
            pid=os.getpid(),
            pgid=os.getpgrp(),
            process_identity=identity,
        )
        _write_state(state_path, state)
    except (OSError, TypeError, ValueError):
        return 1

    try:
        os.execve(binary, [binary, *task_args], os.environ)
    except OSError:
        state.update(status="failed", ended_at=time.time())
        _write_state(state_path, state)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
