"""Der Gym-Plan-Seed lässt sich direkt aus scripts/ starten."""

from pathlib import Path
import subprocess
import sys


def test_gym_seed_script_help_runs_from_project_root():
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "gym_seed_plan.py"), "--help"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout
    assert "--apply" in result.stdout
