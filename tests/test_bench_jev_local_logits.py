"""Offline-Tests für Benchmark-Ausgaben des lokalen Logit-Backends."""
from __future__ import annotations

import asyncio
import sys

import pytest

import jevkit
from bench.jev import run
from core.decide import Choice, Noul


def test_tagged_outputs_haben_eigene_dateinamen_und_tag_ist_sicher(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RESULTS", tmp_path)

    raw, report = run.result_paths("logits-2026-09-23")

    assert raw == tmp_path / "raw-logits-2026-09-23.jsonl"
    assert report == tmp_path / "report-logits-2026-09-23.md"
    with pytest.raises(ValueError, match="tag"):
        run.result_paths("../overwrite")


def test_tagged_run_verweigert_vorhandene_ergebnisse(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    raw, report = run.result_paths("repeat")
    report.write_text("bestehender Bericht")

    with pytest.raises(FileExistsError, match="report-repeat.md"):
        run.ensure_output_paths_are_new(raw, report)

    assert report.read_text() == "bestehender Bericht"


def test_local_logits_cli_verlangt_tag_vor_ergebnisdateien(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bench.jev.run", "--no-jev", "--no-local",
                                      "--local-logits", "qwen3.5:9b"])

    with pytest.raises(SystemExit) as error:
        asyncio.run(run.main())

    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_tagged_benchmark_main_schreibt_beide_neuen_dateien(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bench.jev.run", "--no-jev", "--no-local", "--tag", "cli-check"])

    asyncio.run(run.main())

    raw, report = run.result_paths("cli-check")
    assert raw.exists()
    assert report.exists()
    assert not (tmp_path / "raw.jsonl").exists()


def test_benchmark_fragen_werden_in_jevkit_typen_ueberfuehrt():
    converted = run.questions_for_local({
        "q": {"type": "noul", "instructions": "Ist es wahr?", "criteria": {"true": "Ja"}},
        "c": {"type": "choice", "instructions": "Was?", "criteria": {"a": "Erstes", "b": "Zweites"}},
    })

    assert isinstance(converted["q"], Noul)
    assert isinstance(converted["c"], Choice)
    assert converted["q"].criteria == {"true": "Ja"}
    assert converted["c"].criteria == {"a": "Erstes", "b": "Zweites"}


def test_benchmark_ollama_client_nutzt_konfigurierte_base_url(monkeypatch):
    monkeypatch.setattr(run.config, "OLLAMA_BASE_URL", "http://ollama.example:1234")

    class FakeClient:
        def __init__(self, *, host):
            self.host = host

    monkeypatch.setattr(run.ollama, "AsyncClient", FakeClient)

    assert run.ollama_client().host == "http://ollama.example:1234"


def test_logits_report_enthaelt_brier_latenz_accuracy_und_abdeckung(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    rows = [
        {"id": "a", "kind": "address", "state": "x", "expected": True,
         "backend": "local-logits:qwen3.5:9b", "system": "local-logits", "label": True,
         "correct": True, "confidence": 0.8, "raw": {"noul": 0.9}, "latency": 0.5},
        {"id": "b", "kind": "address", "state": "y", "expected": False,
         "backend": "local-logits:qwen3.5:9b", "system": "local-logits", "label": False,
         "correct": True, "confidence": 0.2, "raw": {"noul": 0.4}, "latency": 0.7},
    ]

    run.write_report(rows, tag="metric-test")
    report = (tmp_path / "report-metric-test.md").read_text()

    assert "## Lokales Logit-Scoring" in report
    assert "0.085" in report
    assert "0.60" in report  # Median-Latenz
    assert "1/1 (100%)" in report  # selektive Accuracy
    assert "1/2 (50%)" in report  # Coverage
    assert "local-logits:qwen3.5:9b" in report


def test_rohzeile_fuehrt_system_und_fehlende_labels():
    answer = jevkit.NoulAnswer(0.9)
    decision = run.local_logits_row(
        {"id": "x", "kind": "address", "expected": True, "state": "x", "note": ""},
        answer, model="qwen3.5:9b", latency=0.25, missing_labels=["B"],
    )

    assert decision["system"] == "local-logits"
    assert decision["backend"] == "local-logits:qwen3.5:9b"
    assert decision["raw"] == {"noul": 0.9}
    assert decision["missing_labels"] == ["B"]
