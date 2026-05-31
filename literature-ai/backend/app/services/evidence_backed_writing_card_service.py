from dataclasses import dataclass, field
from typing import Any

@dataclass
class EvidenceItem:
    title: str
    evidence_status: str
    draft_text: str
    warnings: list[str] = field(default_factory=list)
    source_locator: str | None = None

@dataclass
class EvidenceBackedWritingCardRequest:
    candidates: list[EvidenceItem] = field(default_factory=list)

class EvidenceBackedWritingCardService:
    """Read-only service that evaluates citation candidates and generates writing cards."""
    
    def generate_cards(self, request: EvidenceBackedWritingCardRequest) -> dict[str, Any]:
        """
        Generate writing cards based on candidates.
        Strictly applies safety guardrails for D5-3A Evidence-backed Writing Cards Preflight.
        """
        cards = []
        
        for candidate in request.candidates:
            status = candidate.evidence_status
            warnings = list(candidate.warnings)
            
            # Strict check: only safe_verified becomes confirmed
            if status == "safe_verified":
                card_status = "confirmed_writing_card"
                card_type = "confirmed_writing_card"
                can_be_used_as_confirmed_fact = True
            else:
                card_status = "suggestion_only"
                card_type = "suggestion_only"
                can_be_used_as_confirmed_fact = False
                
                # Append human verification warning if not present
                warning_msg = "suggestion_only_needs_human_verification"
                if warning_msg not in warnings:
                    warnings.append(warning_msg)
            
            cards.append({
                "card_type": card_type,
                "status": card_status,
                "can_be_used_as_confirmed_fact": can_be_used_as_confirmed_fact,
                "draft_text": candidate.draft_text,
                "source_title": candidate.title,
                "evidence_status": status,
                "warnings": warnings,
                "safety_guardrails": self._guardrails()
            })
            
        return {
            "writing_cards": cards,
            "safety_guardrails": self._guardrails()
        }

    def _guardrails(self) -> dict[str, bool]:
        return {
            "read_only": True,
            "writes_db": False,
            "auto_insert": False,
            "generates_bibliography": False,
            "export_unlocked": False,
            "verified_status_changed": False
        }
