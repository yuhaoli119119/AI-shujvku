from fastapi.testclient import TestClient

from app.main import app


def test_writer_settings_endpoint_is_disabled(setup_test_db):
    client = TestClient(app)

    response = client.get("/api/writer/settings")
    assert response.status_code == 200
    data = response.json()
    assert data["writer_backend"] == "disabled"
    assert data["writer_model"] == "IDE/MCP AI"
    assert data["writer_api_base"] is None
    assert data["writer_api_key"] is None
    assert data["writer_fallback_backend"] == "disabled"

    response = client.post(
        "/api/writer/settings",
        json={
            "writer_backend": "openai_compatible",
            "writer_model": "test-model-abc",
            "writer_api_base": "https://test-api.example.com",
            "writer_api_key": "sk-test-key-123456",
            "writer_fallback_backend": "rule",
        },
    )
    assert response.status_code == 200
    assert response.json() == data


def test_writer_draft_endpoint_is_gone(setup_test_db):
    client = TestClient(app)

    response = client.post(
        "/api/writer/draft",
        json={
            "topic": "test",
            "paper_ids": ["00000000-0000-0000-0000-000000000000"],
            "sections": ["abstract"],
        },
    )

    assert response.status_code == 410
    assert "IDE" in response.json()["detail"]
