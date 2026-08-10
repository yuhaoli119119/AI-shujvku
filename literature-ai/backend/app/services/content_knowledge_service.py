from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    EvidenceClaim,
    EvidenceLocator,
    ContentEvidenceItem,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    MechanismClaim,
    Paper,
    PaperNote,
    PaperSection,
    WritingCard,
)
from app.services.content_knowledge_search import (
    AUDIT_RESULT_VIEW,
    AUDIT_SOURCE_TYPES,
    CONTENT_RESULT_VIEW,
    RESULT_VIEWS,
    content_item_filters,
    content_search_score,
)
from app.services.content_figure_link_service import ContentFigureLinkService
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import (
    ContentObjectGateResult,
    bulk_export_gate_results,
    content_object_gate,
    get_target_reviews,
    writing_card_content_gate,
    writing_card_gate,
)
from app.services.embedding import get_embedding_service
from app.config import get_settings
from app.utils.writing_card_content import normalized_evidence_chain


CONTENT_KNOWLEDGE_SCHEMA_VERSION = "content_knowledge.v1"

CATEGORY_LABELS: dict[str, str] = {
    "mechanism_evidence": "机理内容",
    "performance_evidence": "性能证据",
    "dft_evidence": "DFT证据",
    "figure_table_evidence": "图表证据",
    "material_evidence": "材料信息",
    "method_evidence": "方法信息",
    "writing_material": "论文重点内容",
    "review_viewpoint": "综述观点",
    "uncertainty_note": "争议/风险",
    "draft_evidence_check": "草稿证据核验",
}

PROBLEM_CANDIDATE_STATUSES = {"requires_resolution", "unmapped", "failed", "skipped"}
BLOCKED_CANDIDATE_STATUSES = {"failed", "skipped"}
CITABLE_EVIDENCE_STATUSES = {"approved", "validated", "safe_verified"}
ACTIVE_AUDIT_STATUSES = {"candidate", "pending", "requires_resolution", "unmapped", "needs_human"}
TERMINAL_AUDIT_STATUSES = {
    "failed",
    "skipped",
    "rejected",
    "ai_rejected",
    "rejected_by_local_ai",
    "ignored",
}
APPLIED_AUDIT_STATUSES = {"materialized", "ai_applied"}


