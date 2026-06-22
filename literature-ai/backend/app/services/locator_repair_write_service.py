from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import EvidenceLocator, ExtractionFieldReview, Paper
from app.services.evidence_locator_service import EvidenceLocatorService
from app.utils.review_safety import is_safe_verified_review


PILOT_PAPER_ID = "3978dc79f94f4457863fd68449ae293d"
APPROVED_FIELDS = frozenset({"catalyst_type", "metal_centers", "rate"})
REJECTED_FIELDS = frozenset({"name", "convergence_settings"})
MIN_CONFIDENCE = 0.6


class LocatorRepairWriteError(ValueError):
    pass


@dataclass(frozen=True)
class LocatorRepairWriteResult:
    dry_run: bool
    written_count: int
    target_review_ids: tuple[str, ...]
    rollback_snapshot: dict[str, Any]
    post_write: dict[str, Any] | None
    touched_locators: tuple[dict[str, Any], ...]


def load_json_file(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LocatorRepairWriteError(f"Expected JSON object: {path}")
    return payload


def approved_items_from_plan(
    *,
    disabled_plan: dict[str, Any],
    proposal_manifest: dict[str, Any],
    approved_review_ids: set[str],
) -> list[dict[str, Any]]:
    proposals = {str(item.get("review_id")): item for item in proposal_manifest.get("proposals", [])}
    items: list[dict[str, Any]] = []
    for item in disabled_plan.get("items", []):
        review_id = str(item.get("review_id") or "")
        if review_id not in approved_review_ids:
            continue
        merged = {**item}
        if review_id in proposals:
            proposal = proposals[review_id]
            merged["matched_text"] = proposal.get("matched_text")
            merged["source_artifact"] = proposal.get("source_artifact") or item.get("source_artifact")
            merged["proposed_page"] = proposal.get("proposed_page", item.get("proposed_page"))
            merged["proposed_bbox"] = proposal.get("proposed_bbox", item.get("proposed_bbox"))
            merged["match_method"] = proposal.get("match_method", item.get("match_method"))
            merged["confidence"] = proposal.get("confidence", item.get("confidence"))
        items.append(merged)
    return items


class ControlledLocatorRepairWriteService:
    """Narrow D4-3H.1 locator-only write service.

    The service never marks reviews verified and never computes export/writing
    eligibility as unlocked. Callers must pass both explicit approval flags to
    write; dry_run is the default.
    """

    def __init__(
        self,
        session: Session,
        *,
        pilot_paper_id: str = PILOT_PAPER_ID,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> None:
        self.session = session
        self.pilot_paper_id = pilot_paper_id
        self.min_confidence = min_confidence

    def apply(
        self,
        items: list[dict[str, Any]],
        *,
        approved_review_ids: set[str],
        allow_active_db_write: bool = False,
        confirmed_approval: bool = False,
        dry_run: bool = True,
    ) -> LocatorRepairWriteResult:
        normalized_ids = {str(item) for item in approved_review_ids}
        self._validate_items(items, approved_review_ids=normalized_ids)
        snapshot = self.build_rollback_snapshot(normalized_ids)

        if dry_run:
            return LocatorRepairWriteResult(
                dry_run=True,
                written_count=0,
                target_review_ids=tuple(sorted(normalized_ids)),
                rollback_snapshot=snapshot,
                post_write=None,
                touched_locators=(),
            )

        if not allow_active_db_write or not confirmed_approval:
            raise LocatorRepairWriteError("Active locator write requires allow_active_db_write and confirmed_approval")

        locator_service = EvidenceLocatorService(self.session)
        touched: list[dict[str, Any]] = []
        for item in items:
            review = self._review_for_item(item)
            locator = locator_service.upsert_locator(
                paper_id=review.paper_id,
                claim_id=None,
                chunk_id=review.target_id,
                source_type="text",
                page=int(item["proposed_page"]),
                bbox=item.get("proposed_bbox"),
                section=None,
                target_type=review.target_type,
                target_id=review.target_id,
                field_name=review.field_name,
                evidence_text=str(item.get("matched_text") or ""),
                parser_source="docling",
                locator_confidence=float(item["confidence"]),
                warning_reason=_warning_reason(item),
            )
            touched.append(_locator_to_dict(locator))

        self.session.flush()
        post_write = self.build_status_snapshot(normalized_ids)
        self._validate_post_write(post_write, expected_count=len(items))
        return LocatorRepairWriteResult(
            dry_run=False,
            written_count=len(touched),
            target_review_ids=tuple(sorted(normalized_ids)),
            rollback_snapshot=snapshot,
            post_write=post_write,
            touched_locators=tuple(touched),
        )

    def build_rollback_snapshot(self, target_review_ids: set[str]) -> dict[str, Any]:
        return {
            "snapshot_type": "D4-3H.1_locator_repair_rollback_snapshot",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "paper_id": self.pilot_paper_id,
            "target_review_ids": sorted(target_review_ids),
            "pre_write": self.build_status_snapshot(target_review_ids),
        }

    def build_status_snapshot(self, target_review_ids: set[str]) -> dict[str, Any]:
        paper_uuid = _uuid(self.pilot_paper_id)
        paper = self.session.get(Paper, paper_uuid)
        reviews = list(
            self.session.scalars(
                select(ExtractionFieldReview)
                .where(ExtractionFieldReview.paper_id == paper_uuid)
                .order_by(ExtractionFieldReview.field_name.asc(), ExtractionFieldReview.id.asc())
            ).all()
        )
        locators = list(
            self.session.scalars(
                select(EvidenceLocator)
                .where(EvidenceLocator.paper_id == paper_uuid)
                .order_by(EvidenceLocator.field_name.asc(), EvidenceLocator.id.asc())
            ).all()
        )
        target_locators = [
            locator
            for locator in locators
            if str(locator.field_name or "") in APPROVED_FIELDS
            and any(_review_id_for_field(reviews, locator.field_name) == review_id for review_id in target_review_ids)
        ]
        non_pilot_reviews = self.session.execute(
            select(
                ExtractionFieldReview.id,
                ExtractionFieldReview.paper_id,
                ExtractionFieldReview.target_type,
                ExtractionFieldReview.target_id,
                ExtractionFieldReview.field_name,
                ExtractionFieldReview.reviewer_status,
                ExtractionFieldReview.target_resolution_status,
            )
            .where(ExtractionFieldReview.paper_id != paper_uuid)
            .order_by(ExtractionFieldReview.paper_id.asc(), ExtractionFieldReview.id.asc())
        ).all()
        verified = [review for review in reviews if str(review.reviewer_status).lower() == "verified"]
        safe_verified = [review for review in reviews if is_safe_verified_review(review)]
        return {
            "paper_exists": paper is not None,
            "paper_title": paper.title if paper is not None else None,
            "review_rows_total": len(reviews),
            "pending_rows": sum(1 for review in reviews if str(review.reviewer_status).lower() == "pending"),
            "verified_rows": len(verified),
            "safe_verified_rows": len(safe_verified),
            "export_eligible_count": 0 if not safe_verified else "not_evaluated",
            "writing_eligible_count": 0 if not safe_verified else "not_evaluated",
            "pilot_locator_count": len(locators),
            "target_locator_count": len(target_locators),
            "review_rows": [_review_to_dict(review) for review in reviews],
            "target_locators": [_locator_to_dict(locator) for locator in target_locators],
            "non_pilot_review_count": len(non_pilot_reviews),
            "non_pilot_review_checksum": _checksum([tuple(row) for row in non_pilot_reviews]),
            "non_pilot_locator_count": int(
                self.session.scalar(
                    select(func.count()).select_from(EvidenceLocator).where(EvidenceLocator.paper_id != paper_uuid)
                )
                or 0
            ),
        }

    def _validate_items(self, items: list[dict[str, Any]], *, approved_review_ids: set[str]) -> None:
        if not approved_review_ids:
            raise LocatorRepairWriteError("Missing approved review_ids")
        if len(items) != len(approved_review_ids):
            raise LocatorRepairWriteError("Approved item set does not match approved review_ids")
        seen: set[str] = set()
        for item in items:
            review_id = str(item.get("review_id") or "")
            field = str(item.get("field") or "")
            seen.add(review_id)
            if review_id not in approved_review_ids:
                raise LocatorRepairWriteError(f"Unapproved review_id: {review_id}")
            if field in REJECTED_FIELDS:
                raise LocatorRepairWriteError(f"Rejected field cannot be written: {field}")
            if field not in APPROVED_FIELDS:
                raise LocatorRepairWriteError(f"Unknown or unapproved field: {field}")
            if str(item.get("paper_id")) != self.pilot_paper_id:
                raise LocatorRepairWriteError(f"Non-pilot paper is not allowed: {item.get('paper_id')}")
            if item.get("proposed_page") is None:
                raise LocatorRepairWriteError(f"Missing proposed page for {review_id}")
            if item.get("proposed_bbox") is None:
                raise LocatorRepairWriteError(f"Missing proposed bbox for {review_id}")
            if not str(item.get("matched_text") or "").strip():
                raise LocatorRepairWriteError(f"Missing matched_text for {review_id}")
            confidence = float(item.get("confidence") or 0.0)
            if confidence < self.min_confidence:
                raise LocatorRepairWriteError(f"Confidence below threshold for {review_id}: {confidence}")
            if item.get("safe_verified") is True or item.get("export_eligible") is True or item.get("writing_eligible") is True:
                raise LocatorRepairWriteError(f"Verified/export/writing-like payload is not allowed for {review_id}")
            if item.get("mark_verified") is True or item.get("verified") is True:
                raise LocatorRepairWriteError(f"Verified-like payload is not allowed for {review_id}")
            self._review_for_item(item)
        if seen != approved_review_ids:
            raise LocatorRepairWriteError("Approved review_ids were not fully represented by plan items")

    def _review_for_item(self, item: dict[str, Any]) -> ExtractionFieldReview:
        review_id = str(item.get("review_id") or "")
        review_uuid = _uuid(review_id)
        review = self.session.get(ExtractionFieldReview, review_uuid)
        if review is None:
            raise LocatorRepairWriteError(f"Review row not found: {review_id}")
        if _uuid_hex(review.paper_id) != _uuid_hex(self.pilot_paper_id):
            raise LocatorRepairWriteError(f"Review row is not on pilot paper: {review_id}")
        if review.field_name != item.get("field"):
            raise LocatorRepairWriteError(f"Review field mismatch for {review_id}")
        if str(review.reviewer_status).lower() != "pending":
            raise LocatorRepairWriteError(f"Review row is not pending: {review_id}")
        if str(review.target_resolution_status).lower() != "active":
            raise LocatorRepairWriteError(f"Review target is not active: {review_id}")
        return review

    @staticmethod
    def _validate_post_write(snapshot: dict[str, Any], *, expected_count: int) -> None:
        if snapshot["review_rows_total"] != 5:
            raise LocatorRepairWriteError("Unexpected pilot review row count after write")
        if snapshot["pending_rows"] != 5:
            raise LocatorRepairWriteError("Write changed pending review state")
        if snapshot["verified_rows"] != 0 or snapshot["safe_verified_rows"] != 0:
            raise LocatorRepairWriteError("Write created verified or safe verified rows")
        if snapshot["export_eligible_count"] != 0 or snapshot["writing_eligible_count"] != 0:
            raise LocatorRepairWriteError("Write unlocked export or writing")
        if snapshot["target_locator_count"] != expected_count:
            raise LocatorRepairWriteError("Expected target locator count was not written")


def apply_locator_repair_plan_to_sqlite(
    *,
    db_path: str | Path,
    disabled_plan_path: str | Path,
    proposal_manifest_path: str | Path,
    approved_review_ids: set[str],
    rollback_snapshot_path: str | Path,
    allow_active_db_write: bool = False,
    confirmed_approval: bool = False,
    dry_run: bool = True,
) -> LocatorRepairWriteResult:
    raise LocatorRepairWriteError("SQLite locator repair writes have been removed. Use PostgreSQL session services.")


def _warning_reason(item: dict[str, Any]) -> str:
    return (
        "controlled_locator_repair:D4-3H.1;"
        f"review_id={item.get('review_id')};"
        f"source_artifact={item.get('source_artifact')};"
        f"match_method={item.get('match_method')};"
        "locator_only_no_review_change"
    )


def _uuid(value: Any) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError as exc:
        raise LocatorRepairWriteError(f"Invalid UUID: {value}") from exc


def _uuid_hex(value: Any) -> str:
    return _uuid(value).hex


def _write_snapshot(path: str | Path, snapshot: dict[str, Any], *, db_path: str | Path) -> None:
    payload = {
        "active_db_path": str(Path(db_path).resolve()),
        **snapshot,
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _review_to_dict(review: ExtractionFieldReview) -> dict[str, Any]:
    return {
        "id": str(review.id),
        "paper_id": str(review.paper_id),
        "target_type": review.target_type,
        "target_id": review.target_id,
        "field_name": review.field_name,
        "reviewer_status": review.reviewer_status,
        "target_resolution_status": review.target_resolution_status,
        "evidence_text": review.evidence_text,
    }


def _locator_to_dict(locator: EvidenceLocator) -> dict[str, Any]:
    return {
        "id": str(locator.id) if locator.id is not None else None,
        "paper_id": str(locator.paper_id),
        "target_type": locator.target_type,
        "target_id": locator.target_id,
        "field_name": locator.field_name,
        "page": locator.page,
        "bbox": locator.bbox,
        "source_type": locator.source_type,
        "locator_status": locator.locator_status,
        "locator_confidence": locator.locator_confidence,
        "parser_source": locator.parser_source,
        "warning_reason": locator.warning_reason,
        "evidence_text": locator.evidence_text,
    }


def _review_id_for_field(reviews: list[ExtractionFieldReview], field_name: str | None) -> str | None:
    for review in reviews:
        if review.field_name == field_name:
            return str(review.id)
    return None


def _checksum(rows: list[Any]) -> str:
    payload = json.dumps(rows, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
