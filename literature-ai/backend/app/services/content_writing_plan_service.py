from __future__ import annotations

import hashlib
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.rag.multi_paper_evidence_plan import MultiPaperEvidencePlanner
from app.rag.prompt_builder import PaperWriterPromptBuilder
from app.rag.retrieval_intent import route_retrieval_intent
from app.services.content_figure_link_service import ContentFigureLinkService
from app.services.content_knowledge_service import ContentKnowledgeService


class ContentWritingPlanService:
    """Produces an exportable RAG evidence/writing/citation plan, never a hidden web draft."""

    def __init__(
        self,
        session: Session,
        *,
        multi_paper_planner: MultiPaperEvidencePlanner | None = None,
        prompt_builder: PaperWriterPromptBuilder | None = None,
    ) -> None:
        self.session = session
        self.multi_paper_planner = multi_paper_planner
        self.prompt_builder = prompt_builder or PaperWriterPromptBuilder()

    def build(
        self,
        *,
        query: str,
        paper_ids: list[str | UUID] | None = None,
        mode: str | None = "narrative",
        evidence_types: Iterable[str] | None = None,
        requested_sections: Iterable[str] | None = None,
        evidence_budget: int = 24,
        batch_size: int = 10,
        max_evidence_per_paper: int = 3,
        max_sources_per_claim: int = 5,
        candidate_pool_per_type: int = 24,
    ) -> dict:
        if paper_ids:
            return self._build_bounded_multi_paper_plan(
                query=query,
                paper_ids=paper_ids,
                mode=mode,
                evidence_types=evidence_types,
                requested_sections=requested_sections,
                evidence_budget=evidence_budget,
                batch_size=batch_size,
                max_evidence_per_paper=max_evidence_per_paper,
                max_sources_per_claim=max_sources_per_claim,
                candidate_pool_per_type=candidate_pool_per_type,
            )
        return self._build_legacy_unscoped_plan(
            query=query,
            mode=mode,
            evidence_types=evidence_types,
            requested_sections=requested_sections,
        )

    def _build_bounded_multi_paper_plan(
        self,
        *,
        query: str,
        paper_ids: list[str | UUID],
        mode: str | None,
        evidence_types: Iterable[str] | None,
        requested_sections: Iterable[str] | None,
        evidence_budget: int,
        batch_size: int,
        max_evidence_per_paper: int,
        max_sources_per_claim: int,
        candidate_pool_per_type: int,
    ) -> dict[str, Any]:
        planner = self.multi_paper_planner or MultiPaperEvidencePlanner(self.session)
        plan = planner.plan(
            query=query,
            paper_ids=paper_ids,
            mode=mode,
            evidence_types=evidence_types,
            requested_sections=requested_sections,
            evidence_budget=evidence_budget,
            batch_size=batch_size,
            max_evidence_per_paper=max_evidence_per_paper,
            max_sources_per_claim=max_sources_per_claim,
            candidate_pool_per_type=candidate_pool_per_type,
        )
        batch_contexts = [
            self.prompt_builder.build_batch_prompt_context(plan, str(batch["batch_id"]))
            for batch in plan.get("batches") or []
        ]
        selected = [
            item for item in (plan.get("selected_evidence") or [])
            if isinstance(item, dict)
        ]
        citable = [item for item in selected if item.get("can_use_for_citation") is True]
        writing_only = [
            item for item in selected
            if item.get("can_use_for_writing") is True
            and item.get("can_use_for_citation") is not True
        ]
        claims = [self._planned_evidence_claim(query=query, item=item) for item in citable]
        valid_papers = (plan.get("paper_scope") or {}).get("valid_papers") or []
        valid_paper_ids = [
                UUID(str(paper["paper_id"]))
                for paper in valid_papers
                if isinstance(paper, dict) and paper.get("paper_id")
        ]
        excluded_unreviewed = (
            ContentKnowledgeService(self.session).count_unreviewed_matching(
                query=query,
                paper_ids=valid_paper_ids,
            )
            if valid_paper_ids
            else 0
        )
        plan.update(
            {
                "mode": "evidence_pack_only",
                "web_model_disabled": True,
                "bounded_multi_paper_plan_used": True,
                "batch_prompt_contexts": batch_contexts,
                "evidence_pack": claims,
                "writing_context": [
                    self._planned_writing_context(item) for item in writing_only
                ],
                "writing_plan": [
                    {
                        "claim_id": item["claim_id"],
                        "instruction": (
                            "Use only the bound evidence fragment; preserve uncertainty "
                            "and the source marker."
                        ),
                    }
                    for item in claims
                ],
                "citation_plan": [
                    {
                        key: item[key]
                        for key in (
                            "claim_id",
                            "evidence_item_id",
                            "paper_code",
                            "source_fragment",
                            "locator",
                            "page_start",
                            "page_end",
                            "citation_status",
                        )
                    }
                    for item in claims
                ],
                "matched_eligible": len(selected),
                "citation_eligible": len(citable),
                "writing_only_eligible": len(writing_only),
                "excluded_unreviewed": excluded_unreviewed,
                "no_citable_match": not citable,
                "persistence": {"writes_db": False, "saved_plan": False},
            }
        )
        return plan

    def _build_legacy_unscoped_plan(
        self,
        *,
        query: str,
        mode: str | None,
        evidence_types: Iterable[str] | None,
        requested_sections: Iterable[str] | None,
    ) -> dict[str, Any]:
        intent = route_retrieval_intent(
            query,
            mode=mode,
            evidence_types=evidence_types,
            requested_sections=requested_sections,
        )
        knowledge = ContentKnowledgeService(self.session)
        rows = knowledge.search_for_rag(
            query=query, paper_ids=None, include_review_assist=False, limit=20
        )
        evidence = [item for item, _ in rows if item.can_use_for_citation]
        writing_only = [
            item for item, _ in rows
            if item.can_use_for_writing and not item.can_use_for_citation
        ]
        figure_links = ContentFigureLinkService(self.session)
        excluded_unreviewed = knowledge.count_unreviewed_matching(query=query, paper_ids=None)
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
            **intent.as_dict(),
            "bounded_multi_paper_plan_used": False,
            "batch_prompt_contexts": [],
            "evidence_pack": claims,
            "writing_context": [
                {
                    "content_item_id": item.item_id,
                    "paper_code": item.paper_code,
                    "content": item.content,
                    "source_fragment": item.evidence_text,
                    "locator": item.evidence_locator,
                    "page_start": item.page_start,
                    "page_end": item.page_end,
                    "citation_status": "writing_only",
                    "evidence_chain": (item.metadata or {}).get("evidence_chain") or [],
                    "linked_figures": figure_links.links_for_content_source(
                        paper_id=item.paper_id,
                        source_type=item.source_type,
                        source_id=item.source_id,
                    ),
                }
                for item in writing_only
            ],
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
            "writing_only_eligible": len(writing_only),
            "excluded_unreviewed": excluded_unreviewed,
            "no_citable_match": not evidence,
            "database_writes": False,
            "read_only": True,
            "persistence": {"writes_db": False, "saved_plan": False},
        }

    @staticmethod
    def _planned_evidence_claim(*, query: str, item: dict[str, Any]) -> dict[str, Any]:
        evidence_id = str(item.get("evidence_id") or item.get("object_id") or "")
        claim_id = "plan:" + hashlib.sha256(
            f"{query}\0{evidence_id}".encode("utf-8")
        ).hexdigest()[:24]
        locator = item.get("evidence_locator")
        return {
            "claim_id": claim_id,
            "claim_text": str(item.get("excerpt") or "").strip(),
            "evidence_item_id": evidence_id,
            "paper_code": item.get("paper_code"),
            "source_fragment": str(item.get("excerpt") or ""),
            "locator": locator,
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "citation_status": "citable",
        }

    @staticmethod
    def _planned_writing_context(item: dict[str, Any]) -> dict[str, Any]:
        locator = item.get("evidence_locator")
        return {
            "content_item_id": item.get("evidence_id"),
            "object_id": item.get("object_id"),
            "source_paper_id": item.get("source_paper_id"),
            "paper_code": item.get("paper_code"),
            "evidence_type": item.get("evidence_type"),
            "content": item.get("excerpt"),
            "source_fragment": item.get("excerpt"),
            "locator": locator,
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "citation_status": "writing_only",
            "gate_status": item.get("gate_status"),
            "review_status": item.get("review_status"),
            "doi": item.get("doi"),
            "property": item.get("property"),
            "value": item.get("value"),
            "unit": item.get("unit"),
            "context": item.get("context"),
        }
