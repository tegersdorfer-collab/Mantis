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


def test_gym_seed_script_passes_parsed_ap_export_to_preview(tmp_path, monkeypatch, capsys):
    from scripts import gym_seed_plan

    export = tmp_path / "export.txt"
    export.write_text(
        '"Upper A · Day 2 · Week 7 · New plan";"2026-10-05 13:26 h";"1 hr"\n'
        '"1. Bench Press · Barbell · 4 reps"\n#;KG;REPS\n1;80;4\n',
        encoding="utf-8",
    )
    seen = {}

    def fake_preview(name, ap_workouts=None):
        seen["workouts"] = ap_workouts
        return {"name": name, "start_date": "2026-08-19", "days": [{
            "name": "Upper A", "exercises": [
                {"sort_order": 0, "name": "Bench Press", "target_sets": 3,
                 "rep_min": 4, "rep_max": 4, "target_unit": "reps"},
                {"sort_order": 1, "name": "Weighted Deadhang", "target_sets": 2,
                 "rep_min": 30, "rep_max": 40, "target_unit": "seconds"},
            ],
        }]}

    monkeypatch.setattr(gym_seed_plan.plans, "preview_seed", fake_preview)

    assert gym_seed_plan.main(["--ap-export", str(export)]) == 0

    assert [item.target_reps for item in seen["workouts"][0].exercises] == [4]
    output = capsys.readouterr().out
    assert "3 Sätze · 4 Wdh." in output
    assert "2 Sätze · 30–40 s" in output
