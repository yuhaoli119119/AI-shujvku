from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    EvidenceClaim,
    ContentEvidenceItem,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    MechanismClaim,
    Paper,
    PaperNote,
    WritingCard,
)
from app.utils.library_names import build_library_name_clause, normalize_library_name
from app.utils.review_safety import writing_card_content_gate, writing_card_gate
from app.services.embedding import get_embedding_service
from app.config import get_settings


CONTENT_KNOWLEDGE_SCHEMA_VERSION = "content_knowledge.v1"

CATEGORY_LABELS: dict[str, str] = {
    "mechanism_evidence": "机理证据卡",
    "performance_evidence": "性能证据卡",
    "dft_evidence": "DFT证据卡",
    "figure_table_evidence": "图表证据卡",
    "material_evidence": "材料信息卡",
    "method_evidence": "方法信息卡",
    "writing_material": "写作素材卡",
    "review_viewpoint": "综述观点卡",
    "uncertainty_note": "争议/风险卡",
    "draft_evidence_check": "草稿证据核验",
}

PROBLEM_CANDIDATE_STATUSES = {"requires_resolution", "unmapped", "failed", "skipped"}
BLOCKED_CANDIDATE_STATUSES = {"failed", "skipped"}
CITABLE_EVIDENCE_STATUSES = {"approved", "validated", "safe_verified"}


