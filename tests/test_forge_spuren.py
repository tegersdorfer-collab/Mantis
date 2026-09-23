"""Vertragstests für die optionale Forge-Trace-CLI."""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forge import daemon, freigabe, jev_gate, models as m, pipeline, spuren, stages
from forge.runner import RunResult


def _cli(monkeypatch, ids=None, calls=None):
    ids = iter(ids or [f"{n:032x}" for n in range(1, 30)])
    calls = calls if calls is not None else []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if kwargs.get("timeout") != 10:
            raise AssertionError("tracelog muss nach 10 Sekunden abbrechen")
        return subprocess.CompletedProcess(args, 0, next(ids) + "\n", "")

    monkeypatch.setattr(spuren.subprocess, "run", run)
    return calls


@pytest.fixture(autouse=True)
def _isolierter_state(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_SPUREN_STATE", str(tmp_path / "state.json"))
    cli = tmp_path / "tracelog"
    cli.write_text("fake cli placeholder", encoding="utf-8")
    monkeypatch.setenv("TRACELOG_BIN", str(cli))
    monkeypatch.setenv("FORGE_SPUREN", "1")
    monkeypatch.setattr(spuren, "_konkrete_secret_werte", lambda: ())


def test_record_gibt_eltern_id_weiter_und_haengt_den_neuen_record_an(monkeypatch):
    calls = _cli(monkeypatch)

    erste = spuren.record(42, "stage", input={"stufe": "spec"})
    zweite = spuren.record(42, "stage", input={"stufe": "plan"})

    assert (erste, zweite) == (f"{1:032x}", f"{2:032x}")
    assert "--parent" not in calls[0][0]
    assert calls[1][0][calls[1][0].index("--parent") + 1] == erste
    assert spuren.letzter_record(42) == zweite


def test_blobs_und_inline_daten_werden_vor_dem_cli_geschwaerzt(monkeypatch):
    calls = _cli(monkeypatch)
    gelesene_blobs = []
    original_run = spuren.subprocess.run

    def run(args, **kwargs):
        for argument in args:
            if argument.startswith("prompt="):
                gelesene_blobs.append(Path(argument.split("=", 1)[1]).read_text())
        return original_run(args, **kwargs)

    monkeypatch.setattr(spuren.subprocess, "run", run)
    spuren.record(
        7,
        "stage",
        input={"API_TOKEN": "redaction-sentinel"},
        blobs={"prompt": "SERVICE_SECRET=blob sentinel text"},
    )

    args, kwargs = calls[0]
    payload = json.loads(kwargs["input"])
    assert "redaction-sentinel" not in json.dumps(payload)
    assert "blob sentinel text" not in gelesene_blobs[0]


def test_fehlende_cli_und_timeout_bleiben_still(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACELOG_BIN", "/tmp/tracelog-does-not-exist")
    monkeypatch.setattr(
        spuren.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Fehlende CLI darf nicht gestartet werden"),
    )
    assert spuren.record(1, "stage") is None
    assert spuren.letzter_record(1) is None

    fake_cli = tmp_path / "tracelog"
    fake_cli.write_text("fake cli placeholder", encoding="utf-8")
    monkeypatch.setenv("TRACELOG_BIN", str(fake_cli))
    monkeypatch.setattr(
        spuren.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], 10)),
    )
    assert spuren.record(1, "stage") is None


def test_schalter_aus_startet_keine_cli(monkeypatch):
    monkeypatch.setenv("FORGE_SPUREN", "0")
    monkeypatch.setattr(
        spuren.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("CLI darf bei deaktivierten Spuren nicht starten"),
    )

    assert spuren.record(1, "stage") is None
    assert spuren.verdict(1, "gemerged") is None


def test_verdict_zeigt_auf_letzten_record(monkeypatch):
    calls = _cli(monkeypatch)
    spuren.record(9, "stage")

    neue_id = spuren.verdict(9, "geparkt", note="manuelle Sichtung", score=0.0)

    assert neue_id == f"{2:032x}"
    args, kwargs = calls[1]
    assert args[1:3] == ["verdict", f"{1:032x}"]
    assert "geparkt" in args
    assert "--source" in args and "forge" in args
    assert "--score" in args and "0.0" in args
    assert spuren.letzter_record(9) == neue_id


