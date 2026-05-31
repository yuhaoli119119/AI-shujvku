import pytest
from app.services.draft_revision_assistant_service import (
    DraftRevisionAssistantService,
    DraftRevisionRequest
)

def test_draft_revision_assistant_blank_input():
    service = DraftRevisionAssistantService()
    
    response = service.revise_draft(DraftRevisionRequest(draft_text="   "))
    assert len(response["revision_suggestions"]) == 0
    guardrails = response["safety_guardrails"]
    assert guardrails["is_suggestion_only"] is True
    assert guardrails["writes_db"] is False
    assert guardrails["auto_apply"] is False

def test_draft_revision_assistant_unsupported_claim():
    service = DraftRevisionAssistantService()
    
    response = service.revise_draft(DraftRevisionRequest(draft_text="This is the best catalyst."))
    assert len(response["revision_suggestions"]) > 0
    suggestion = response["revision_suggestions"][0]
    
    assert suggestion["suggestion_type"] == "unsupported_claim"
    assert "unsupported_claim_needs_evidence" in suggestion["warnings"]
    assert "draft_do_not_use_as_final_fact" in suggestion["warnings"]
    
    guardrails = response["safety_guardrails"]
    assert guardrails["is_suggestion_only"] is True
    assert guardrails["writes_db"] is False
    assert guardrails["auto_apply"] is False
    assert guardrails["generates_bibliography"] is False
    assert guardrails["export_unlocked"] is False
    assert guardrails["verified_status_changed"] is False
    
def test_draft_revision_assistant_normal_claim():
    service = DraftRevisionAssistantService()
    
    response = service.revise_draft(DraftRevisionRequest(draft_text="It is a catalyst."))
    assert len(response["revision_suggestions"]) > 0
    suggestion = response["revision_suggestions"][0]
    
    assert suggestion["suggestion_type"] == "clarity_improvement"
    assert "draft_do_not_use_as_final_fact" in suggestion["warnings"]
    assert "unsupported_claim_needs_evidence" not in suggestion["warnings"]
    
    guardrails = response["safety_guardrails"]
    assert guardrails["is_suggestion_only"] is True

def test_draft_revision_assistant_candidate_passthrough():
    service = DraftRevisionAssistantService()
    
    candidates = [
        {"title": "Paper 1", "evidence_status": "metadata_only", "warnings": ["source_metadata_only"]},
        {"title": "Paper 2", "evidence_status": "pending", "warnings": []},
        {"title": "Paper 3", "evidence_status": "unverified", "warnings": ["some_other_warning"]}
    ]
    
    response = service.revise_draft(DraftRevisionRequest(
        draft_text="It is a catalyst.",
        candidate_papers=candidates
    ))
    
    suggestion = response["revision_suggestions"][0]
    
    # Assert candidates passed through without escalating
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
    assert "some_other_warning" in cand_3["warnings"]
    assert "suggestion_only_needs_human_verification" in cand_3["warnings"]
    
    # Assert proper warnings appended
    assert "suggestion_only_needs_human_verification" in suggestion["warnings"]
    assert "draft_do_not_use_as_final_fact" in suggestion["warnings"]
    
    guardrails = response["safety_guardrails"]
    assert guardrails["is_suggestion_only"] is True
    assert guardrails["verified_status_changed"] is False