@dataclass(slots=True)
class ContentKnowledgeItem:
    item_id: str
    paper_id: str
    paper_code: str | None
    paper_title: str | None
    category: str
    category_label: str
    source_type: str
    source_id: str
    source_table: str
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
        include_candidates: bool = True,
        include_blocked: bool = False,
        review_status: str | None = None,
        citation_status: str | None = None,
        source_trust: str | None = None,
        problem_status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        normalized_library = normalize_library_name(library_name) if library_name is not None else None
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
        items = self._persistent_items(
            paper_ids=paper_ids,
            paper_by_id=paper_by_id,
            run_id=run_uuid,
            category=category,
            query=query,
            include_candidates=include_candidates,
            include_blocked=include_blocked,
            review_status=review_status,
            citation_status=citation_status,
            source_trust=source_trust,
            problem_status=problem_status,
            limit=limit_value,
        )
        if not items and paper_ids and not run_id:
            items = self._legacy_items(paper_ids, paper_by_id, include_candidates=include_candidates)
        filtered = [item for item in items if self._include_item(
            item, category=category, query=query, include_candidates=include_candidates, include_blocked=include_blocked,
        )][:limit_value]

        counts = Counter(item.category for item in filtered)
        return {
            "schema_version": CONTENT_KNOWLEDGE_SCHEMA_VERSION,
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
                "include_candidates": include_candidates,
                "include_blocked": include_blocked,
                "review_status": review_status,
                "citation_status": citation_status,
                "source_trust": source_trust,
                "problem_status": problem_status,
                "limit": limit_value,
            },
            "category_counts": dict(sorted(counts.items())),
            "items": [item.payload() for item in filtered],
        }

    def sync_items(
        self,
        *,
        paper_id: str | uuid.UUID | None = None,
        library_name: str | None = None,
        include_candidates: bool = True,
    ) -> dict[str, Any]:
        """Materialize legacy source rows into the ContentEvidenceItem contract.

        The method never upgrades review/citation state.  Existing human review
        state is preserved while source text/locators are refreshed.
        """
        papers = self._scoped_papers(
            paper_id=paper_id,
            library_name=normalize_library_name(library_name) if library_name is not None else None,
        )
        paper_by_id = {paper.id: paper for paper in papers}
        legacy = self._legacy_items(list(paper_by_id), paper_by_id, include_candidates=include_candidates)
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
                embedding_changed = any(
                    getattr(existing, field_name) != value
                    for field_name, value in (
                        ("content", item.content),
                        ("evidence_text", item.evidence_text),
                        ("evidence_locator", item.evidence_locator),
                        ("page_start", item.page_start),
                        ("page_end", item.page_end),
                        ("section_title", item.section_title),
                    )
                )
                existing.content = item.content
                existing.evidence_text = item.evidence_text
                existing.evidence_locator = item.evidence_locator
                existing.page_start = item.page_start
                existing.page_end = item.page_end
                existing.section_title = item.section_title
                existing.source_record = source_metadata
                if source_run_id is not None:
                    existing.run_id = source_run_id
                existing.risk_flags = list(dict.fromkeys([*(existing.risk_flags or []), *(item.risk_flags or [])]))
                if embedding_changed:
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
        stmt = select(ContentEvidenceItem)
        if paper_ids:
            stmt = stmt.where(ContentEvidenceItem.paper_id.in_(paper_ids))
        if not include_review_assist:
            stmt = stmt.where(ContentEvidenceItem.citation_status.in_(("citable", "writing_only")))
        terms = _search_terms(query)
        if terms:
            # OR is recall; BM25-like character n-gram scoring below supplies precision.
            stmt = stmt.where(or_(*[
                or_(ContentEvidenceItem.content.ilike(f"%{term}%"), ContentEvidenceItem.evidence_text.ilike(f"%{term}%"))
                for term in terms
            ]))
        candidate_limit = max(limit * 6, 30)
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
        for row in rows:
            lexical = _bm25ish_score(query, f"{row.content} {row.evidence_text or ''}")
            vector = _cosine(query_vector, row.embedding)
            hybrid = round(0.68 * lexical + 0.32 * vector, 4)
            if hybrid > 0 or not terms:
                scored.append((self._persistent_item(row, paper_by_id.get(row.paper_id)), {"bm25": lexical, "vector": vector, "hybrid": hybrid}))
        return sorted(scored, key=lambda pair: pair[1]["hybrid"], reverse=True)[:limit]

    def count_unreviewed_matching(
        self,
        *,
        query: str,
        paper_ids: list[uuid.UUID] | None = None,
    ) -> int:
        """Return the DB-scoped count excluded by the formal-citation review gate."""
        stmt = select(func.count()).select_from(ContentEvidenceItem).where(
            ContentEvidenceItem.review_status.in_(("needs_review", "needs_human"))
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
            items.extend(self._writing_card_items(paper_ids, paper_by_id))
            items.extend(self._paper_note_items(paper_ids, paper_by_id))
            items.extend(self._evidence_claim_items(paper_ids, paper_by_id))
            if include_candidates:
                items.extend(self._external_candidate_items(paper_ids, paper_by_id))
        return items

    def _persistent_items(self, *, paper_ids, paper_by_id, run_id, category, query, include_candidates,
                          include_blocked, review_status, citation_status, source_trust, problem_status, limit):
        if not paper_ids:
            return []
        stmt = select(ContentEvidenceItem).where(ContentEvidenceItem.paper_id.in_(paper_ids))
        if run_id:
            stmt = stmt.where(ContentEvidenceItem.run_id == run_id)
        if category:
            stmt = stmt.where(ContentEvidenceItem.category == category)
        if review_status:
            stmt = stmt.where(ContentEvidenceItem.review_status == review_status)
        if citation_status:
            stmt = stmt.where(ContentEvidenceItem.citation_status == citation_status)
        if not include_candidates:
            stmt = stmt.where(ContentEvidenceItem.source_type != "external_analysis_candidate")
        if not include_blocked:
            stmt = stmt.where(ContentEvidenceItem.citation_status != "blocked")
        if source_trust == "verified":
            stmt = stmt.where(ContentEvidenceItem.source_identity_verified.is_(True))
        elif source_trust == "unverified":
            stmt = stmt.where(ContentEvidenceItem.source_identity_verified.is_(False))
        if problem_status == "has_risk":
            stmt = stmt.where(ContentEvidenceItem.risk_flags != [])
        # PostgreSQL filters scope before any in-process rerank.  Query tokens are
        # intentionally substring-based here so Chinese and formula/mixed tokens
        # remain searchable even without whitespace segmentation.
        for token in _search_terms(query):
            pattern = f"%{token}%"
            stmt = stmt.where(or_(ContentEvidenceItem.content.ilike(pattern), ContentEvidenceItem.evidence_text.ilike(pattern)))
        rows = self.session.scalars(stmt.order_by(ContentEvidenceItem.updated_at.desc()).limit(limit)).all()
        return [self._persistent_item(row, paper_by_id.get(row.paper_id)) for row in rows]

    def _persistent_item(self, row: ContentEvidenceItem, paper: Paper | None) -> ContentKnowledgeItem:
        citation = _normalized(row.citation_status) or "needs_review"
        reviewed = _normalized(row.review_status) in {"validated", "approved", "safe_verified"}
        can_cite = citation == "citable" and reviewed and bool(row.evidence_text) and bool(row.page_start or row.section_title)
        metadata = dict(row.source_record or {})
        if row.run_id is not None:
            metadata.setdefault("external_analysis_run_id", str(row.run_id))
        return ContentKnowledgeItem(
            item_id=str(row.id), paper_id=str(row.paper_id), paper_code=getattr(paper, "paper_code", None),
            paper_title=getattr(paper, "title", None), category=row.category,
            category_label=CATEGORY_LABELS.get(row.category, row.category), source_type=row.source_type,
            source_id=row.source_id, source_table="content_evidence_items", content=_clean_text(row.content),
            evidence_text=_clean_text(row.evidence_text) or None, evidence_locator=row.evidence_locator,
            page_start=row.page_start, page_end=row.page_end, section_title=row.section_title,
            review_status=row.review_status, review_gate_status=row.review_status,
            citation_policy=citation, can_use_for_writing=citation in {"citable", "writing_only"},
            can_use_for_citation=can_cite, risk_flags=list(row.risk_flags or []),
            recommended_action=None if reviewed else "review_content_evidence", source_ai=metadata.get("source_ai"),
            source_label=metadata.get("source_label"), source_identity=row.source_identity,
            source_identity_verified=bool(row.source_identity_verified), reviewer=row.reviewer,
            reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
            snapshot_fingerprint=row.snapshot_fingerprint, metadata=metadata,
        )

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
        items: list[ContentKnowledgeItem] = []
        for row in rows:
            paper = paper_by_id.get(row.paper_id)
            content = _clean_text(row.claim_text)
            risks = []
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
                    review_status="needs_review",
                    review_gate_status="needs_review",
                    citation_policy="needs_review",
                    can_use_for_writing=bool(content),
                    can_use_for_citation=False,
                    risk_flags=risks,
                    recommended_action="review_mechanism_claim_evidence",
                    metadata={
                        "claim_type": row.claim_type,
                        "confidence": row.confidence,
                        "evidence_types": row.evidence_types or [],
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
        items: list[ContentKnowledgeItem] = []
        for row in rows:
            paper = paper_by_id.get(row.paper_id)
            content = _writing_card_content(row)
            content_gate = writing_card_content_gate(row)
            gate = writing_card_gate(row)
            can_write = bool(gate.can_use_for_writing and content)
            risks = list(gate.blocked_reasons)
            citation_policy = "writing_only" if can_write else "needs_review"
            review_status = "content_ready" if can_write else "needs_review"
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
                    citation_policy=citation_policy,
                    can_use_for_writing=can_write,
                    can_use_for_citation=False,
                    risk_flags=risks,
                    recommended_action=None if can_write else "complete_writing_card_evidence_chain",
                    metadata={
                        "paper_type": row.paper_type,
                        "evidence_chain_status": content_gate.evidence_chain_status,
                        "figure_logic": row.figure_logic,
                        "section_strategy": row.section_strategy,
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
                    recommended_action="resolve_external_ai_candidate" if risks else "review_external_ai_candidate",
                    source_ai=run.source,
                    source_label=run.source_label,
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
                        "materialized_target_type": candidate.materialized_target_type,
                        "materialized_target_id": candidate.materialized_target_id,
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
        metadata: dict[str, Any] | None = None,
    ) -> ContentKnowledgeItem:
        item_category = category if category in CATEGORY_LABELS else "draft_evidence_check"
        paper_id = str(paper.id) if paper is not None else ""
        item_id = f"{source_type}:{source_id}"
        return ContentKnowledgeItem(
            item_id=item_id,
            paper_id=paper_id,
            paper_code=getattr(paper, "paper_code", None),
            paper_title=getattr(paper, "title", None),
            category=item_category,
            category_label=CATEGORY_LABELS[item_category],
            source_type=source_type,
            source_id=str(source_id),
            source_table=source_table,
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
            metadata=metadata or {},
        )

    def _include_item(
        self,
        item: ContentKnowledgeItem,
        *,
        category: str | None,
        query: str | None,
        include_candidates: bool,
        include_blocked: bool,
    ) -> bool:
        if category and item.category != category:
            return False
        if not include_candidates and item.source_type == "external_analysis_candidate":
            return False
        if not include_blocked and item.citation_policy == "blocked":
            return False
        tokens = _query_tokens(query)
        if tokens and not all(token in _search_blob(item) for token in tokens):
            return False
        return True


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
                item.paper_title or "",
                item.paper_code or "",
                item.category,
                item.category_label,
                item.source_type,
                _json_preview(item.metadata),
            ]
        )
    )


def _writing_card_content(card: WritingCard) -> str:
    parts = []
    for label, field_name in (
        ("research_gap", "research_gap"),
        ("proposed_solution", "proposed_solution"),
        ("core_hypothesis", "core_hypothesis"),
        ("abstract_logic", "abstract_logic"),
        ("introduction_logic", "introduction_logic"),
        ("discussion_logic", "discussion_logic"),
        ("figure_logic", "figure_logic"),
    ):
        value = _clean_text(getattr(card, field_name, None))
        if value:
            parts.append(f"{label}: {value}")
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
