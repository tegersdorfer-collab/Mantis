"""Prozessidentitäten und sichere Signale für Agent Remote."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Mapping


class _ProcBSDInfo(ctypes.Structure):
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


def _darwin_process_info(pid: int) -> _ProcBSDInfo | None:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.POINTER(_ProcBSDInfo),
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = _ProcBSDInfo()
        size = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info) or info.pbi_pid != pid:
            return None
        return info
    except (AttributeError, OSError, ValueError):
        return None


def process_identity(pid: int) -> str | None:
    """Liefert eine Start-ID, die bei wiederverwendeter PID nicht mehr passt."""
    if sys.platform == "darwin":
        info = _darwin_process_info(pid)
        if info is None:
            return None
        return f"darwin:{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
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


def process_group_identities(pgid: int) -> dict[str, str] | None:
    """Liefert einen stabilen Snapshot aller lesbaren Prozessidentitäten einer Gruppe."""
    if not isinstance(pgid, int) or pgid <= 0:
        return None
    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            list_pids = libproc.proc_listallpids
            list_pids.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
            list_pids.restype = ctypes.c_int
            count = list_pids(None, 0)
            if count <= 0:
                return None
            pids = (ctypes.c_int * (count + 1024))()
            found = list_pids(pids, ctypes.sizeof(pids))
            if found <= 0 or found >= len(pids):
                return None
        except (AttributeError, OSError, ValueError):
            return None

        identities = {}
        for pid in pids[:found]:
            if pid <= 0:
                continue
            try:
                current_pgid = os.getpgid(pid)
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                return None
            if current_pgid != pgid:
                continue
            info = _darwin_process_info(pid)
            if info is None or info.pbi_pgid != pgid:
                return None
            identity = f"darwin:{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
            if process_identity(pid) != identity:
                return None
            try:
                if os.getpgid(pid) != pgid:
                    continue
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                return None
            identities[str(pid)] = identity
        return identities

    if sys.platform.startswith("linux"):
        identities = {}
        try:
            proc_entries = tuple(Path("/proc").iterdir())
        except OSError:
            return None
        for entry in proc_entries:
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                stat_text = (entry / "stat").read_text(encoding="ascii")
                fields = stat_text[stat_text.rfind(")") + 2:].split()
                if len(fields) <= 19 or int(fields[2]) != pgid:
                    continue
                identity = f"linux:{fields[19]}"
                stat_again = (entry / "stat").read_text(encoding="ascii")
                fields_again = stat_again[stat_again.rfind(")") + 2:].split()
                if (
                    len(fields_again) <= 19
                    or int(fields_again[2]) != pgid
                    or f"linux:{fields_again[19]}" != identity
                ):
                    return None
                identities[str(pid)] = identity
            except (OSError, IndexError, ValueError):
                try:
                    if os.getpgid(pid) == pgid:
                        return None
                except ProcessLookupError:
                    continue
                except (PermissionError, OSError):
                    return None
        return identities

    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin", "HOME": "/", "LANG": "C"},
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    pids = set()
    try:
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                return None
            pid, member_pgid = map(int, fields)
            if member_pgid == pgid:
                pids.add(pid)
    except ValueError:
        return None

    identities = {}
    for pid in pids:
        before = process_identity(pid)
        try:
            member_pgid = os.getpgid(pid)
        except ProcessLookupError:
            continue
        except (PermissionError, OSError):
            return None
        if member_pgid != pgid:
            continue
        if not before or process_identity(pid) != before:
            return None
        identities[str(pid)] = before
    return identities


def _snapshot_covers_current_group(
    current: Mapping[str, str] | None,
    verified: Mapping[str, str],
) -> bool:
    if not current:
        return False
    return all(
        identity and verified.get(str(pid)) == identity
        for pid, identity in current.items()
    )


def signal_verified_process_group(
    pid: int,
    pgid: int,
    kind: str,
    expected_identity: str,
    verified_members: Mapping[str, str] | None = None,
) -> bool:
    """Signale nur einen Lauf mit verifiziertem Leader und Gruppen-Snapshot."""
    if not isinstance(pid, int) or pid <= 0 or pgid != pid or not expected_identity:
        return False
    if not process_group_exists(pgid):
        return True

    if (
        not isinstance(verified_members, Mapping)
        or verified_members.get(str(pid)) != expected_identity
        or not _snapshot_covers_current_group(
            process_group_identities(pgid), verified_members,
        )
    ):
        return False

    try:
        leader_pgid = os.getpgid(pid)
    except ProcessLookupError:
        leader_pgid = None
    except (PermissionError, OSError):
        return False

    if leader_pgid is None:
        if kind not in {"TERM_ORPHAN", "KILL"}:
            return False
    else:
        leader_identity = process_identity(pid)
        if leader_pgid != pgid or leader_identity != expected_identity:
            return False
        if kind == "TERM_ORPHAN":
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
