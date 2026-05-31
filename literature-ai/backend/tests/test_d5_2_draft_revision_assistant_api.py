import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from app.main import app
from app.db.session import get_db_session

client = TestClient(app)

@pytest.fixture(autouse=True)
def override_db_session():
    app.dependency_overrides[get_db_session] = lambda: None
    yield
    app.dependency_overrides.clear()

@patch("app.api.writing.DraftRevisionAssistantService")
def test_draft_revisions_endpoint(mock_service_class):
    mock_instance = MagicMock()
    mock_service_class.return_value = mock_instance
    
    mock_instance.revise_draft.return_value = {
        "draft_text": "Test draft",
        "revision_suggestions": [
            {
                "suggestion_type": "clarity_improvement",
                "original_excerpt": "Test draft",
                "suggested_revision": "Test draft (revised)",
                "warnings": ["suggestion_only_needs_human_verification"],
                "candidate_papers": [
                    {
                        "title": "Mock Paper 1",
                        "evidence_status": "metadata_only",
                        "warnings": ["source_metadata_only", "suggestion_only_needs_human_verification"]
                    },
                    {
                        "title": "Mock Paper 2",
                        "evidence_status": "pending",
                        "warnings": ["suggestion_only_needs_human_verification"]
                    },
                    {
                        "title": "Mock Paper 3",
                        "evidence_status": "unverified",
                        "warnings": ["suggestion_only_needs_human_verification"]
                    }
                ]
            }
        ],
        "safety_guardrails": {}
    }

    payload = {
        "draft_text": "Test draft",
        "candidate_papers": [
            {
                "title": "Mock Paper 1",
                "evidence_status": "metadata_only",
                "warnings": ["source_metadata_only"]
            },
            {
                "title": "Mock Paper 2",
                "evidence_status": "pending",
                "warnings": []
            },
            {
                "title": "Mock Paper 3",
                "evidence_status": "unverified",
                "warnings": []
            }
        ]
    }
    
    response = client.post("/api/writing/draft-revisions", json=payload)
    assert response.status_code == 200
    
    # Assert that the service was called with the correct request object
    mock_instance.revise_draft.assert_called_once()
    called_request = mock_instance.revise_draft.call_args[0][0]
    assert called_request.draft_text == "Test draft"
    assert len(called_request.candidate_papers) == 3
    assert called_request.candidate_papers[0]["evidence_status"] == "metadata_only"
    assert "source_metadata_only" in called_request.candidate_papers[0]["warnings"]
    assert called_request.candidate_papers[1]["evidence_status"] == "pending"
    assert called_request.candidate_papers[2]["evidence_status"] == "unverified"
    
    data = response.json()
    
    assert data["draft_text"] == "Test draft"
    assert len(data["revision_suggestions"]) == 1
    
    suggestion = data["revision_suggestions"][0]
    assert len(suggestion["candidate_papers"]) == 3
    
    cand_1 = suggestion["candidate_papers"][0]
    assert cand_1["evidence_status"] == "metadata_only"
    assert "source_metadata_only" in cand_1["warnings"]
    assert "suggestion_only_needs_human_verification" in cand_1["warnings"]
    
    cand_2 = suggestion["candidate_papers"][1]
    assert cand_2["evidence_status"] == "pending"
    assert "suggestion_only_needs_human_verification" in cand_2["warnings"]
    
    cand_3 = suggestion["candidate_papers"][2]
    assert cand_3["evidence_status"] == "unverified"
    assert "suggestion_only_needs_human_verification" in cand_3["warnings"]
    
    assert "suggestion_only_needs_human_verification" in suggestion["warnings"]
    
    guardrails = data["safety_guardrails"]
    assert guardrails["is_suggestion_only"] is True
    assert guardrails["writes_db"] is False
    assert guardrails["auto_apply"] is False
    assert guardrails["generates_bibliography"] is False
    assert guardrails["export_unlocked"] is False
    assert guardrails["verified_status_changed"] is False

def test_draft_revisions_blank_text():
    response = client.post("/api/writing/draft-revisions", json={
        "draft_text": "   "
    })
    assert response.status_code == 422
    assert "blank" in response.json()["detail"].lower()
