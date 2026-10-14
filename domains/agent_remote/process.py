"""Prozessidentitäten und sichere Signale für Agent Remote."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path


def process_identity(pid: int) -> str | None:
    """Liefert eine Start-ID, die bei wiederverwendeter PID nicht mehr passt."""
    if sys.platform == "darwin":
        class ProcBSDInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.POINTER(ProcBSDInfo),
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            info = ProcBSDInfo()
            size = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info) or info.pbi_pid != pid:
                return None
            return f"darwin:{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
        except (AttributeError, OSError, ValueError):
            return None
    if sys.platform.startswith("linux"):
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat_text[stat_text.rfind(")") + 2:].split()
            return f"linux:{fields[19]}" if len(fields) > 19 else None
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "LANG": "C"},
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identity = result.stdout.strip()
    return identity if result.returncode == 0 and identity else None


def process_group_exists(pgid: int) -> bool:
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def signal_verified_process_group(pid: int, pgid: int, kind: str, expected_identity: str) -> bool:
    """Signale nach Identitätsprüfung; KILL bleibt für die verifizierte Gruppe möglich."""
    if not isinstance(pid, int) or pid <= 0 or pgid != pid or not expected_identity:
        return False
    if not process_group_exists(pgid):
        return True
    # Die Identität des Gruppenleiters wurde beim Start gespeichert. Solange die Gruppe
    # existiert, kann der Kernel die PID nicht neu vergeben; ist der Leader tot, gehört die
    # Gruppe daher weiterhin dem verifizierten Lauf. Hält dagegen nachweislich ein anderer
    # Prozess die PID/PGID, wird nie signalisiert.
    try:
        leader_identity = process_identity(pid)
        if leader_identity is None:
            try:
                if os.getpgid(pid) != pgid:
                    return False
            except ProcessLookupError:
                pass
        elif leader_identity != expected_identity or os.getpgid(pid) != pgid:
            return False
    except (ProcessLookupError, PermissionError, OSError):
        return False
    if kind in {"TERM", "TERM_ORPHAN"}:
        sig = signal.SIGTERM
    elif kind == "KILL":
        sig = signal.SIGKILL
    else:
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True
