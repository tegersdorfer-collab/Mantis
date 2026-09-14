"""
Restart-Watchdog: wird von Mantis als Subprocess gespawnt.
Überlebt den Mantis-Neustart, prüft Gesundheit, rollt zurück bei Fehler.

Aufruf: python3 scripts/restart_watchdog.py <mantis_pid> <rollback_commit> [<logfile>]
"""
import os
import sys
import time
import signal
import subprocess
import urllib.request

MANTIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MANTIS_DIR)
from web.client_auth import dashboard_url, dashboard_headers
HEALTH_TIMEOUT = 90   # Sekunden bis Mantis hochkommen muss
CHECK_INTERVAL = 3


def log(msg: str):
    print(f"[watchdog] {msg}", flush=True)


def mantis_healthy() -> bool:
    try:
        request = urllib.request.Request(f"{dashboard_url()}/api/status", headers=dashboard_headers())
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def start_mantis(logfile: str) -> subprocess.Popen:
    with open(logfile, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=MANTIS_DIR,
            stdout=lf, stderr=lf,
        )
    log(f"Mantis gestartet (PID {proc.pid})")
    return proc


def git_rollback(commit: str):
    log(f"Rollback zu Commit {commit[:8]}…")
    subprocess.run(["git", "reset", "--hard", commit], cwd=MANTIS_DIR, capture_output=True)


def main():
    if len(sys.argv) < 3:
        print("Usage: restart_watchdog.py <old_pid> <rollback_commit> [logfile]")
        sys.exit(1)

    old_pid     = int(sys.argv[1])
    rollback_to = sys.argv[2]
    logfile     = sys.argv[3] if len(sys.argv) > 3 else "/tmp/mantis_out.log"

    log(f"Starte — beende alten Mantis (PID {old_pid}), Rollback-Commit: {rollback_to[:8]}")

    # 1. Alten Mantis beenden
    try:
        os.kill(old_pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.5)
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(old_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    log("Alter Prozess beendet")

    # 2. Neuen Mantis starten
    proc = start_mantis(logfile)
    time.sleep(5)  # Startzeit

    # 3. Health-Check
    deadline = time.time() + HEALTH_TIMEOUT
    healthy  = False
    while time.time() < deadline:
        if mantis_healthy():
            healthy = True
            break
        if proc.poll() is not None:
            log("Mantis abgestürzt!")
            break
        time.sleep(CHECK_INTERVAL)

    if healthy:
        log("✅ Mantis läuft gesund — Update erfolgreich")
        return

    # 4. Rollback
    log("❌ Health-Check fehlgeschlagen — Rollback wird eingeleitet")
    try:
        proc.kill()
    except Exception:
        pass
    time.sleep(2)

    git_rollback(rollback_to)

    log("Starte Mantis mit alter Version…")
    start_mantis(logfile)

    # Kurze Verifikation nach Rollback
    time.sleep(8)
    if mantis_healthy():
        log("✅ Rollback erfolgreich — Mantis läuft mit alter Version")
    else:
        log("⚠️  Rollback-Start braucht mehr Zeit — Watchdog beendet sich")


if __name__ == "__main__":
    main()
