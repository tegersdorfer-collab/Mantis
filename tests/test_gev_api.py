"""Authentifizierte API für die GEV-Steuerung."""

from fastapi.testclient import TestClient

import config
from web.api import create_app


def test_gev_ack_ist_geschuetzt_und_lehnt_unbekannte_id_ab(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "test-token")
    client = TestClient(create_app())
    payload = {"id": "0" * 32, "result": {"ok": True}}
    assert client.post("/api/gev/ack", json=payload).status_code == 401
    response = client.post(
        "/api/gev/ack", json=payload,
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 404


def test_gev_ack_lehnt_ungueltiges_resultat_ab(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_TOKEN", "test-token")
    client = TestClient(create_app())
    response = client.post(
        "/api/gev/ack", json={"id": "x", "result": {"ok": "yes"}},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 400
