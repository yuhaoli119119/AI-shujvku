from dataclasses import dataclass, field
from typing import Any
from app.services.writing_citation_candidate_service import (
    WritingCitationCandidateService,
    CitationCandidateRequest,
    CitationCandidateFilters,
)

@dataclass
class CommentSuggestionRequest:
    paragraph_text: str
    max_candidates_per_suggestion: int = 3

class ManuscriptCommentAssistantService:
    """Read-only service that analyzes paragraphs and suggests comments/citations."""

    def __init__(self, citation_service: WritingCitationCandidateService) -> None:
        self.citation_service = citation_service

    def suggest_comments(self, request: CommentSuggestionRequest) -> dict[str, Any]:
        """
        Analyze a paragraph and provide suggestion-only comments.
        Strictly applies safety guardrails for D5-1 Writing Workflow Preflight.
        """
        # Minimal skeleton: use the whole paragraph text to query for citation candidates
        citation_request = CitationCandidateRequest(
            text=request.paragraph_text,
            max_candidates=request.max_candidates_per_suggestion,
        )
        
        try:
            candidates_response = self.citation_service.recommend(citation_request)
            candidates = candidates_response.get("candidates", [])
        except ValueError:
            # e.g., text must contain at least two searchable terms
            candidates = []

        suggestions = []
        if candidates:
            # If any candidate requires human verification, bubble up the warning
            has_unverified = any(
                cand.get("requires_human_verification", True) for cand in candidates
            )
            
            warnings = ["draft_do_not_use_as_final_fact", "suggestion_only"]
            if has_unverified:
                warnings.append("suggestion_only_needs_human_verification")

            suggestions.append({
                "type": "draft_comment_suggestion",
                "text": "Consider citing evidence for the claims in this paragraph. Please verify all suggestions.",
                "candidate_papers": candidates,
                "warnings": warnings,
            })

        return {
            "paragraph_text": request.paragraph_text,
            "suggestions": suggestions,
            "safety_guardrails": {
                "is_suggestion_only": True,
                "writes_db": False,
                "auto_insert": False,
                "generates_bibliography": False
            }
        }
