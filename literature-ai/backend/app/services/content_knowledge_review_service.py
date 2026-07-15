from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AuditLog, ContentEvidenceItem, Paper, utcnow
from app.schemas.content_knowledge import ContentReviewDecision
from app.services.content_knowledge_service import CONTENT_KNOWLEDGE_SCHEMA_VERSION, serialize_content_item
from app.utils.artifact_paths import resolve_paper_pdf_path


class ContentKnowledgeReviewError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ContentKnowledgeReviewService:
    """Human card review over the canonical ContentEvidenceItem projection."""

    def __init__(self, session: Session):
        self.session = session
        self.settings = get_settings()

    def get_item(self, item_id: UUID) -> dict:
        row = self.session.execute(
            select(ContentEvidenceItem, Paper)
            .join(Paper, Paper.id == ContentEvidenceItem.paper_id)
            .where(ContentEvidenceItem.id == item_id)
        ).one_or_none()
        if row is None:
            raise ContentKnowledgeReviewError("content_knowledge_item_not_found", status_code=404)
        item, paper = row
        return {"schema_version": CONTENT_KNOWLEDGE_SCHEMA_VERSION, "item": serialize_content_item(item, paper).payload()}

    def review_item(
        self,
        item_id: UUID,
        *,
        decision: ContentReviewDecision,
        reviewer: str,
        reason: str | None,
        expected_updated_at: datetime,
    ) -> dict:
        row = self.session.execute(
            select(ContentEvidenceItem, Paper)
            .join(Paper, Paper.id == ContentEvidenceItem.paper_id)
            .where(ContentEvidenceItem.id == item_id)
            .with_for_update(of=ContentEvidenceItem)
        ).one_or_none()
        if row is None:
            raise ContentKnowledgeReviewError("content_knowledge_item_not_found", status_code=404)
        item, paper = row
        self._assert_current(item, expected_updated_at)
        self._assert_decision_allowed(item, paper, decision)

        before = _review_state(item)
        item.review_status, item.citation_status = _decision_state(decision)
        item.reviewer = reviewer
        item.reviewed_at = utcnow()
        self.session.add(item)
        self.session.flush()
        self.session.refresh(item)
        after = _review_state(item)
        audit = AuditLog(
            paper_id=item.paper_id,
            action="review_content_evidence_item",
            source="human_content_review",
            target_type="content_evidence_item",
            target_id=str(item.id),
            payload={
                "decision": decision,
                "reviewer": reviewer,
                "reason": reason,
                "before": before,
                "after": after,
            },
        )
        self.session.add(audit)
        self.session.flush()
        return {
            "schema_version": CONTENT_KNOWLEDGE_SCHEMA_VERSION,
            "reviewed": True,
            "item": serialize_content_item(item, paper).payload(),
            "audit_log_id": str(audit.id),
        }

    def paper_summary(self, paper_id: str) -> dict:
        paper = self._paper(paper_id)
        rows = self.session.execute(
            select(
                ContentEvidenceItem.category,
                ContentEvidenceItem.review_status,
                ContentEvidenceItem.citation_status,
                func.count(),
            )
            .where(ContentEvidenceItem.paper_id == paper.id)
            .group_by(
                ContentEvidenceItem.category,
                ContentEvidenceItem.review_status,
                ContentEvidenceItem.citation_status,
            )
        ).all()
        by_category: Counter[str] = Counter()
        by_review_status: Counter[str] = Counter()
        by_citation_status: Counter[str] = Counter()
        category_status: dict[str, Counter[str]] = defaultdict(Counter)
        for category, review_status, citation_status, count in rows:
            count_value = int(count)
            by_category[category] += count_value
            by_review_status[review_status] += count_value
            by_citation_status[citation_status] += count_value
            category_status[category][review_status] += count_value
        total = sum(by_category.values())
        pending = by_review_status["needs_review"] + by_review_status["needs_human"]
        return {
            "schema_version": CONTENT_KNOWLEDGE_SCHEMA_VERSION,
            "paper": {
                "paper_id": str(paper.id),
                "paper_code": paper.paper_code,
                "title": paper.title,
                "doi": paper.doi,
            },
            "total": total,
            "pending_total": pending,
            "completed_total": total - pending,
            "by_category": dict(sorted(by_category.items())),
            "by_review_status": dict(sorted(by_review_status.items())),
            "by_citation_status": dict(sorted(by_citation_status.items())),
            "category_status": {
                category: dict(sorted(statuses.items()))
                for category, statuses in sorted(category_status.items())
            },
        }

    def _paper(self, paper_id: str) -> Paper:
        try:
            paper_uuid = UUID(str(paper_id))
        except (TypeError, ValueError):
            paper_uuid = None
        stmt = select(Paper).where(Paper.id == paper_uuid) if paper_uuid else select(Paper).where(Paper.paper_code == paper_id.strip())
        paper = self.session.scalar(stmt)
        if paper is None:
            raise ContentKnowledgeReviewError("paper_not_found", status_code=404)
        return paper

    @staticmethod
    def _assert_current(item: ContentEvidenceItem, expected: datetime) -> None:
        if _naive_utc(item.updated_at) != _naive_utc(expected):
            raise ContentKnowledgeReviewError("content_knowledge_item_updated")

    def _assert_decision_allowed(
        self,
        item: ContentEvidenceItem,
        paper: Paper,
        decision: ContentReviewDecision,
    ) -> None:
        if decision != "approve_citable":
            return
        if item.category == "figure_table_evidence":
            raise ContentKnowledgeReviewError("figure_table_evidence_requires_chart_review")
        if resolve_paper_pdf_path(paper.pdf_path, self.settings.storage_root) is None:
            raise ContentKnowledgeReviewError("citable_requires_real_pdf")
        if not str(item.evidence_text or "").strip():
            raise ContentKnowledgeReviewError("citable_requires_evidence_text")
        if not _has_locator(item):
            raise ContentKnowledgeReviewError("citable_requires_locator")


def _decision_state(decision: ContentReviewDecision) -> tuple[str, str]:
    return {
        "approve_citable": ("validated", "citable"),
        "writing_only": ("validated", "writing_only"),
        "needs_human": ("needs_human", "needs_review"),
        "reject": ("rejected", "blocked"),
    }[decision]


def _review_state(item: ContentEvidenceItem) -> dict[str, str | None]:
    return {
        "review_status": item.review_status,
        "citation_status": item.citation_status,
        "reviewer": item.reviewer,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _has_locator(item: ContentEvidenceItem) -> bool:
    locator = item.evidence_locator if isinstance(item.evidence_locator, dict) else {}
    return bool(
        item.page_start
        or str(item.section_title or "").strip()
        or locator.get("page")
        or locator.get("page_start")
        or locator.get("section")
        or locator.get("section_title")
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
