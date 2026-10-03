"""Offline-Tests für das Cloudflare-Backend des Jev-Benchmarks."""
from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from bench.jev import run


@pytest.mark.parametrize("model", ("clef", "clef-flash"))
def test_cloudflare_call_baut_request_und_entpackt_antwort(monkeypatch, model):
    jev_response = {
        "model": model,
        "answers": {"q": {"type": "noul", "noul": 0.9}},
        "usage": {"input_tokens": 12},
    }
    envelope = {"result": jev_response, "success": True, "errors": []}
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(json.dumps(envelope).encode())

    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)

    response, _ = run.cloudflare_call(
        account_id="account-123",
        token="test-token",
        model=model,
        state={"text": "hallo"},
        questions={"q": {"type": "noul"}},
    )

    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == (
        "https://api.cloudflare.com/client/v4/accounts/account-123/ai/run/"
        f"@cf/cloudflare/{model}"
    )
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {
        "model": model,
        "state": {"text": "hallo"},
        "questions": {"q": {"type": "noul"}},
    }
    assert timeout == 60
    assert response == jev_response


def test_cloudflare_call_meldet_cloudflare_fehler(monkeypatch):
    envelope = {"result": None, "success": False, "errors": [{"code": 1000, "message": "invalid model"}]}
    monkeypatch.setattr(
        run.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(envelope).encode()),
    )

    with pytest.raises(RuntimeError, match="invalid model"):
        run.cloudflare_call(
            account_id="account-123",
            token="test-token",
            model="clef",
            state={},
            questions={},
        )


def test_cloudflare_credentials_laden_aus_mantis_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "mantis.env"
    env_file.write_text('CLOUDFLARE_ACCOUNT_ID="account-from-file"\nCLOUDFLARE_AUTH_TOKEN=token-from-file\n')
    monkeypatch.setenv("MANTIS_ENV_FILE", str(env_file))
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_AUTH_TOKEN", raising=False)

    assert run._load_cloudflare_credentials() == ("account-from-file", "token-from-file")


def test_cloudflare_credentials_bevorzugen_umgebungsvariablen(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_ENV_FILE", str(tmp_path / "not-read.env"))
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account-from-env")
    monkeypatch.setenv("CLOUDFLARE_AUTH_TOKEN", "token-from-env")

    assert run._load_cloudflare_credentials() == ("account-from-env", "token-from-env")


def test_cloudflare_backend_erfordert_tag_vor_ergebnisdateien(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "bench.jev.run", "--jev-backend", "clef", "--no-jev", "--no-local",
    ])

    with pytest.raises(SystemExit) as error:
        asyncio.run(run.main())

    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_cloudflare_backendname_erscheint_im_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    backend = "clef-flash (Cloudflare)"
    rows = [{
        "id": "a", "kind": "address", "state": "x", "expected": True,
        "backend": backend, "label": True, "correct": True,
        "confidence": 0.8, "raw": {"noul": 0.9}, "latency": 0.5,
    }]

    run.write_report(rows, tag="cloudflare")

    assert backend in (tmp_path / "report-cloudflare.md").read_text()