def test_backfill_journal_wird_nach_erfolg_nicht_doppelt_gesendet(monkeypatch):
    calls = _cli(monkeypatch)
    journal = [{
        "id": 18,
        "task_id": 4,
        "ts": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "kind": "stage_done",
        "message": "Plan fertig",
        "tokens_in": 12,
        "tokens_out": 5,
        "cache_read": 3,
    }]
    tasks = [{"id": 4, "title": "Aufgabe", "state": "parked"}]
    abfragen = []

    def query(sql, params=None):
        abfragen.append(sql)
        return tasks if "forge_tasks" in sql else journal

    monkeypatch.setattr(spuren.db, "query", query)

    assert spuren.backfill() == 1
    assert spuren.backfill() == 0
    assert len(calls) == 1
    assert any("forge_journal" in sql for sql in abfragen)
    assert any("forge_tasks" in sql for sql in abfragen)
    payload = json.loads(calls[0][1]["input"])
    assert payload["input"]["original_ts"] == "2026-09-01T00:00:00+00:00"
    assert payload["input"]["original_kind"] == "stage_done"
    assert payload["cost"]["tokens_in"] == 12


def test_backfill_dry_run_liest_db_aber_schreibt_keine_records(monkeypatch):
    calls = _cli(monkeypatch)
    rows = [{"id": 18, "task_id": 4, "kind": "stage_done"}]
    monkeypatch.setattr(
        spuren.db,
        "query",
        lambda sql, params=None: [{"id": 4}] if "forge_tasks" in sql else rows,
    )

    assert spuren.backfill(dry_run=True) == 1
    assert calls == []


def test_verdict_ohne_vorherigen_record_legt_einen_beurteilbaren_wurzelrecord_an(monkeypatch):
    calls = _cli(monkeypatch)

    result_id = spuren.verdict(88, "verworfen", note="vor dem ersten Lauf verworfen")

    assert result_id == f"{2:032x}"
    root_call, verdict_call = calls
    assert root_call[0][root_call[0].index("--kind") + 1] == "decision"
    assert verdict_call[0][1:4] == ["verdict", f"{1:032x}", "verworfen"]


def test_weicher_stop_haengt_gestoppt_verdict_an_aktiven_task(monkeypatch):
    calls = _cli(monkeypatch)
    monkeypatch.setattr(daemon.queue, "active", lambda: {"id": 89, "state": m.REVIEWING})

    daemon._trace_stop_aktiven_task()

    verdict_call = next(call for call in calls if call[0][1] == "verdict")
    assert verdict_call[0][3] == "gestoppt"


def test_erschoepfte_kette_haengt_kontingent_verdict_an(monkeypatch, tmp_path):
    calls = _cli(monkeypatch)
    monkeypatch.setattr(pipeline.journal, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.ketten, "waehle", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.ketten, "kette_erschoepft", lambda *args, **kwargs: True)

    task = {"id": 90, "title": "Aufgabe", "description": "", "state": m.IMPLEMENTING}
    assert pipeline.eine_stufe(task, tmp_path) == "kontingent"

    verdict_call = next(call for call in calls if call[0][1] == "verdict")
    assert verdict_call[0][3] == "kontingent"


def test_pipeline_retry_haengt_an_park_verdict_und_zaehlt_versuche(monkeypatch, tmp_path):
    calls = _cli(monkeypatch)
    results = iter([
        RunResult(ok=False, error="Agent abgestürzt"),
        RunResult(ok=True, text="Änderung bereit"),
    ])
    monkeypatch.setattr(pipeline.journal, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.queue, "zaehle_fehlschlag", lambda *args, **kwargs: False)
    monkeypatch.setattr(pipeline.queue, "park", lambda *args, **kwargs: True)
    monkeypatch.setattr(pipeline.queue, "set_state", lambda *args, **kwargs: True)
    monkeypatch.setattr(pipeline.queue, "merke_implement_modell", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.queue, "versuche_zuruecksetzen", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.queue, "fixrunden", lambda *args, **kwargs: 0)
    monkeypatch.setattr(pipeline.budget, "buche", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.budget, "markiere_erschoepft", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.ketten, "waehle", lambda name, verboten=frozenset(): ("opencode", "modell"))
    monkeypatch.setattr(pipeline.backends, "hole", lambda name: (
        lambda *args, **kwargs: next(results)
    ))
    monkeypatch.setattr(pipeline, "_committe_stufenarbeit", lambda *args, **kwargs: None)

    task = {"id": 31, "title": "Versuch", "description": "", "state": m.IMPLEMENTING}
    assert pipeline.eine_stufe(task, tmp_path) == "geparkt"
    assert pipeline.eine_stufe(task, tmp_path) == "weiter"

    stage_calls = [call for call in calls if call[0][1] == "record"]
    verdict_calls = [call for call in calls if call[0][1] == "verdict"]
    assert len(stage_calls) == 2
    assert len(verdict_calls) == 1
    assert verdict_calls[0][0][3] == "geparkt"
    assert stage_calls[1][0][stage_calls[1][0].index("--parent") + 1] == f"{2:032x}"
    first_payload = json.loads(stage_calls[0][1]["input"])
    second_payload = json.loads(stage_calls[1][1]["input"])
    assert first_payload["input"]["versuch"] == 1
    assert second_payload["input"]["versuch"] == 2


