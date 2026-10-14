"""Speichert die Prozessidentität und ersetzt sich dann durch codex-task."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

try:
    from .process import process_identity
    from .state import locked_state, write_locked_state
except ImportError:  # Ausführung als direktes Python-Skript
    from process import process_identity
    from state import locked_state, write_locked_state


_REMOTE_ID = re.compile(r"^remote-[a-f0-9]{12}$")


def main(
    argv: list[str] | None = None,
    *,
    getpid=os.getpid,
    getpgrp=os.getpgrp,
) -> int:
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
        with locked_state(state_path) as (state_file, state):
            if state.get("remote_id") != remote_id or state.get("status") != "starting":
                return 2
            pid = getpid()
            pgid = getpgrp()
            if state.get("pid") not in {None, pid} or state.get("pgid") not in {None, pgid}:
                return 2
            identity = process_identity(pid)
            if not identity:
                return 2
            saved_identity = state.get("process_identity")
            if saved_identity not in {None, identity}:
                return 2
            state.update(pid=pid, pgid=pgid, process_identity=identity)

            # Compare-and-set direkt vor execve: Recovery darf den Startstatus
            # während dieser Prüfung nicht auf interrupted setzen.
            if (
                state.get("status") != "starting"
                or state.get("remote_id") != remote_id
                or state.get("process_identity") != identity
            ):
                return 2
            state["status"] = "running"
            write_locked_state(state_file, state)
            os.environ.pop("GIT_EXTERNAL_DIFF", None)
            os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
            os.environ["GIT_ATTR_NOSYSTEM"] = "1"
            try:
                os.execve(binary, [binary, *task_args], os.environ)
            except OSError:
                state.update(status="failed", ended_at=time.time())
                write_locked_state(state_file, state)
                return 1
    except (OSError, TypeError, ValueError):
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
