from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.content_knowledge_service import ContentKnowledgeService


class ContentWritingPlanService:
    """Produces an exportable RAG evidence/writing/citation plan, never a hidden web draft."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def build(self, *, query: str, paper_ids: list[UUID] | None = None) -> dict:
        knowledge = ContentKnowledgeService(self.session)
        rows = knowledge.search_for_rag(
            query=query, paper_ids=paper_ids, include_review_assist=False, limit=20
        )
        evidence = [item for item, _ in rows if item.can_use_for_citation]
        excluded_unreviewed = knowledge.count_unreviewed_matching(query=query, paper_ids=paper_ids)
        claims: list[dict[str, Any]] = []
        for item in evidence:
            claim_text = item.content.strip()
            claim_id = "plan:" + hashlib.sha256(
                f"{query}\0{item.item_id}".encode("utf-8")
            ).hexdigest()[:24]
            claims.append({
                "claim_id": claim_id, "claim_text": claim_text,
                "evidence_item_id": item.item_id, "paper_code": item.paper_code,
                "source_fragment": item.evidence_text, "locator": item.evidence_locator,
                "page_start": item.page_start, "page_end": item.page_end,
                "citation_status": "citable",
            })
        return {
            "query": query, "mode": "evidence_pack_only", "web_model_disabled": True,
            "evidence_pack": claims,
            "writing_plan": [
                {"claim_id": item["claim_id"], "instruction": "Use only the cited evidence fragment; preserve uncertainty."}
                for item in claims
            ],
            "citation_plan": [
                {key: item[key] for key in ("claim_id", "evidence_item_id", "paper_code", "source_fragment", "locator", "page_start", "page_end", "citation_status")}
                for item in claims
            ],
            "matched_eligible": len(rows),
            "citation_eligible": len(evidence),
            "excluded_unreviewed": excluded_unreviewed,
            "no_citable_match": not evidence,
            "persistence": {"writes_db": False, "saved_plan": False},
        }
