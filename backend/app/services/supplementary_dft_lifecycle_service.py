from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult, PaperRelationship, utcnow


SUPPLEMENTARY_RELATIONSHIP_TYPES = {
    "supplementary",
    "supplementary_information",
    "supporting_information",
    "si",
}
SUPPORT_DFT_LIFECYCLE_STATUSES = {
    "pending",
    "ignored",
    "replaced",
    "written_back",
    "needs_human",
}
OPEN_SUPPORT_DFT_LIFECYCLE_STATUSES = {"pending", "needs_human"}
CLOSED_SUPPORT_DFT_LIFECYCLE_STATUSES = {"ignored", "replaced", "written_back"}


class SupplementaryDFTLifecycleService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def initialize_pending(
        self,
        *,
        main_id_by_support_id: dict[UUID, UUID],
    ) -> int:
        if not main_id_by_support_id:
            return 0
        rows = self.session.scalars(
            select(DFTResult).where(
                DFTResult.paper_id.in_(set(main_id_by_support_id)),
                DFTResult.support_lifecycle_status.is_(None),
            )
        ).all()
        now = utcnow()
        for row in rows:
            row.support_lifecycle_status = "pending"
            row.support_writeback_paper_id = main_id_by_support_id[row.paper_id]
            row.support_lifecycle_reason = "linked_supplementary_candidate"
            row.support_lifecycle_actor = "system:supplementary_link"
            row.support_lifecycle_updated_at = now
            self.session.add(row)
        if rows:
            self.session.flush()
        return len(rows)

    def resolve(
        self,
        *,
        main_paper_id: UUID,
        support_candidate_id: UUID,
        status: str,
        actor: str,
        reason: str | None = None,
        canonical_dft_result_id: UUID | None = None,
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in SUPPORT_DFT_LIFECYCLE_STATUSES - {"pending"}:
            raise ValueError("invalid_support_dft_lifecycle_status")
        candidate = self.session.get(DFTResult, support_candidate_id)
        if candidate is None:
            raise LookupError("Supplementary DFT candidate not found")
        if candidate.paper_id == main_paper_id:
            raise ValueError("support_candidate_must_belong_to_supplementary_paper")
        relationship = self.session.scalar(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == main_paper_id,
                PaperRelationship.target_paper_id == candidate.paper_id,
                PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
            )
        )
        if relationship is None:
            raise ValueError("supplementary_relationship_not_found")

        current_status = str(candidate.support_lifecycle_status or "pending").strip().lower()
        if current_status in CLOSED_SUPPORT_DFT_LIFECYCLE_STATUSES:
            if (
                current_status == normalized_status
                and candidate.support_writeback_dft_result_id == canonical_dft_result_id
            ):
                return self._serialize(candidate)
            raise ValueError(f"support_dft_candidate_already_closed:{current_status}")

        canonical = None
        if normalized_status in {"replaced", "written_back"}:
            if canonical_dft_result_id is None:
                raise ValueError("canonical_dft_result_id_required")
            canonical = self.session.get(DFTResult, canonical_dft_result_id)
            if canonical is None or canonical.paper_id != main_paper_id:
                raise ValueError("canonical_dft_result_must_belong_to_main_paper")
        elif canonical_dft_result_id is not None:
            raise ValueError("canonical_dft_result_id_not_allowed_for_status")

        normalized_reason = str(reason or "").strip()
        if normalized_status in {"ignored", "needs_human"} and not normalized_reason:
            raise ValueError("support_dft_lifecycle_reason_required")

        candidate.support_lifecycle_status = normalized_status
        candidate.support_writeback_paper_id = main_paper_id
        candidate.support_writeback_dft_result_id = canonical.id if canonical else None
        candidate.support_lifecycle_reason = normalized_reason or (
            "canonical_dft_result_created_from_supplementary_candidate"
            if normalized_status == "written_back"
            else "canonical_main_paper_result_already_exists"
        )
        candidate.support_lifecycle_actor = str(actor or "system").strip() or "system"
        candidate.support_lifecycle_updated_at = utcnow()
        self.session.add(candidate)
        self.session.add(
            AuditLog(
                paper_id=main_paper_id,
                action="resolve_supplementary_dft_candidate",
                source=candidate.support_lifecycle_actor,
                target_type="dft_results",
                target_id=str(candidate.id),
                payload={
                    "support_paper_id": str(candidate.paper_id),
                    "status": normalized_status,
                    "canonical_dft_result_id": str(canonical.id) if canonical else None,
                    "reason": candidate.support_lifecycle_reason,
                },
            )
        )
        self.session.flush()
        return self._serialize(candidate)

    @staticmethod
    def counts(rows: list[DFTResult]) -> dict[str, int]:
        return dict(
            Counter(
                str(row.support_lifecycle_status or "pending").strip().lower() or "pending"
                for row in rows
            )
        )

    @staticmethod
    def _serialize(candidate: DFTResult) -> dict[str, Any]:
        return {
            "support_candidate_id": str(candidate.id),
            "support_paper_id": str(candidate.paper_id),
            "status": candidate.support_lifecycle_status or "pending",
            "writeback_paper_id": (
                str(candidate.support_writeback_paper_id)
                if candidate.support_writeback_paper_id
                else None
            ),
            "canonical_dft_result_id": (
                str(candidate.support_writeback_dft_result_id)
                if candidate.support_writeback_dft_result_id
                else None
            ),
            "reason": candidate.support_lifecycle_reason,
            "actor": candidate.support_lifecycle_actor,
            "updated_at": (
                candidate.support_lifecycle_updated_at.isoformat()
                if candidate.support_lifecycle_updated_at
                else None
            ),
        }
