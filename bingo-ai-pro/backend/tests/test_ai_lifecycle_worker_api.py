from __future__ import annotations

from fastapi.testclient import TestClient

from app import app


def test_ai_worker_trigger_disabled_without_token(monkeypatch):
    monkeypatch.delenv("AI_LIFECYCLE_TRIGGER_TOKEN", raising=False)
    client = TestClient(app)
    response = client.post("/api/ai-lifecycle-worker/run", params={"issue": "115056270"})
    assert response.status_code == 503


def test_ai_worker_trigger_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("AI_LIFECYCLE_TRIGGER_TOKEN", "expected-token")
    client = TestClient(app)
    response = client.post(
        "/api/ai-lifecycle-worker/run",
        params={"issue": "115056270"},
        headers={"X-AI-Lifecycle-Token": "wrong-token"},
    )
    assert response.status_code == 401
