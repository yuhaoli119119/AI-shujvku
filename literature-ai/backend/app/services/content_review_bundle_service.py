from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import ContentReviewBundle


CONTENT_REVIEW_BUNDLE_V1_DEPRECATED_CODE = "content_review_bundle_v1_deprecated"


class ContentReviewBundleService:
    """Read historical v1 bundle rows without restoring their retired write path."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_readonly(self, bundle_id: UUID) -> dict:
        bundle = self.session.get(ContentReviewBundle, bundle_id)
        if bundle is None:
            raise ValueError("content_review_bundle_not_found")
        return {
            "bundle_id": str(bundle.id),
            "schema_version": "content_evidence_review_bundle_v1",
            "deprecated": True,
            "deprecated_code": CONTENT_REVIEW_BUNDLE_V1_DEPRECATED_CODE,
            "paper_id": str(bundle.paper_id),
            "run_id": str(bundle.run_id) if bundle.run_id else None,
            "status": bundle.status,
            "snapshot_fingerprint": bundle.snapshot_fingerprint,
            "manifest": bundle.manifest,
            "result_payload": bundle.result_payload,
            "created_by": bundle.created_by,
            "created_at": bundle.created_at.isoformat() if bundle.created_at else None,
            "updated_at": bundle.updated_at.isoformat() if bundle.updated_at else None,
        }
