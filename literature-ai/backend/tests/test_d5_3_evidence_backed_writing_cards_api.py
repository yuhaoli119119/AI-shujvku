import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_evidence_backed_cards_api_safe_verified():
    response = client.post(
        "/api/writing/evidence-backed-cards",
        json={
            "candidates": [
                {
                    "title": "Safe Title",
                    "evidence_status": "safe_verified",
                    "draft_text": "Safe text.",
                    "warnings": []
                }
            ]
        }
    )
    assert response.status_code == 200
    data = response.json()
    
    assert "writing_cards" in data
    assert len(data["writing_cards"]) == 1
    
    card = data["writing_cards"][0]
    assert card["card_type"] == "confirmed_writing_card"
    assert card["evidence_status"] == "safe_verified"
    
    guardrails = card["safety_guardrails"]
    assert guardrails["writes_db"] is False
    assert guardrails["auto_insert"] is False

def test_evidence_backed_cards_api_non_safe():
    response = client.post(
        "/api/writing/evidence-backed-cards",
        json={
            "candidates": [
                {
                    "title": "Unverified Title",
                    "evidence_status": "unverified",
                    "draft_text": "Unverified text."
                }
            ]
        }
    )
    assert response.status_code == 200
    data = response.json()
    
    card = data["writing_cards"][0]
    assert card["card_type"] == "suggestion_only"
    assert "suggestion_only_needs_human_verification" in card["warnings"]
    
    guardrails = card["safety_guardrails"]
    assert guardrails["writes_db"] is False
    assert guardrails["auto_insert"] is False
    
    global_guardrails = data["safety_guardrails"]
    assert global_guardrails["export_unlocked"] is False
    
def test_evidence_backed_cards_api_empty():
    response = client.post(
        "/api/writing/evidence-backed-cards",
        json={"candidates": []}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["writing_cards"] == []