@dataclass(slots=True)
class ContentKnowledgeItem:
    item_id: str
    paper_id: str
    paper_code: str | None
    paper_title: str | None
    paper_doi: str | None
    category: str
    category_label: str
    source_type: str
    source_id: str
    source_table: str
    reviewable: bool
    requires_sync: bool
    content: str
    evidence_text: str | None = None
    evidence_locator: dict[str, Any] | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    review_status: str = "needs_review"
    review_gate_status: str = "needs_review"
    candidate_status: str | None = None
    citation_policy: str = "needs_review"
    can_use_for_writing: bool = False
    can_use_for_citation: bool = False
    risk_flags: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    source_ai: str | None = None
    source_label: str | None = None
    source_identity: str | None = None
    source_identity_verified: bool = False
    reviewer: str | None = None
    reviewed_at: str | None = None
    snapshot_fingerprint: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    item_kind: str = "content"
    audit_state: str | None = None
    audit_state_label: str | None = None
    audit_requires_action: bool = False
    linked_target_type: str | None = None
    linked_target_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class ContentKnowledgeService:
    """Persistent content-evidence projection used by review, RAG and citation plans."""

    def __init__(self, session: Session):
        self.session = session

    def list_items(
        self,
        *,
        paper_id: str | uuid.UUID | None = None,
        run_id: str | uuid.UUID | None = None,
        library_name: str | None = None,
        category: str | None = None,
        query: str | None = None,
        result_view: str = CONTENT_RESULT_VIEW,
        include_candidates: bool = True,
        include_blocked: bool = False,
        review_status: str | None = None,
        citation_status: str | None = None,
        source_trust: str | None = None,
        problem_status: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        normalized_view = _normalize_result_view(result_view)
        normalized_library = normalize_library_name(library_name) if library_name is not None else None
        offset_value = max(0, int(offset or 0))
        limit_value = max(1, min(int(limit or 100), 500))
        papers = self._scoped_papers(paper_id=paper_id, library_name=normalized_library)
        paper_by_id = {paper.id: paper for paper in papers}
        paper_ids = list(paper_by_id)
        run_uuid = _maybe_uuid(run_id) if run_id else None
        if run_id and run_uuid is None:
            paper_ids = []
        # Projection is deliberately explicit: callers that need a durable review
        # package call sync_items() (and commit).  A GET remains read-only and can
        # still render legacy source rows until the first sync is performed.
        items, total, counts, distinct_paper_count = self._persistent_items(
            paper_ids=paper_ids,
            run_id=run_uuid,
            category=category,
            query=query,
            result_view=normalized_view,
            include_candidates=include_candidates,
            include_blocked=include_blocked,
            review_status=review_status,
            citation_status=citation_status,
            source_trust=source_trust,
            problem_status=problem_status,
            offset=offset_value,
            limit=limit_value,
        )
        if total == 0 and paper_ids and not run_id and not self._has_persistent_items(
            paper_ids,
            result_view=normalized_view,
            include_candidates=include_candidates,
        ):
            legacy = self._legacy_items(
                paper_ids,
                paper_by_id,
                include_candidates=include_candidates and normalized_view != CONTENT_RESULT_VIEW,
            )
            matched = [
                item
                for item in legacy
                if self._include_item(
                    item,
                    category=category,
                    query=query,
                    result_view=normalized_view,
                    include_candidates=include_candidates,
                    include_blocked=include_blocked,
                    review_status=review_status,
                    citation_status=citation_status,
                    source_trust=source_trust,
                    problem_status=problem_status,
                )
            ]
            ordered = _sort_legacy_items(matched, query=query)
            total = len(ordered)
            counts = Counter(item.category for item in ordered)
            distinct_paper_count = len({item.paper_id for item in ordered if item.paper_id})
            items = ordered[offset_value:offset_value + limit_value]

        return {
            "schema_version": CONTENT_KNOWLEDGE_SCHEMA_VERSION,
            "result_view": normalized_view,
            "result_item_count": len(items),
            "distinct_paper_count": distinct_paper_count,
            "count_semantics": {
                "result_items": "Evidence projection records matching the current view and filters; this is not a paper count.",
                "distinct_papers": "Distinct papers represented by the full filtered result, not only the current page.",
                "review_objects": "Objects included in a generated review bundle and checked individually.",
                "unique_evidence_pages": "Deduplicated PDF pages across review objects; multiple objects may share one page.",
            },
            "policy": {
                "source_of_truth": "postgresql",
                "verified_boundary": "AI candidates and raw extracted content stay needs_review until a safe review/correction path approves them.",
                "citation_policy_values": ["citable", "writing_only", "needs_review", "blocked"],
                "formal_citation_requires": "citation_status=citable, reviewed evidence, and a real PDF/page or section locator",
            },
            "filters": {
                "paper_id": str(paper_id) if paper_id else None,
                "run_id": str(run_uuid) if run_uuid else None,
                "library_name": normalized_library,
                "category": _clean_text(category),
                "query": _clean_text(query),
                "result_view": normalized_view,
                "include_candidates": include_candidates,
                "include_blocked": include_blocked,
                "review_status": review_status,
                "citation_status": citation_status,
                "source_trust": source_trust,
                "problem_status": problem_status,
                "offset": offset_value,
                "limit": limit_value,
            },
            "total": total,
            "offset": offset_value,
            "limit": limit_value,
            "has_more": offset_value + len(items) < total,
            "category_counts": dict(sorted(counts.items())),
            "items": [item.payload() for item in items],
        }

    def sync_items(
        self,
        *,
        paper_id: str | uuid.UUID | None = None,
        library_name: str | None = None,
        include_candidates: bool = True,
        source_types: Iterable[str] | None = None,
        source_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Materialize legacy source rows into the ContentEvidenceItem contract.

        The method never upgrades review/citation state. Existing decisions are
        preserved only while review-relevant source fields remain unchanged;
        changed accepted cards are invalidated and must be reviewed again.
        """
        papers = self._scoped_papers(
            paper_id=paper_id,
            library_name=normalize_library_name(library_name) if library_name is not None else None,
        )
        paper_by_id = {paper.id: paper for paper in papers}
        legacy = self._legacy_items(list(paper_by_id), paper_by_id, include_candidates=include_candidates)
        allowed_types = {str(value).strip() for value in (source_types or []) if str(value).strip()}
        allowed_ids = {str(value).strip() for value in (source_ids or []) if str(value).strip()}
        if allowed_types:
            legacy = [item for item in legacy if item.source_type in allowed_types]
        if allowed_ids:
            legacy = [item for item in legacy if str(item.source_id) in allowed_ids]
        created = updated = 0
        settings = get_settings()
        embedding = get_embedding_service(
            provider=settings.embedding_provider,
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
        for item in legacy:
            existing = self.session.scalar(
                select(ContentEvidenceItem).where(
                    ContentEvidenceItem.source_type == item.source_type,
                    ContentEvidenceItem.source_id == item.source_id,
                )
            )
            source_metadata = dict(item.metadata or {})
            # Figure links are request-time authorization data, not durable
            # projection state. Rebuild them from the canonical source object
            # and the current figure review gate whenever content is read.
            source_metadata.pop("linked_figures", None)
            source_run_id = _maybe_uuid(source_metadata.get("external_analysis_run_id"))
            if existing is None:
                citation_status = item.citation_policy
                # A legacy status may be informative, but it cannot create a
                # citable item without both raw evidence and a concrete locator.
                if citation_status == "citable" and not (item.evidence_text and (item.page_start or item.section_title)):
                    citation_status = "needs_review"
                try:
                    vector = embedding.embed_text(f"{item.content}\n{item.evidence_text or ''}")
                except Exception:
                    vector = None
                existing = ContentEvidenceItem(
                    paper_id=uuid.UUID(item.paper_id), category=item.category,
                    source_type=item.source_type, source_id=item.source_id,
                    source_record=source_metadata, content=item.content,
                    evidence_text=item.evidence_text, evidence_locator=item.evidence_locator,
                    page_start=item.page_start, page_end=item.page_end, section_title=item.section_title,
                    review_status=item.review_status, citation_status=citation_status,
                    risk_flags=item.risk_flags, source_identity=source_metadata.get("source_identity"),
                    source_identity_verified=bool(source_metadata.get("source_identity_verified")),
                    run_id=source_run_id,
                    snapshot_fingerprint=source_metadata.get("snapshot_fingerprint"),
                    embedding=vector, embedding_model=settings.embedding_model,
                )
                self.session.add(existing)
                created += 1
            else:
                merged_risks = list(dict.fromkeys([*(existing.risk_flags or []), *(item.risk_flags or [])]))
                changed_fields = _review_relevant_changes(
                    existing,
                    item,
                    source_metadata=source_metadata,
                    run_id=source_run_id if source_run_id is not None else existing.run_id,
                    risk_flags=merged_risks,
                )
                before_review = _stored_review_state(existing)
                existing.content = item.content
                existing.evidence_text = item.evidence_text
                existing.evidence_locator = item.evidence_locator
                existing.page_start = item.page_start
                existing.page_end = item.page_end
                existing.section_title = item.section_title
                existing.category = item.category
                existing.source_record = source_metadata
                if source_run_id is not None:
                    existing.run_id = source_run_id
                if "source_identity" in source_metadata:
                    existing.source_identity = source_metadata.get("source_identity")
                if "source_identity_verified" in source_metadata:
                    existing.source_identity_verified = bool(source_metadata.get("source_identity_verified"))
                if "snapshot_fingerprint" in source_metadata:
                    existing.snapshot_fingerprint = source_metadata.get("snapshot_fingerprint")
                existing.risk_flags = merged_risks
                if changed_fields and _has_effective_review(existing):
                    existing.review_status = "needs_review"
                    existing.citation_status = "needs_review"
                    existing.reviewer = None
                    existing.reviewed_at = None
                    existing.risk_flags = list(dict.fromkeys([*merged_risks, "source_changed_after_review"]))
                    self.session.add(
                        AuditLog(
                            paper_id=existing.paper_id,
                            action="invalidate_content_evidence_review",
                            source="content_knowledge_sync",
                            target_type="content_evidence_item",
                            target_id=str(existing.id),
                            payload={
                                "changed_fields": changed_fields,
                                "before": before_review,
                                "after": {
                                    "review_status": "needs_review",
                                    "citation_status": "needs_review",
                                    "reviewer": None,
                                    "reviewed_at": None,
                                },
                                "risk_flag": "source_changed_after_review",
                            },
                        )
                    )
                if changed_fields:
                    try:
                        existing.embedding = embedding.embed_text(f"{item.content}\n{item.evidence_text or ''}")
                        existing.embedding_model = settings.embedding_model
                    except Exception:
                        # Never retain a vector derived from changed source text.
                        existing.embedding = None
                        existing.embedding_model = None
                updated += 1
        self.session.flush()
        return {"created": created, "updated": updated, "total": len(legacy)}

    def search_for_rag(
        self,
        *,
        query: str,
        paper_ids: list[uuid.UUID] | None = None,
        include_review_assist: bool = False,
        limit: int = 20,
    ) -> list[tuple[ContentKnowledgeItem, dict[str, float]]]:
        """Hybrid content retrieval with DB scope/filtering before Python rerank."""
        stmt = select(ContentEvidenceItem).where(
            ContentEvidenceItem.source_type.not_in(AUDIT_SOURCE_TYPES)
        )
        if paper_ids:
            stmt = stmt.where(ContentEvidenceItem.paper_id.in_(paper_ids))
        terms = _search_terms(query)
        if terms:
            # OR is recall; BM25-like character n-gram scoring below supplies precision.
            stmt = stmt.where(or_(*[
                or_(ContentEvidenceItem.content.ilike(f"%{term}%"), ContentEvidenceItem.evidence_text.ilike(f"%{term}%"))
                for term in terms
            ]))
        candidate_limit = max(limit * 20, 100)
        rows = self.session.scalars(stmt.order_by(ContentEvidenceItem.updated_at.desc()).limit(candidate_limit)).all()
        scoped_paper_ids = [row.paper_id for row in rows]
        paper_by_id = {
            item.id: item
            for item in self.session.scalars(select(Paper).where(Paper.id.in_(scoped_paper_ids))).all()
        } if scoped_paper_ids else {}
        settings = get_settings()
        try:
            query_vector = get_embedding_service(
                provider=settings.embedding_provider, api_base=settings.embedding_api_base,
                api_key=settings.embedding_api_key, model=settings.embedding_model,
                dimension=settings.embedding_dimension,
            ).embed_text(query)
        except Exception:
            query_vector = None
        scored = []
        figure_links = ContentFigureLinkService(self.session)
        for row in rows:
            gate = content_object_gate(self.session, row.source_type, row)
            if not include_review_assist and not (
                gate.can_use_for_writing or gate.can_use_for_citation
            ):
                continue
            lexical = _bm25ish_score(query, f"{row.content} {row.evidence_text or ''}")
            vector = _cosine(query_vector, row.embedding)
            hybrid = round(0.68 * lexical + 0.32 * vector, 4)
            if hybrid > 0 or not terms:
                scored.append((
                    self._persistent_item(
                        row,
                        paper_by_id.get(row.paper_id),
                        object_gate=gate,
                        figure_links=figure_links,
                    ),
                    {"bm25": lexical, "vector": vector, "hybrid": hybrid},
                ))
        return sorted(scored, key=lambda pair: pair[1]["hybrid"], reverse=True)[:limit]

    def count_unreviewed_matching(
        self,
        *,
        query: str,
        paper_ids: list[uuid.UUID] | None = None,
    ) -> int:
        """Return the DB-scoped count excluded by the formal-citation review gate."""
        stmt = select(func.count()).select_from(ContentEvidenceItem).where(
            ContentEvidenceItem.review_status.in_(("needs_review", "needs_human")),
            ContentEvidenceItem.source_type.not_in(AUDIT_SOURCE_TYPES),
        )
        if paper_ids:
            stmt = stmt.where(ContentEvidenceItem.paper_id.in_(paper_ids))
        terms = _search_terms(query)
        if terms:
            stmt = stmt.where(or_(*[
                or_(ContentEvidenceItem.content.ilike(f"%{term}%"), ContentEvidenceItem.evidence_text.ilike(f"%{term}%"))
                for term in terms
            ]))
        return int(self.session.scalar(stmt) or 0)

    def _legacy_items(self, paper_ids, paper_by_id, *, include_candidates: bool) -> list[ContentKnowledgeItem]:
        items: list[ContentKnowledgeItem] = []
        if paper_ids:
            items.extend(self._mechanism_items(paper_ids, paper_by_id))
            items.extend(self._section_items(paper_ids, paper_by_id))
            items.extend(self._writing_card_items(paper_ids, paper_by_id))
            items.extend(self._paper_note_items(paper_ids, paper_by_id))
            items.extend(self._evidence_claim_items(paper_ids, paper_by_id))
            if include_candidates:
                items.extend(self._external_candidate_items(paper_ids, paper_by_id))
        return items

    def _persistent_items(
        self,
        *,
        paper_ids,
        run_id,
        category,
        query,
        result_view,
        include_candidates,
        include_blocked,
        review_status,
        citation_status,
        source_trust,
        problem_status,
        offset,
        limit,
    ):
        if not paper_ids:
            return [], 0, {}, 0
        terms = _search_terms(query)
        filters = content_item_filters(
            paper_ids=paper_ids,
            run_id=run_id,
            category=category,
            terms=terms,
            result_view=result_view,
            include_candidates=include_candidates,
            include_blocked=include_blocked,
            review_status=review_status,
            citation_status=citation_status,
            source_trust=source_trust,
            problem_status=problem_status,
        )
        total = int(
            self.session.scalar(
                select(func.count())
                .select_from(ContentEvidenceItem)
                .join(Paper, Paper.id == ContentEvidenceItem.paper_id)
                .where(*filters)
            )
            or 0
        )
        category_counts = {
            str(row_category): int(row_count)
            for row_category, row_count in self.session.execute(
                select(ContentEvidenceItem.category, func.count())
                .join(Paper, Paper.id == ContentEvidenceItem.paper_id)
                .where(*filters)
                .group_by(ContentEvidenceItem.category)
            ).all()
        }
        distinct_paper_count = int(
            self.session.scalar(
                select(func.count(func.distinct(ContentEvidenceItem.paper_id)))
                .select_from(ContentEvidenceItem)
                .join(Paper, Paper.id == ContentEvidenceItem.paper_id)
                .where(*filters)
            )
            or 0
        )
        stmt = (
            select(ContentEvidenceItem, Paper)
            .join(Paper, Paper.id == ContentEvidenceItem.paper_id)
            .where(*filters)
        )
        score = content_search_score(terms)
        if score is not None:
            stmt = stmt.order_by(
                score.desc(),
                ContentEvidenceItem.updated_at.desc(),
                ContentEvidenceItem.id.asc(),
            )
        else:
            stmt = stmt.order_by(
                ContentEvidenceItem.updated_at.desc(),
                ContentEvidenceItem.id.asc(),
            )
        rows = self.session.execute(stmt.offset(offset).limit(limit)).all()
        audit_candidate_by_id = self._audit_candidates_for_rows([item for item, _paper in rows])
        figure_links = ContentFigureLinkService(self.session)
        return [
            serialize_content_item(
                item,
                paper,
                object_gate=content_object_gate(self.session, item.source_type, item),
                session=self.session,
                figure_links=figure_links,
                audit_candidate=audit_candidate_by_id.get(item.source_id),
            )
            for item, paper in rows
        ], total, category_counts, distinct_paper_count

    def _persistent_item(
        self,
        row: ContentEvidenceItem,
        paper: Paper | None,
        *,
        object_gate: ContentObjectGateResult | None = None,
        figure_links: ContentFigureLinkService | None = None,
    ) -> ContentKnowledgeItem:
        return serialize_content_item(
            row,
            paper,
            object_gate=object_gate,
            session=self.session,
            figure_links=figure_links,
        )

    def _has_persistent_items(
        self,
        paper_ids: list[uuid.UUID],
        *,
        result_view: str,
        include_candidates: bool,
    ) -> bool:
        filters = content_item_filters(
            paper_ids=paper_ids,
            run_id=None,
            category=None,
            terms=[],
            result_view=result_view,
            include_candidates=include_candidates,
            include_blocked=True,
            review_status=None,
            citation_status=None,
            source_trust=None,
            problem_status=None,
        )
        return self.session.scalar(
            select(ContentEvidenceItem.id).where(*filters).limit(1)
        ) is not None

    def _audit_candidates_for_rows(
        self,
        rows: list[ContentEvidenceItem],
    ) -> dict[str, ExternalAnalysisCandidate]:
        candidate_ids = [
            candidate_id
            for row in rows
            if _is_audit_source_type(row.source_type)
            and (candidate_id := _maybe_uuid(row.source_id)) is not None
        ]
        if not candidate_ids:
            return {}
        return {
            str(candidate.id): candidate
            for candidate in self.session.scalars(
                select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.id.in_(candidate_ids))
            ).all()
        }

    def _scoped_papers(
        self,
        *,
        paper_id: str | uuid.UUID | None,
        library_name: str | None,
    ) -> list[Paper]:
        stmt = select(Paper)
        if paper_id:
            paper_uuid = _maybe_uuid(paper_id)
            if paper_uuid is not None:
                stmt = stmt.where(Paper.id == paper_uuid)
            else:
                stmt = stmt.where(Paper.paper_code == str(paper_id).strip())
        if library_name is not None:
            stmt = stmt.where(build_library_name_clause(Paper.library_name, library_name))
        return list(self.session.scalars(stmt.order_by(Paper.created_at.desc())).all())

    def _mechanism_items(
        self,
        paper_ids: list[uuid.UUID],
        paper_by_id: dict[uuid.UUID, Paper],
    ) -> list[ContentKnowledgeItem]:
        rows = self.session.scalars(
            select(MechanismClaim).where(MechanismClaim.paper_id.in_(paper_ids)).limit(500)
        ).all()
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="mechanism_claims")
        figure_links = ContentFigureLinkService(self.session)
        items: list[ContentKnowledgeItem] = []
        for row in rows:
            paper = paper_by_id.get(row.paper_id)
            content = _clean_text(row.claim_text)
            gate = gate_by_id[str(row.id)]
            risks = list(gate.reasons)
            if not _clean_text(row.evidence_text):
                risks.append("missing_evidence_text")
            items.append(
                self._item(
                    paper,
                    category="mechanism_evidence",
                    source_type="mechanism_claim",
                    source_id=row.id,
                    source_table="mechanism_claims",
                    content=content,
                    evidence_text=row.evidence_text,
                    review_status=gate.review_status,
                    review_gate_status=gate.review_gate_status,
                    citation_policy="citable" if gate.eligible else "needs_review",
                    can_use_for_writing=bool(content and gate.eligible),
                    can_use_for_citation=gate.eligible,
                    risk_flags=risks,
                    recommended_action="review_mechanism_claim_evidence",
                    updated_at=getattr(row, "updated_at", getattr(row, "created_at", None)),
                    metadata={
                        "claim_type": row.claim_type,
                        "confidence": row.confidence,
                        "evidence_types": row.evidence_types or [],
                        "ai_verification": _ai_verification_metadata(
                            self.session, "mechanism_claims", row
                        ),
                        "linked_figures": figure_links.links_for_mechanism_claim(row),
                    },
                )
            )
        return items

    def _writing_card_items(
        self,
        paper_ids: list[uuid.UUID],
        paper_by_id: dict[uuid.UUID, Paper],
    ) -> list[ContentKnowledgeItem]:
        rows = self.session.scalars(
            select(WritingCard).where(WritingCard.paper_id.in_(paper_ids)).limit(500)
        ).all()
        figure_links = ContentFigureLinkService(self.session)
        items: list[ContentKnowledgeItem] = []
        for row in rows:
            paper = paper_by_id.get(row.paper_id)
            content = _writing_card_content(row)
            content_gate = writing_card_content_gate(row)
            gate = writing_card_gate(self.session, row)
            can_write = bool(gate.can_use_for_writing and content)
            risks = list(gate.blocked_reasons)
            citation_policy = "writing_only" if can_write else "needs_review"
            review_status = "safe_verified" if can_write else "needs_review"
            items.append(
                self._item(
                    paper,
                    category="writing_material",
                    source_type="writing_card",
                    source_id=row.id,
                    source_table="writing_cards",
                    content=content,
                    evidence_text=_evidence_preview(row.evidence_chain),
                    evidence_locator=_first_locator(row.evidence_chain),
                    review_status=review_status,
                    review_gate_status=gate.review_gate_status,
                    candidate_status="reviewed_exportable" if can_write else "candidate_unverified",
                    citation_policy=citation_policy,
                    can_use_for_writing=can_write,
                    can_use_for_citation=False,
                    risk_flags=risks,
                    recommended_action=None if can_write else "complete_writing_card_evidence_chain",
                    updated_at=getattr(row, "updated_at", getattr(row, "created_at", None)),
                    metadata={
                        "paper_type": row.paper_type,
                        "evidence_chain_status": content_gate.evidence_chain_status,
                        "evidence_chain": normalized_evidence_chain(row.evidence_chain, limit=8),
                        "ai_verification": _ai_verification_metadata(
                            self.session, "writing_cards", row
                        ),
                        "linked_figures": figure_links.links_for_writing_card(row),
                    },
                )
            )
        return items

    def _section_items(
        self,
        paper_ids: list[uuid.UUID],
        paper_by_id: dict[uuid.UUID, Paper],
    ) -> list[ContentKnowledgeItem]:
        rows = self.session.scalars(
            select(PaperSection).where(PaperSection.paper_id.in_(paper_ids)).limit(500)
        ).all()
        if not rows:
            return []
        gate_by_id = bulk_export_gate_results(self.session, rows, target_type="sections")
        target_ids = {str(row.id) for row in rows}
        locators_by_target: dict[str, list[EvidenceLocator]] = {target_id: [] for target_id in target_ids}
        for locator in self.session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id.in_(paper_ids),
                EvidenceLocator.target_type.in_(["sections", "section", "paper_section", "PaperSection"]),
                EvidenceLocator.target_id.in_(target_ids),
                EvidenceLocator.field_name == "text",
                EvidenceLocator.locator_status.in_(["exact_page", "exact_bbox"]),
            )
        ).all():
            locators_by_target.setdefault(str(locator.target_id), []).append(locator)

        items: list[ContentKnowledgeItem] = []
        for row in rows:
            paper = paper_by_id.get(row.paper_id)
            content = _clean_text(row.text)
            gate = gate_by_id[str(row.id)]
            locators = sorted(
                locators_by_target.get(str(row.id), []),
                key=lambda locator: (int(locator.page or 0), str(locator.id)),
            )
            locator = locators[0] if locators else None
            locator_payload = (
                {
                    "page": locator.page,
                    "locator_status": locator.locator_status,
                    "evidence_text": locator.evidence_text,
                }
                if locator is not None
                else None
            )
            items.append(
                self._item(
                    paper,
                    category="writing_material",
                    source_type="section",
                    source_id=row.id,
                    source_table="paper_sections",
                    content=content,
                    evidence_text=content,
                    evidence_locator=locator_payload,
                    page_start=locator.page if locator is not None else row.page_start,
                    page_end=locator.page if locator is not None else row.page_end,
                    section_title=row.section_title,
                    review_status=gate.review_status,
                    review_gate_status=gate.review_gate_status,
                    candidate_status="reviewed_exportable" if gate.eligible else "candidate_unverified",
                    citation_policy="citable" if gate.eligible else "needs_review",
                    can_use_for_writing=bool(content and gate.eligible),
                    can_use_for_citation=gate.eligible,
                    risk_flags=list(gate.reasons),
                    recommended_action=None if gate.eligible else "verify_section_text_evidence",
                    updated_at=getattr(row, "updated_at", getattr(row, "created_at", None)),
                    metadata={
                        "section_type": row.section_type,
                        "ai_verification": _ai_verification_metadata(self.session, "sections", row),
                    },
                )
            )
        return items

    def _paper_note_items(
        self,
        paper_ids: list[uuid.UUID],
        paper_by_id: dict[uuid.UUID, Paper],
    ) -> list[ContentKnowledgeItem]:
        rows = self.session.scalars(
            select(PaperNote).where(PaperNote.paper_id.in_(paper_ids)).order_by(PaperNote.created_at.desc()).limit(500)
        ).all()
        items: list[ContentKnowledgeItem] = []
        for row in rows:
            paper = paper_by_id.get(row.paper_id)
            content = _clean_text(row.content)
            category = "uncertainty_note" if _looks_uncertain(content) else "draft_evidence_check"
            items.append(
                self._item(
                    paper,
                    category=category,
                    source_type="paper_note",
                    source_id=row.id,
                    source_table="paper_notes",
                    content=content,
                    evidence_text=row.quoted_text,
                    evidence_locator={"page": row.page, "section_title": row.section_title}
                    if row.page or row.section_title
                    else None,
                    page_start=row.page,
                    page_end=row.page,
                    section_title=row.section_title,
                    review_status="needs_review",
                    review_gate_status="needs_review",
                    citation_policy="needs_review",
                    can_use_for_writing=bool(content),
                    can_use_for_citation=False,
                    risk_flags=["note_requires_review"],
                    recommended_action="review_note_before_writing",
                    updated_at=getattr(row, "updated_at", getattr(row, "created_at", None)),
                    metadata={"source": row.source, "field_name": row.field_name},
                )
            )
        return items

    def _evidence_claim_items(
        self,
        paper_ids: list[uuid.UUID],
        paper_by_id: dict[uuid.UUID, Paper],
    ) -> list[ContentKnowledgeItem]:
        rows = self.session.scalars(
            select(EvidenceClaim)
            .where(EvidenceClaim.paper_id.in_(paper_ids))
            .order_by(EvidenceClaim.created_at.desc())
            .limit(500)
        ).all()
        items: list[ContentKnowledgeItem] = []
        for row in rows:
            if _normalized(row.source_type) == "content_writing_plan":
                # A writing plan is derived output, never a new evidence source.
                continue
            paper = paper_by_id.get(row.paper_id)
            if _normalized(row.source_type) == "section_page_fragment":
                gate = content_object_gate(self.session, "section_page_fragments", row)
                locator = self.session.scalar(
                    select(EvidenceLocator)
                    .where(
                        EvidenceLocator.paper_id == row.paper_id,
                        EvidenceLocator.target_type.in_(
                            ["section_page_fragments", "section_page_fragment"]
                        ),
                        EvidenceLocator.target_id == str(row.id),
                        EvidenceLocator.field_name == "text",
                        EvidenceLocator.page == row.page_start,
                        EvidenceLocator.locator_status.in_(["exact_page", "exact_bbox"]),
                    )
                    .order_by(EvidenceLocator.id.asc())
                )
                parent = self.session.get(PaperSection, row.section_id) if row.section_id else None
                items.append(
                    self._item(
                        paper,
                        category="writing_material",
                        source_type="section_page_fragment",
                        source_id=row.id,
                        source_table="evidence_claims",
                        content=_clean_text(row.claim_text),
                        evidence_text=row.evidence_text,
                        evidence_locator=(
                            {
                                "page": locator.page,
                                "page_start": locator.page,
                                "page_end": locator.page,
                                "locator_status": locator.locator_status,
                                "evidence_text": locator.evidence_text,
                            }
                            if locator is not None
                            else {
                                "page": row.page_start,
                                "page_start": row.page_start,
                                "page_end": row.page_end,
                                "locator_status": "missing",
                            }
                        ),
                        page_start=row.page_start,
                        page_end=row.page_end,
                        section_title=getattr(parent, "section_title", None),
                        review_status=(
                            "safe_verified" if gate.review_gate_status == "safe_verified" else "needs_review"
                        ),
                        review_gate_status=gate.review_gate_status,
                        candidate_status=(
                            "reviewed_exportable"
                            if gate.can_use_for_writing or gate.can_use_for_citation
                            else "candidate_unverified"
                        ),
                        citation_policy=(
                            "citable" if gate.can_use_for_citation else "needs_review"
                        ),
                        can_use_for_writing=gate.can_use_for_writing,
                        can_use_for_citation=gate.can_use_for_citation,
                        risk_flags=list(gate.blocked_reasons),
                        recommended_action=(
                            None
                            if gate.can_use_for_writing or gate.can_use_for_citation
                            else "verify_section_page_fragment"
                        ),
                        updated_at=getattr(row, "created_at", None),
                        metadata={
                            "parent_section_id": str(row.section_id) if row.section_id else None,
                            "section_type": getattr(parent, "section_type", None),
                            "physical_page_numbering": "1_based_pdf",
                            "fragment_metadata": row.meta or {},
                            "ai_verification": _ai_verification_metadata(
                                self.session,
                                "section_page_fragments",
                                row,
                            ),
                        },
                    )
                )
                continue
            status = _normalized(row.validation_status) or "unverified"
            is_citable = status in CITABLE_EVIDENCE_STATUSES
            category = _category_from_hint(row.target_type or row.source_type)
            items.append(
                self._item(
                    paper,
                    category=category,
                    source_type="evidence_claim",
                    source_id=row.id,
                    source_table="evidence_claims",
                    content=_clean_text(row.claim_text),
                    evidence_text=row.evidence_text,
                    evidence_locator={
                        "chunk_id": row.chunk_id,
                        "page_start": row.page_start,
                        "page_end": row.page_end,
                    },
                    page_start=row.page_start,
                    page_end=row.page_end,
                    review_status="validated" if is_citable else "needs_review",
                    review_gate_status=status,
                    citation_policy="citable" if is_citable else "needs_review",
                    can_use_for_writing=bool(_clean_text(row.claim_text)),
                    can_use_for_citation=is_citable,
                    risk_flags=[] if is_citable else ["evidence_claim_unverified"],
                    recommended_action=None if is_citable else "review_evidence_claim",
                    updated_at=getattr(row, "updated_at", getattr(row, "created_at", None)),
                    metadata={
                        "target_type": row.target_type,
                        "target_id": row.target_id,
                        "source_type": row.source_type,
                        "confidence": row.confidence,
                        "metadata": row.meta or {},
                    },
                )
            )
        return items

    def _external_candidate_items(
        self,
        paper_ids: list[uuid.UUID],
        paper_by_id: dict[uuid.UUID, Paper],
    ) -> list[ContentKnowledgeItem]:
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id.in_(paper_ids))
            .order_by(ExternalAnalysisCandidate.created_at.desc())
            .limit(500)
        ).all()
        items: list[ContentKnowledgeItem] = []
        for candidate, run in rows:
            paper = paper_by_id.get(candidate.paper_id)
            status = _normalized(candidate.status) or "pending"
            risks = _candidate_risks(status)
            category = _external_candidate_category(candidate)
            policy = "blocked" if status in BLOCKED_CANDIDATE_STATUSES else "needs_review"
            content = _candidate_content(candidate)
            audit = candidate_audit_semantics(
                status,
                target_type=candidate.materialized_target_type,
                target_id=candidate.materialized_target_id,
            )
            items.append(
                self._item(
                    paper,
                    category=category,
                    source_type="external_analysis_candidate",
                    source_id=candidate.id,
                    source_table="external_analysis_candidates",
                    content=content,
                    evidence_text=_evidence_preview(candidate.evidence_payload),
                    evidence_locator=_first_locator(candidate.evidence_payload),
                    review_status="needs_review",
                    review_gate_status="needs_review",
                    candidate_status=status,
                    citation_policy=policy,
                    can_use_for_writing=False,
                    can_use_for_citation=False,
                    risk_flags=risks,
                    recommended_action=audit["recommended_action"],
                    source_ai=run.source,
                    source_label=run.source_label,
                    updated_at=getattr(candidate, "updated_at", getattr(candidate, "created_at", None)),
                    metadata={
                        "candidate_type": candidate.candidate_type,
                        "mapping_reason": candidate.mapping_reason,
                        "confidence": candidate.confidence,
                        "mapping_status": run.mapping_status,
                        "source_ai": run.source,
                        "source_label": run.source_label,
                        "source_identity": run.source_identity,
                        "source_identity_verified": run.source_identity_verified,
                        "external_analysis_run_id": str(run.id),
                        "external_analysis_candidate_id": str(candidate.id),
                        "candidate_status": status,
                        "materialized_target_type": candidate.materialized_target_type,
                        "materialized_target_id": candidate.materialized_target_id,
                        "audit_lifecycle": audit,
                        "normalized_payload": candidate.normalized_payload,
                    },
                )
            )
        return items

    def _item(
        self,
        paper: Paper | None,
        *,
        category: str,
        source_type: str,
        source_id: Any,
        source_table: str,
        content: str | None,
        evidence_text: str | None = None,
        evidence_locator: dict[str, Any] | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        section_title: str | None = None,
        review_status: str = "needs_review",
        review_gate_status: str = "needs_review",
        candidate_status: str | None = None,
        citation_policy: str = "needs_review",
        can_use_for_writing: bool = False,
        can_use_for_citation: bool = False,
        risk_flags: list[str] | None = None,
        recommended_action: str | None = None,
        source_ai: str | None = None,
        source_label: str | None = None,
        updated_at: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContentKnowledgeItem:
        item_category = category if category in CATEGORY_LABELS else "draft_evidence_check"
        paper_id = str(paper.id) if paper is not None else ""
        item_id = f"{source_type}:{source_id}"
        audit = (metadata or {}).get("audit_lifecycle") if _is_audit_source_type(source_type) else None
        return ContentKnowledgeItem(
            item_id=item_id,
            paper_id=paper_id,
            paper_code=getattr(paper, "paper_code", None),
            paper_title=getattr(paper, "title", None),
            paper_doi=getattr(paper, "doi", None),
            category=item_category,
            category_label=CATEGORY_LABELS[item_category],
            source_type=source_type,
            source_id=str(source_id),
            source_table=source_table,
            reviewable=False,
            requires_sync=True,
            content=_clean_text(content),
            evidence_text=_clean_text(evidence_text) or None,
            evidence_locator=evidence_locator,
            page_start=page_start,
            page_end=page_end,
            section_title=section_title,
            review_status=review_status,
            review_gate_status=review_gate_status,
            candidate_status=candidate_status,
            citation_policy=citation_policy,
            can_use_for_writing=bool(can_use_for_writing),
            can_use_for_citation=bool(can_use_for_citation),
            risk_flags=list(dict.fromkeys(risk_flags or [])),
            recommended_action=recommended_action,
            source_ai=source_ai,
            source_label=source_label,
            source_identity=(metadata or {}).get("source_identity"),
            source_identity_verified=bool((metadata or {}).get("source_identity_verified")),
            updated_at=updated_at.isoformat() if updated_at else None,
            item_kind="audit" if audit else "content",
            audit_state=(audit or {}).get("state"),
            audit_state_label=(audit or {}).get("label"),
            audit_requires_action=bool((audit or {}).get("requires_action")),
            linked_target_type=(audit or {}).get("target_type"),
            linked_target_id=(audit or {}).get("target_id"),
            metadata=metadata or {},
        )

    def _include_item(
        self,
        item: ContentKnowledgeItem,
        *,
        category: str | None,
        query: str | None,
        result_view: str,
        include_candidates: bool,
        include_blocked: bool,
        review_status: str | None,
        citation_status: str | None,
        source_trust: str | None,
        problem_status: str | None,
    ) -> bool:
        if result_view == CONTENT_RESULT_VIEW and item.item_kind == "audit":
            return False
        if result_view == AUDIT_RESULT_VIEW and item.item_kind != "audit":
            return False
        if category and item.category != category:
            return False
        if not include_candidates and item.source_type == "external_analysis_candidate":
            return False
        if (
            result_view != AUDIT_RESULT_VIEW
            and not include_blocked
            and item.citation_policy == "blocked"
        ):
            return False
        if review_status and item.review_status != review_status:
            return False
        if citation_status and item.citation_policy != citation_status:
            return False
        if source_trust == "verified" and not item.source_identity_verified:
            return False
        if source_trust == "unverified" and item.source_identity_verified:
            return False
        if problem_status == "has_risk" and not item.risk_flags:
            return False
        tokens = _query_tokens(query)
        if tokens and not all(token in _search_blob(item) for token in tokens):
            return False
        return True


def serialize_content_item(
    row: ContentEvidenceItem,
    paper: Paper | None,
    *,
    object_gate: ContentObjectGateResult | None = None,
    session: Session | None = None,
    figure_links: ContentFigureLinkService | None = None,
    audit_candidate: ExternalAnalysisCandidate | None = None,
) -> ContentKnowledgeItem:
    citation = _normalized(row.citation_status) or "needs_review"
    reviewed = _normalized(row.review_status) in {"validated", "approved", "safe_verified"}
    metadata = dict(row.source_record or {})
    # Never expose a cached authorization decision. Even if an old projection
    # contains linked_figures, current output is rebuilt from the real source
    # row and the latest figure review state.
    metadata.pop("linked_figures", None)
    if session is not None:
        live_links = figure_links or ContentFigureLinkService(session)
        metadata["linked_figures"] = live_links.links_for_content_source(
            paper_id=row.paper_id,
            source_type=row.source_type,
            source_id=row.source_id,
        )
    metadata["projection_state"] = {
        "review_status": row.review_status,
        "citation_status": citation,
        "reviewer": row.reviewer,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }
    if row.run_id is not None:
        metadata.setdefault("external_analysis_run_id", str(row.run_id))
    risk_flags = list(row.risk_flags or [])
    is_audit = _is_audit_source_type(row.source_type)
    candidate_status = None
    audit = None
    if is_audit:
        candidate_status = _normalized(
            getattr(audit_candidate, "status", None) or metadata.get("candidate_status")
        ) or "unknown"
        target_type = (
            getattr(audit_candidate, "materialized_target_type", None)
            if audit_candidate is not None
            else metadata.get("materialized_target_type")
        )
        target_id = (
            getattr(audit_candidate, "materialized_target_id", None)
            if audit_candidate is not None
            else metadata.get("materialized_target_id")
        )
        audit = candidate_audit_semantics(
            candidate_status,
            target_type=target_type,
            target_id=target_id,
        )
        metadata["candidate_status"] = candidate_status
        metadata["materialized_target_type"] = target_type
        metadata["materialized_target_id"] = target_id
        metadata["audit_lifecycle"] = audit
        can_write = False
        can_cite = False
        review_gate_status = "audit_only_not_writing_or_citation"
        effective_citation = "blocked"
        risk_flags = list(dict.fromkeys([*risk_flags, "audit_only_not_writing_or_citation"]))
        recommended_action = audit["recommended_action"]
    elif object_gate is None:
        can_write = False
        can_cite = False
        review_gate_status = "projection_only"
        effective_citation = citation
        risk_flags = list(dict.fromkeys([*risk_flags, "content_projection_non_authoritative"]))
        recommended_action = "review_source_object"
    else:
        can_write = object_gate.can_use_for_writing
        can_cite = object_gate.can_use_for_citation
        review_gate_status = object_gate.review_gate_status
        effective_citation = "citable" if can_cite else "writing_only" if can_write else "blocked"
        projection_claims_access = citation in {"citable", "writing_only"} or reviewed
        if projection_claims_access and not (can_write or can_cite):
            risk_flags.append("content_projection_gate_mismatch")
        elif (can_write or can_cite) and not projection_claims_access:
            risk_flags.append("content_projection_cache_stale")
        risk_flags.extend(object_gate.blocked_reasons)
        risk_flags = list(dict.fromkeys(risk_flags))
        metadata["content_object_gate"] = {
            "can_use_for_writing": can_write,
            "can_use_for_citation": can_cite,
            "review_gate_status": review_gate_status,
            "locator_status": object_gate.locator_status,
            "blocked_reasons": list(object_gate.blocked_reasons),
            "policy_version": object_gate.policy_version,
        }
        recommended_action = None if can_write or can_cite else "review_source_object"
    return ContentKnowledgeItem(
        item_id=str(row.id),
        paper_id=str(row.paper_id),
        paper_code=getattr(paper, "paper_code", None),
        paper_title=getattr(paper, "title", None),
        paper_doi=getattr(paper, "doi", None),
        category=row.category,
        category_label=CATEGORY_LABELS.get(row.category, row.category),
        source_type=row.source_type,
        source_id=row.source_id,
        source_table="content_evidence_items",
        reviewable=not is_audit,
        requires_sync=False,
        content=_clean_text(row.content),
        evidence_text=_clean_text(row.evidence_text) or None,
        evidence_locator=row.evidence_locator,
        page_start=row.page_start,
        page_end=row.page_end,
        section_title=row.section_title,
        review_status=row.review_status,
        review_gate_status=review_gate_status,
        candidate_status=candidate_status,
        citation_policy=effective_citation,
        can_use_for_writing=can_write,
        can_use_for_citation=can_cite,
        risk_flags=risk_flags,
        recommended_action=recommended_action,
        source_ai=metadata.get("source_ai"),
        source_label=metadata.get("source_label"),
        source_identity=row.source_identity,
        source_identity_verified=bool(row.source_identity_verified),
        reviewer=row.reviewer,
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
        snapshot_fingerprint=row.snapshot_fingerprint,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        item_kind="audit" if is_audit else "content",
        audit_state=(audit or {}).get("state"),
        audit_state_label=(audit or {}).get("label"),
        audit_requires_action=bool((audit or {}).get("requires_action")),
        linked_target_type=(audit or {}).get("target_type"),
        linked_target_id=(audit or {}).get("target_id"),
        metadata=metadata,
    )


def _review_relevant_changes(
    stored: ContentEvidenceItem,
    incoming: ContentKnowledgeItem,
    *,
    source_metadata: dict[str, Any],
    run_id: uuid.UUID | None,
    risk_flags: list[str],
) -> list[str]:
    current_risks = [flag for flag in (stored.risk_flags or []) if flag != "source_changed_after_review"]
    incoming_risks = [flag for flag in risk_flags if flag != "source_changed_after_review"]
    source_identity = source_metadata.get("source_identity", stored.source_identity)
    source_identity_verified = source_metadata.get(
        "source_identity_verified", stored.source_identity_verified
    )
    snapshot_fingerprint = source_metadata.get(
        "snapshot_fingerprint", stored.snapshot_fingerprint
    )
    values = {
        "category": (stored.category, incoming.category),
        "content": (stored.content, incoming.content),
        "evidence_text": (stored.evidence_text, incoming.evidence_text),
        "evidence_locator": (stored.evidence_locator, incoming.evidence_locator),
        "page_start": (stored.page_start, incoming.page_start),
        "page_end": (stored.page_end, incoming.page_end),
        "section_title": (stored.section_title, incoming.section_title),
        "source_record": (stored.source_record or {}, source_metadata),
        "run_id": (stored.run_id, run_id),
        "source_identity": (stored.source_identity, source_identity),
        "source_identity_verified": (
            stored.source_identity_verified,
            bool(source_identity_verified),
        ),
        "snapshot_fingerprint": (stored.snapshot_fingerprint, snapshot_fingerprint),
        "risk_flags": (current_risks, incoming_risks),
    }
    return [field_name for field_name, (before, after) in values.items() if before != after]


def _content_item_has_locator(item: ContentEvidenceItem) -> bool:
    locator = item.evidence_locator if isinstance(item.evidence_locator, dict) else {}
    return bool(
        item.page_start
        or str(item.section_title or "").strip()
        or locator.get("page")
        or locator.get("page_start")
        or locator.get("section")
        or locator.get("section_title")
    )


def _ai_verification_metadata(session: Session, target_type: str, target: Any) -> dict[str, Any] | None:
    reviews = get_target_reviews(
        session,
        paper_id=target.paper_id,
        target_type=target_type,
        target_id=target.id,
    )
    for review in reviews:
        if str(review.reviewer_status or "").casefold() != "ai_verified":
            continue
        payload = review.review_payload if isinstance(review.review_payload, dict) else {}
        verification = payload.get("ai_verification")
        if isinstance(verification, dict):
            return verification
    return None


def _has_effective_review(item: ContentEvidenceItem) -> bool:
    return (
        _normalized(item.review_status) in {"validated", "approved", "safe_verified"}
        or _normalized(item.citation_status) in {"citable", "writing_only"}
    )


def _stored_review_state(item: ContentEvidenceItem) -> dict[str, str | None]:
    return {
        "review_status": item.review_status,
        "citation_status": item.citation_status,
        "reviewer": item.reviewer,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
    }


def _sort_legacy_items(items: list[ContentKnowledgeItem], *, query: str | None) -> list[ContentKnowledgeItem]:
    ordered = sorted(items, key=lambda item: item.item_id)
    ordered.sort(key=lambda item: item.updated_at or "", reverse=True)
    terms = _search_terms(query)
    if terms:
        ordered.sort(key=lambda item: _legacy_search_score(item, terms), reverse=True)
    return ordered


def _legacy_search_score(item: ContentKnowledgeItem, terms: list[str]) -> int:
    score = 0
    fields = (
        (item.paper_title, 12),
        (item.content, 10),
        (item.evidence_text, 9),
        (item.section_title, 7),
        (item.category, 5),
        (item.source_type, 4),
        (item.paper_code, 15),
        (item.paper_doi, 14),
    )
    for term in terms:
        if _normalized(item.paper_code) == term:
            score += 40
        if _normalized(item.paper_doi) == term:
            score += 35
        score += sum(weight for value, weight in fields if term in _normalized(value))
    return score


def _normalize_result_view(value: Any) -> str:
    normalized = _normalized(value) or CONTENT_RESULT_VIEW
    if normalized not in RESULT_VIEWS:
        raise ValueError(f"unsupported result_view: {value}")
    return normalized


def _is_audit_source_type(source_type: Any) -> bool:
    return _normalized(source_type) in AUDIT_SOURCE_TYPES


def candidate_audit_semantics(
    status: Any,
    *,
    target_type: Any = None,
    target_id: Any = None,
) -> dict[str, Any]:
    """Derive display-only audit lifecycle state without mutating or guessing links."""
    normalized_status = _normalized(status) or "unknown"
    normalized_target_type = _clean_text(target_type) or None
    normalized_target_id = _clean_text(target_id) or None
    has_explicit_link = bool(normalized_target_type and normalized_target_id)

    if normalized_status in APPLIED_AUDIT_STATUSES and has_explicit_link:
        is_dft = _normalized(normalized_target_type) == "dft_results"
        return {
            "state": "applied_to_formal_dft" if is_dft else "applied_to_linked_object",
            "label": "已应用到正式 DFT / 已归档审计" if is_dft else "已应用到关联对象 / 已归档审计",
            "requires_action": False,
            "terminal": True,
            "status": normalized_status,
            "target_type": normalized_target_type,
            "target_id": normalized_target_id,
            "linkage_explicit": True,
            "recommended_action": "view_linked_audit_target",
        }
    if normalized_status in TERMINAL_AUDIT_STATUSES:
        return {
            "state": "terminal_history",
            "label": "终态 / 历史审计记录",
            "requires_action": False,
            "terminal": True,
            "status": normalized_status,
            "target_type": normalized_target_type,
            "target_id": normalized_target_id,
            "linkage_explicit": has_explicit_link,
            "recommended_action": "view_audit_history",
        }
    if normalized_status in ACTIVE_AUDIT_STATUSES:
        return {
            "state": "active_unresolved",
            "label": "待处理审计候选",
            "requires_action": True,
            "terminal": False,
            "status": normalized_status,
            "target_type": normalized_target_type,
            "target_id": normalized_target_id,
            "linkage_explicit": has_explicit_link,
            "recommended_action": "resolve_external_ai_candidate",
        }
    return {
        "state": "unknown_requires_attention",
        "label": "审计状态未知 / 需处理",
        "requires_action": True,
        "terminal": False,
        "status": normalized_status,
        "target_type": normalized_target_type,
        "target_id": normalized_target_id,
        "linkage_explicit": has_explicit_link,
        "recommended_action": "inspect_external_ai_candidate",
        "warning": (
            "materialized_status_missing_explicit_target_link"
            if normalized_status in APPLIED_AUDIT_STATUSES
            else "unrecognized_candidate_status"
        ),
    }


def _maybe_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return _json_preview(value)


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


def _query_tokens(query: str | None) -> list[str]:
    return _search_terms(query)


def _search_terms(query: str | None) -> list[str]:
    """Keep CJK runs and mixed scientific tokens searchable without jieba."""
    text = _normalized(query)
    if not text:
        return []
    import re
    terms = re.findall(r"[\u3400-\u9fff]+|[a-z0-9][a-z0-9+./_-]*", text, flags=re.I)
    return list(dict.fromkeys(term for term in terms if term))


def _char_ngrams(value: str) -> set[str]:
    compact = "".join(str(value or "").casefold().split())
    if not compact:
        return set()
    grams = {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}
    return grams or {compact}


def _bm25ish_score(query: str, text: str) -> float:
    qgrams, tgrams = _char_ngrams(query), _char_ngrams(text)
    if not qgrams or not tgrams:
        return 0.0
    overlap = len(qgrams & tgrams)
    if not overlap:
        return 0.0
    # Saturating term-frequency approximation; PostgreSQL has already limited
    # the candidate set using indexed scope/status and lexical predicates.
    return round(min(1.0, (overlap / len(qgrams)) * (1.0 / (1.0 + len(tgrams) / 600))), 4)


def _cosine(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    import math
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return round(max(0.0, sum(a * b for a, b in zip(left, right)) / denominator), 4) if denominator else 0.0


def _search_blob(item: ContentKnowledgeItem) -> str:
    return _normalized(
        " ".join(
            [
                item.content,
                item.evidence_text or "",
                item.section_title or "",
                item.paper_title or "",
                item.paper_code or "",
                item.paper_doi or "",
                item.category,
                item.source_type,
            ]
        )
    )


def _writing_card_content(card: WritingCard) -> str:
    parts = []
    for label, field_name in (
        ("research_gap", "research_gap"),
        ("proposed_solution", "proposed_solution"),
        ("core_hypothesis", "core_hypothesis"),
    ):
        value = _clean_text(getattr(card, field_name, None))
        if value:
            parts.append(f"{label}: {value}")
    for item in normalized_evidence_chain(card.evidence_chain, limit=8):
        if item["supports_fields"]:
            continue
        parts.append(f"{item['evidence_type']}: {item['text']}")
    return " | ".join(parts)


def _evidence_preview(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _clean_text(value) or None
    if isinstance(value, dict):
        for key in ("evidence_text", "quoted_text", "text", "content", "reason"):
            text = _clean_text(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        texts = []
        for item in value:
            text = _evidence_preview(item)
            if text:
                texts.append(text)
        return " | ".join(texts[:3]) or None
    return _json_preview(value) or None


def _first_locator(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        locator = value.get("evidence_locator")
        if isinstance(locator, dict):
            return locator
        keys = {
            "page",
            "page_start",
            "page_end",
            "section",
            "section_title",
            "figure",
            "table",
            "locator_status",
            "bbox",
            "can_jump_to_pdf_page",
        }
        payload = {key: value.get(key) for key in keys if value.get(key) is not None}
        return payload or None
    if isinstance(value, list):
        for item in value:
            locator = _first_locator(item)
            if locator:
                return locator
    return None


def _json_preview(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def _looks_uncertain(content: str) -> bool:
    lowered = _normalized(content)
    markers = ("risk", "conflict", "uncertain", "problem", "mismatch", "问题", "冲突", "争议", "不确定")
    return any(marker in lowered for marker in markers)


def _category_from_hint(hint: Any) -> str:
    text = _normalized(hint)
    if "dft" in text:
        return "dft_evidence"
    if "figure" in text or "table" in text:
        return "figure_table_evidence"
    if "mechanism" in text:
        return "mechanism_evidence"
    if "electrochemical" in text or "performance" in text or "capacity" in text:
        return "performance_evidence"
    if "catalyst" in text or "material" in text or "sample" in text:
        return "material_evidence"
    if "method" in text or "synthesis" in text:
        return "method_evidence"
    if "writing" in text or "card" in text:
        return "writing_material"
    return "draft_evidence_check"


def _external_candidate_category(candidate: ExternalAnalysisCandidate) -> str:
    payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
    hints = [
        candidate.candidate_type,
        candidate.materialized_target_type,
        payload.get("target_type"),
        payload.get("target_path"),
        payload.get("field_name"),
        payload.get("category"),
    ]
    return _category_from_hint(" ".join(_clean_text(hint) for hint in hints if hint))


def _candidate_risks(status: str) -> list[str]:
    risks = []
    if status in PROBLEM_CANDIDATE_STATUSES:
        risks.append(f"candidate_{status}")
    return risks


def _candidate_content(candidate: ExternalAnalysisCandidate) -> str:
    payload = candidate.normalized_payload
    if isinstance(payload, dict):
        for key in ("content", "claim_text", "text", "reason", "summary", "proposed_value"):
            text = _clean_text(payload.get(key))
            if text:
                return text
    text = _clean_text(payload)
    if text and text not in {"{}", "[]"}:
        return text
    return _clean_text(candidate.mapping_reason) or f"{candidate.candidate_type} candidate"
