import pytest
from app.services.evidence_backed_writing_card_service import (
    EvidenceBackedWritingCardService,
    EvidenceBackedWritingCardRequest,
    EvidenceItem
)

def test_empty_input_returns_safe_empty_list():
    service = EvidenceBackedWritingCardService()
    request = EvidenceBackedWritingCardRequest(candidates=[])
    result = service.generate_cards(request)
    
    assert result["writing_cards"] == []
    guardrails = result["safety_guardrails"]
    assert guardrails["writes_db"] is False
    assert guardrails["auto_insert"] is False

def test_safe_verified_produces_confirmed_card():
    service = EvidenceBackedWritingCardService()
    request = EvidenceBackedWritingCardRequest(candidates=[
        EvidenceItem(
            title="Safe Paper",
            evidence_status="safe_verified",
            draft_text="Safe claim.",
            warnings=["previous_warning"]
        )
    ])
    result = service.generate_cards(request)
    
    assert len(result["writing_cards"]) == 1
    card = result["writing_cards"][0]
    
    assert card["card_type"] == "confirmed_writing_card"
    assert card["status"] == "confirmed_writing_card"
    assert card["can_be_used_as_confirmed_fact"] is True
    assert card["evidence_status"] == "safe_verified"
    assert "previous_warning" in card["warnings"]
    assert "suggestion_only_needs_human_verification" not in card["warnings"]
    
    assert "safety_guardrails" in card
    guardrails = card["safety_guardrails"]
    assert guardrails["writes_db"] is False
    assert guardrails["auto_insert"] is False
    assert guardrails["generates_bibliography"] is False
    assert guardrails["export_unlocked"] is False
    assert guardrails["verified_status_changed"] is False

@pytest.mark.parametrize("status", [
    "verified", "metadata_only", "pending", "unverified", "unknown"
])
def test_non_safe_verified_produces_suggestion_only_card(status):
    service = EvidenceBackedWritingCardService()
    request = EvidenceBackedWritingCardRequest(candidates=[
        EvidenceItem(
            title="Unsafe Paper",
            evidence_status=status,
            draft_text="Unsafe claim.",
            warnings=["existing_warning"]
        )
    ])
    result = service.generate_cards(request)
    
    assert len(result["writing_cards"]) == 1
    card = result["writing_cards"][0]
    
    assert card["card_type"] == "suggestion_only"
    assert card["status"] == "suggestion_only"
    assert card["can_be_used_as_confirmed_fact"] is False
    assert card["evidence_status"] == status
    assert "existing_warning" in card["warnings"]
    assert "suggestion_only_needs_human_verification" in card["warnings"]
    
    assert "safety_guardrails" in card
    guardrails = card["safety_guardrails"]
    assert guardrails["writes_db"] is False
    assert guardrails["auto_insert"] is False
    assert guardrails["generates_bibliography"] is False
    assert guardrails["export_unlocked"] is False
    assert guardrails["verified_status_changed"] is False

def test_guardrails_are_strict():
    service = EvidenceBackedWritingCardService()
    request = EvidenceBackedWritingCardRequest(candidates=[])
    result = service.generate_cards(request)
    guardrails = result["safety_guardrails"]
    
    assert guardrails["read_only"] is True
    assert guardrails["writes_db"] is False
    assert guardrails["auto_insert"] is False
    assert guardrails["generates_bibliography"] is False
    assert guardrails["export_unlocked"] is False
    assert guardrails["verified_status_changed"] is False
