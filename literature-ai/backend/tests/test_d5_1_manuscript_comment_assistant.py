import pytest
from unittest.mock import Mock
from app.services.manuscript_comment_assistant_service import (
    ManuscriptCommentAssistantService,
    CommentSuggestionRequest
)
from app.services.writing_citation_candidate_service import WritingCitationCandidateService

def test_manuscript_comment_assistant_guardrails():
    # Setup mock CitationService
    mock_citation_service = Mock(spec=WritingCitationCandidateService)
    mock_citation_service.recommend.return_value = {
        "candidates": [
            {
                "paper_id": "123",
                "title": "Mock Paper",
                "requires_human_verification": True,
                "evidence_status": "metadata_only"
            }
        ]
    }
    
    service = ManuscriptCommentAssistantService(mock_citation_service)
    request = CommentSuggestionRequest(paragraph_text="The catalyst shows high activity.")
    
    response = service.suggest_comments(request)
    
    # Assert Guardrails
    guardrails = response["safety_guardrails"]
    assert guardrails["is_suggestion_only"] is True, "Must be flagged as suggestion only"
    assert guardrails["writes_db"] is False, "Must not write to DB"
    assert guardrails["auto_insert"] is False, "Must not auto insert"
    assert guardrails["generates_bibliography"] is False, "Must not generate bibliography"
    
    # Assert Warnings
    assert len(response["suggestions"]) == 1
    warnings = response["suggestions"][0]["warnings"]
    assert "draft_do_not_use_as_final_fact" in warnings, "Must warn about draft status"
    assert "suggestion_only_needs_human_verification" in warnings, "Must warn if verification needed"

def test_manuscript_comment_assistant_no_candidates():
    mock_citation_service = Mock(spec=WritingCitationCandidateService)
    mock_citation_service.recommend.return_value = {"candidates": []}
    
    service = ManuscriptCommentAssistantService(mock_citation_service)
    request = CommentSuggestionRequest(paragraph_text="The catalyst shows high activity.")
    
    response = service.suggest_comments(request)
    assert len(response["suggestions"]) == 0
    assert response["safety_guardrails"]["writes_db"] is False
