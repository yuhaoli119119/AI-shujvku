from dataclasses import dataclass
from typing import Any

@dataclass
class DraftRevisionRequest:
    draft_text: str
    candidate_papers: list[dict[str, Any]] | None = None

class DraftRevisionAssistantService:
    """Read-only service that analyzes drafts and provides revision suggestions."""
    
    def revise_draft(self, request: DraftRevisionRequest) -> dict[str, Any]:
        """
        Analyze a draft and provide suggestion-only revisions.
        Strictly applies safety guardrails for D5-2 Draft Revision Assistant Preflight.
        """
        if not request.draft_text or not request.draft_text.strip():
            # Return safe empty result for blank input
            return {
                "draft_text": request.draft_text,
                "revision_suggestions": [],
                "safety_guardrails": self._guardrails()
            }
            
        suggestions = []
        
        # Skeleton minimal logic: If text contains strong unverified claims, flag it.
        # This is a mock/heuristic for the skeleton.
        def append_if_missing(w_list: list[str], warning: str) -> None:
            if warning not in w_list:
                w_list.append(warning)
                
        suggestion_warnings = ["draft_do_not_use_as_final_fact"]
        processed_candidates = []
        
        if request.candidate_papers:
            for cand in request.candidate_papers:
                status = cand.get("evidence_status", "unverified")
                cand_warnings = list(cand.get("warnings", []))
                
                # Strict check: do not escalate unverified statuses
                if status in ["metadata_only", "pending", "unverified"]:
                    append_if_missing(suggestion_warnings, "suggestion_only_needs_human_verification")
                    append_if_missing(cand_warnings, "suggestion_only_needs_human_verification")
                elif status in ["confirmed", "verified", "safe_verified"]:
                    pass # Status remains
                
                processed_candidates.append({
                    "title": cand.get("title", ""),
                    "evidence_status": status,
                    "warnings": cand_warnings
                })
        
        if "prove" in request.draft_text.lower() or "the best" in request.draft_text.lower():
            append_if_missing(suggestion_warnings, "unsupported_claim_needs_evidence")
                
            suggestions.append({
                "suggestion_type": "unsupported_claim",
                "original_excerpt": request.draft_text,
                "suggested_revision": "has been reported to show promising results",
                "warnings": suggestion_warnings,
                "candidate_papers": processed_candidates
            })
        else:
            append_if_missing(suggestion_warnings, "suggestion_only")
                
            suggestions.append({
                "suggestion_type": "clarity_improvement",
                "original_excerpt": request.draft_text,
                "suggested_revision": request.draft_text.strip() + " (revised for clarity)",
                "warnings": suggestion_warnings,
                "candidate_papers": processed_candidates
            })
            
        return {
            "draft_text": request.draft_text,
            "revision_suggestions": suggestions,
            "safety_guardrails": self._guardrails()
        }

    def _guardrails(self) -> dict[str, bool]:
        return {
            "is_suggestion_only": True,
            "writes_db": False,
            "auto_apply": False,
            "generates_bibliography": False,
            "export_unlocked": False,
            "verified_status_changed": False
        }