def test_merge_erzeugt_verdict_am_letzten_record(monkeypatch, tmp_path):
    calls = _cli(monkeypatch)
    spuren.record(55, "gate", result={"status": "ok"})
    baum = tmp_path / "worktree"
    baum.mkdir()
    monkeypatch.setattr(freigabe.queue, "hole", lambda task_id: {"id": task_id, "state": m.AWAITING_APPROVAL})
    monkeypatch.setattr(freigabe.worktree, "path_for", lambda task_id: baum)
    monkeypatch.setattr(freigabe.worktree, "remove", lambda *args, **kwargs: True)
    monkeypatch.setattr(freigabe.queue, "set_state", lambda *args, **kwargs: True)
    monkeypatch.setattr(freigabe.journal, "log", lambda *args, **kwargs: None)

    def git(*args, cwd=None, timeout=300):
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            stdout = "main"
        elif args[:1] == ("merge-base",):
            stdout = "abc123"
        elif args[:2] == ("rev-parse", "main"):
            stdout = "abc123"
        else:
            stdout = ""
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr(freigabe.gitctl, "run", git)

    assert freigabe.freigeben(55, repo=tmp_path) == "gemerged"
    verdict_call = next(call for call in calls if call[0][1] == "verdict")
    assert verdict_call[0][2:4] == [f"{1:032x}", "gemerged"]


def test_review_speichert_urteil_und_diff_als_gate_trace(monkeypatch, tmp_path):
    calls = _cli(monkeypatch)
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "review.json").write_text('{"verdict":"pass","findings":[]}', encoding="utf-8")
    (forge_dir / "diff.patch").write_text("diff --git a/tests/a.py b/tests/a.py\n", encoding="utf-8")
    review_stage = next(stage for stage in stages.STAGES if stage.name == "review")

    pipeline._trace_review(11, review_stage, "agy", "claude-opus", tmp_path)

    args, kwargs = calls[0]
    assert args[args.index("--kind") + 1] == "gate"
    assert {value.split("=", 1)[0] for key, value in zip(args, args[1:]) if key == "--blob"} == {
        "review", "diff",
    }
    payload = json.loads(kwargs["input"])
    assert payload["input"]["phase"] == "review"
    assert payload["result"]["status"] == "ok"
    assert payload["result"]["score"] == 1.0


def test_deterministisches_gate_haengt_diff_tests_und_park_verdict_an(monkeypatch, tmp_path):
    calls = _cli(monkeypatch)
    monkeypatch.setattr(daemon.gate, "pruefe", lambda baum: daemon.gate.GateErgebnis(
        ok=False,
        gruende=["Tests rot"],
        diff="diff patch",
        tests_output="Test failed",
        lint_output="Lint issue",
    ))
    monkeypatch.setattr(daemon.journal, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon.queue, "zaehle_fehlschlag", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon.queue, "park", lambda *args, **kwargs: True)

    assert daemon._gate_und_abschliessen(23, tmp_path) == "geparkt"

    record_call = next(call for call in calls if call[0][1] == "record")
    blob_names = [record_call[0][index + 1].split("=", 1)[0]
                  for index, value in enumerate(record_call[0][:-1]) if value == "--blob"]
    assert blob_names == ["diff", "tests", "lint"]
    verdict_call = next(call for call in calls if call[0][1] == "verdict")
    assert verdict_call[0][3] == "geparkt"


def test_jev_entscheidung_wird_als_gate_record_gespeichert(monkeypatch):
    calls = _cli(monkeypatch)

    async def decision(task):
        return jev_gate.PreflightResult(
            status="ready", mode="tdd", risk="sensitive", external=False,
        )

    monkeypatch.setattr(jev_gate, "_entscheide", decision)

    result = asyncio.run(jev_gate.entscheide({"id": 77, "title": "Aufgabe", "description": ""}))

    assert result.status == "ready"
    args, kwargs = calls[0]
    assert args[args.index("--kind") + 1] == "gate"
    payload = json.loads(kwargs["input"])
    assert payload["input"]["phase"] == "jev"
    assert payload["output"]["mode"] == "tdd"
    assert payload["result"]["score"] == 1.0
