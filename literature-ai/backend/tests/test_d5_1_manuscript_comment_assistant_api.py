import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from app.main import app
from app.db.session import get_db_session

client = TestClient(app)

@pytest.fixture(autouse=True)
def override_db_session():
    # Provide a dummy session to avoid hitting real DB for API routing
    app.dependency_overrides[get_db_session] = lambda: None
    yield
    app.dependency_overrides.clear()

@patch("app.api.writing.ManuscriptCommentAssistantService")
def test_manuscript_comment_suggestions_endpoint(mock_service_class):
    mock_instance = MagicMock()
    mock_service_class.return_value = mock_instance
    
    mock_instance.suggest_comments.return_value = {
        "paragraph_text": "The catalyst shows high activity.",
        "suggestions": [
            {
                "type": "draft_comment_suggestion",
                "text": "Consider citing evidence for the claims in this paragraph. Please verify all suggestions.",
                "candidate_papers": [
                    {
                        "title": "Mock Paper 1",
                        "evidence_status": "metadata_only"
                    }
                ],
                "warnings": [
                    "draft_do_not_use_as_final_fact",
                    "suggestion_only",
                    "suggestion_only_needs_human_verification"
                ]
            }
        ],
        "safety_guardrails": {
            "is_suggestion_only": True,
            "writes_db": False,
            "auto_insert": False,
            "generates_bibliography": False
        }
    }

    payload = {
        "paragraph_text": "The catalyst shows high activity.",
        "max_candidates_per_suggestion": 3
    }
    response = client.post("/api/writing/manuscript-comment-suggestions", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    assert data["paragraph_text"] == payload["paragraph_text"]
    assert len(data["suggestions"]) == 1
    
    suggestion = data["suggestions"][0]
    assert "draft_do_not_use_as_final_fact" in suggestion["warnings"]
    assert "suggestion_only_needs_human_verification" in suggestion["warnings"]
    assert len(suggestion["candidate_papers"]) == 1
    assert suggestion["candidate_papers"][0]["title"] == "Mock Paper 1"
    assert suggestion["candidate_papers"][0]["evidence_status"] == "metadata_only"
    
    # Assert API level guardrails override
    guardrails = data["safety_guardrails"]
    assert guardrails["is_suggestion_only"] is True
    assert guardrails["writes_db"] is False
    assert guardrails["auto_insert"] is False
    assert guardrails["generates_bibliography"] is False
    assert guardrails["export_unlocked"] is False
    assert guardrails["verified_status_changed"] is False

def test_manuscript_comment_suggestions_blank_text():
    response = client.post("/api/writing/manuscript-comment-suggestions", json={
        "paragraph_text": "   ",
        "max_candidates_per_suggestion": 3
    })
    assert response.status_code == 422
    assert "blank" in response.json()["detail"].lower() or response.status_code == 422
