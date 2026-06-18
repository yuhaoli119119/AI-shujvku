from __future__ import annotations

from datetime import UTC
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    ExtractionFieldReview,
    MechanismClaim,
)
from app.schemas.extraction import (
    ExtractionReviewAuditResponse,
    ExtractionFieldReviewResponse,
    ExtractionFieldReviewSaveItem,
    ExtractionReviewMarkVerifiedRequest,
    ExtractionReviewPrepareResponse,
)
from app.services.review_target_resolver import (
    ACTIVE_REVIEW_STATUSES,
    TARGET_TYPE_MODELS,
    canonical_target_type,
    ReviewTargetResolver,
)
from app.utils.review_safety import (
    can_manual_review_mark_verified,
    has_required_evidence_reference,
    is_safe_verified_review,
)

RESULT_KEY_BY_TARGET_TYPE = {
    "catalyst_samples": "CatalystSample",
    "dft_settings": "DFTSetting",
    "dft_results": "DFTResult",
    "mechanism_claims": "MechanismClaim",
    "electrochemical_performance": "ElectrochemicalPerformance",
}

FIELD_SNAPSHOT_BUILDERS = {
    "catalyst_samples": lambda row: {
        "name": {"value": row.name, "unit": None, "evidence_text": row.evidence_strength or row.name or ""},
        "catalyst_type": {"value": row.catalyst_type, "unit": None, "evidence_text": row.evidence_strength or row.catalyst_type or ""},
        "metal_centers": {"value": row.metal_centers or [], "unit": None, "evidence_text": row.evidence_strength or ""},
        "coordination": {"value": row.coordination, "unit": None, "evidence_text": row.evidence_strength or ""},
        "support": {"value": row.support, "unit": None, "evidence_text": row.evidence_strength or ""},
        "synthesis_method": {"value": row.synthesis_method, "unit": None, "evidence_text": row.synthesis_method or row.evidence_strength or ""},
    },
    "dft_settings": lambda row: {
        "software": {"value": row.software, "unit": None, "evidence_text": _raw_evidence(row)},
        "functional": {"value": row.functional, "unit": None, "evidence_text": _raw_evidence(row)},
        "dispersion_correction": {"value": row.dispersion_correction, "unit": None, "evidence_text": _raw_evidence(row)},
        "pseudopotential": {"value": row.pseudopotential, "unit": None, "evidence_text": _raw_evidence(row)},
        "cutoff_energy": {"value": row.cutoff_energy_ev, "unit": "eV", "evidence_text": _raw_evidence(row)},
        "k_points": {"value": row.k_points, "unit": None, "evidence_text": _raw_evidence(row)},
        "convergence_settings": {"value": row.convergence_settings or {}, "unit": None, "evidence_text": _raw_evidence(row)},
        "vacuum_thickness": {"value": row.vacuum_thickness_a, "unit": "A", "evidence_text": _raw_evidence(row)},
    },
    "dft_results": lambda row: {
        "catalyst": {"value": str(row.catalyst_sample_id) if row.catalyst_sample_id else None, "unit": None, "evidence_text": row.evidence_text or ""},
        "adsorbate": {"value": row.adsorbate, "unit": None, "evidence_text": row.evidence_text or ""},
        "energy_type": {"value": row.property_type, "unit": None, "evidence_text": row.evidence_text or ""},
        "value": {"value": row.value, "unit": row.unit, "evidence_text": row.evidence_text or ""},
        "reaction_step": {"value": row.reaction_step, "unit": None, "evidence_text": row.evidence_text or ""},
    },
    "mechanism_claims": lambda row: {
        "claim_type": {"value": row.claim_type, "unit": None, "evidence_text": row.evidence_text or ""},
        "claim_text": {"value": row.claim_text, "unit": None, "evidence_text": row.evidence_text or ""},
        "key_species": {"value": row.evidence_types or [], "unit": None, "evidence_text": row.evidence_text or ""},
        "mechanism_direction": {"value": None, "unit": None, "evidence_text": row.evidence_text or ""},
    },
    "electrochemical_performance": lambda row: {
        "sulfur_loading": {"value": row.sulfur_loading_mg_cm2, "unit": "mg/cm2", "evidence_text": row.evidence_text or ""},
        "sulfur_content": {"value": row.sulfur_content_wt_percent, "unit": "wt%", "evidence_text": row.evidence_text or ""},
        "electrolyte_sulfur_ratio": {"value": row.electrolyte_sulfur_ratio, "unit": None, "evidence_text": row.evidence_text or ""},
        "capacity": {"value": row.capacity_value, "unit": "mAh/g", "evidence_text": row.evidence_text or ""},
        "cycle_number": {"value": row.cycle_number, "unit": None, "evidence_text": row.evidence_text or ""},
        "rate": {"value": row.rate, "unit": None, "evidence_text": row.evidence_text or ""},
        "decay_per_cycle": {"value": row.decay_per_cycle, "unit": "%/cycle", "evidence_text": row.evidence_text or ""},
    },
}


def _raw_evidence(row: DFTSetting) -> str:
    raw = row.raw_json or {}
    return str(raw.get("supporting_text") or raw.get("extracted") or "")


class ExtractionReviewService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.resolver = ReviewTargetResolver(session)

    def list_reviews(self, paper_id: UUID) -> list[ExtractionFieldReviewResponse]:
        rows = self.session.scalars(
            select(ExtractionFieldReview)
            .where(ExtractionFieldReview.paper_id == paper_id)
            .order_by(
                ExtractionFieldReview.target_type.asc(),
                ExtractionFieldReview.target_id.asc(),
                ExtractionFieldReview.field_name.asc(),
            )
        ).all()
        return [self._serialize(row) for row in rows]

    def audit_reviews(self, paper_id: UUID) -> ExtractionReviewAuditResponse:
        items = self.list_reviews(paper_id)
        counts = {status: 0 for status in ("active", "remapped", "stale", "ambiguous", "unresolved")}
        for item in items:
            counts[item.target_resolution_status] = counts.get(item.target_resolution_status, 0) + 1
        return ExtractionReviewAuditResponse(
            paper_id=paper_id,
            total_reviews=len(items),
            active=counts["active"],
            remapped=counts["remapped"],
            stale=counts["stale"],
            ambiguous=counts["ambiguous"],
            unresolved=counts["unresolved"],
            items=items,
        )

    def prepare_pending_reviews(self, paper_id: UUID) -> ExtractionReviewPrepareResponse:
        prepared: list[ExtractionFieldReviewResponse] = []
        created_count = 0
        existing_count = 0
        skipped_count = 0
        for canonical_type, model in TARGET_TYPE_MODELS.items():
            rows = self.session.scalars(select(model).where(model.paper_id == paper_id)).all()
            for target in rows:
                snapshot = self.get_target_field_snapshot(canonical_type, target)
                for field_name, field_snapshot in snapshot.items():
                    if self._is_blank(field_snapshot["value"]):
                        continue
                    review = self._get_or_create_review(paper_id, canonical_type, str(target.id), field_name)
                    if not getattr(review, "_created_by_get_or_create", False):
                        existing_count += 1
                        if review.reviewer_status == "verified":
                            skipped_count += 1
                            prepared.append(self._serialize(review))
                            continue
                        self._fill_missing_prepare_fields(review, field_snapshot, str(target.id))
                        prepared.append(self._serialize(review))
                        continue
                    created_count += 1
                    review.original_value = field_snapshot["value"]
                    review.reviewed_value = None
                    review.unit = field_snapshot["unit"]
                    review.evidence_text = field_snapshot["evidence_text"]
                    review.reviewer_status = "pending"
                    review.reviewer = None
                    review.reviewer_note = "prepared_from_extraction"
                    review.target_resolution_status = "active"
                    review.remapped_from_target_id = None
                    review.last_resolved_target_id = str(target.id)
                    self.resolver._refresh_review_identity(review, canonical_type, target)
                    self.session.add(review)
                    self._flush_review_write()
                    prepared.append(self._serialize(review))
        self.session.commit()
        return ExtractionReviewPrepareResponse(
            paper_id=paper_id,
            created_count=created_count,
            existing_count=existing_count,
            skipped_count=skipped_count,
            verified_count=sum(1 for item in prepared if item.reviewer_status == "verified"),
            safe_verified_count=sum(1 for item in prepared if item.verified),
            review_ids=[item.id for item in prepared],
            items=prepared,
        )

    def save_reviews(self, paper_id: UUID, items: list[ExtractionFieldReviewSaveItem]) -> list[ExtractionFieldReviewResponse]:
        prepared: list[tuple[ExtractionFieldReviewSaveItem, str, Any, dict[str, Any], ExtractionFieldReview | None]] = []
        for item in items:
            canonical_type = self.canonical_target_type(item.target_type)
            target = self.get_target_or_raise(paper_id, canonical_type, item.target_id)
            snapshot = self.get_target_field_snapshot(canonical_type, target)
            if item.field_name not in snapshot:
                raise ValueError(f"Unsupported field for {canonical_type}: {item.field_name}")
            review = self._find_review(paper_id, canonical_type, item.target_id, item.field_name)
            if review is not None:
                self._guard_expected_write_version(review, item.expected_write_version, created=False)
            prepared.append((item, canonical_type, target, snapshot[item.field_name], review))

        writable: list[tuple[ExtractionFieldReviewSaveItem, str, Any, dict[str, Any], ExtractionFieldReview]] = []
        for item, canonical_type, target, field_snapshot, existing_review in prepared:
            review = existing_review or self._get_or_create_review(
                paper_id, canonical_type, item.target_id, item.field_name
            )
            self._guard_expected_write_version(
                review,
                item.expected_write_version,
                created=existing_review is None and getattr(review, "_created_by_get_or_create", False),
            )
            writable.append((item, canonical_type, target, field_snapshot, review))

        saved: list[ExtractionFieldReviewResponse] = []
        for item, canonical_type, target, field_snapshot, review in writable:
            self._guard_verified_review_mutation(review, item.reviewed_value, item.reviewer_status)
            # D1 Phase 3: save_reviews cannot directly set reviewer_status=verified
            # Verified must go through mark_verified API which has evidence checks
            incoming_status = item.reviewer_status
            if incoming_status == "verified":
                raise ValueError(
                    "Cannot set reviewer_status=verified through save. "
                    "Use the mark-verified endpoint for human verification."
                )
            review.original_value = item.original_value if item.original_value is not None else field_snapshot["value"]
            review.reviewed_value = item.reviewed_value if review.reviewer_status != "verified" else review.reviewed_value
            review.unit = item.unit if item.unit is not None else field_snapshot["unit"]
            review.evidence_text = item.evidence_text if item.evidence_text is not None else field_snapshot["evidence_text"]
            review.reviewer_status = incoming_status if review.reviewer_status != "verified" else review.reviewer_status
            review.reviewer = item.reviewer
            review.reviewer_note = item.reviewer_note
            review.review_payload = item.review_payload
            # D1 Phase 3: do not reset target_resolution_status on save
            # Only mark_verified is allowed to set it to "active"
            if getattr(review, "_created_by_get_or_create", False):
                review.target_resolution_status = "active"
                review.remapped_from_target_id = None
                review.last_resolved_target_id = item.target_id
            self.resolver._refresh_review_identity(review, canonical_type, target)
            self.session.add(review)
            self._flush_review_write()
            saved.append(self._serialize(review))
        self.session.commit()
        return saved

    def mark_verified(self, paper_id: UUID, payload: ExtractionReviewMarkVerifiedRequest) -> list[ExtractionFieldReviewResponse]:
        canonical_type = self.canonical_target_type(payload.target_type)
        target = self.get_target_or_raise(paper_id, canonical_type, payload.target_id)
        snapshot = self.get_target_field_snapshot(canonical_type, target)
        field_names = payload.field_names or list(snapshot.keys())
        if len(field_names) > 1 and payload.expected_write_version is not None:
            raise ValueError("write_conflict:extraction_review_per_field_versions_required")

        # D1 Phase 3: check target exists (already done by get_target_or_raise above)
        # and check evidence reference and evidence text before marking verified
        has_evidence_ref = has_required_evidence_reference(
            self.session,
            paper_id=paper_id,
            target_type=canonical_type,
            target_id=target.id,
        )

        prepared: list[tuple[str, dict[str, Any], ExtractionFieldReview | None]] = []
        for field_name in field_names:
            if field_name not in snapshot:
                raise ValueError(f"Unsupported field for {canonical_type}: {field_name}")
            field_snapshot = snapshot[field_name]
            # D1 Phase 3: evidence text guard
            evidence_text_value = field_snapshot["evidence_text"]
            has_evidence_text = bool(evidence_text_value and str(evidence_text_value).strip())
            allowed, reason = can_manual_review_mark_verified(
                target_exists=True,
                evidence_reference_exists=has_evidence_ref,
                evidence_text_exists=has_evidence_text,
                target_resolution_status="active",
            )
            if not allowed:
                raise ValueError(
                    f"Cannot mark {canonical_type}.{field_name} as verified: {reason}. "
                    f"Ensure target exists, evidence reference and evidence text are present."
                )
            review = self._find_review(paper_id, canonical_type, payload.target_id, field_name)
            expected_version = self._expected_mark_verified_version(payload, field_name, len(field_names))
            if review is not None:
                self._guard_expected_write_version(review, expected_version, created=False)
            prepared.append((field_name, field_snapshot, review))

        writable: list[tuple[str, dict[str, Any], ExtractionFieldReview]] = []
        for field_name, field_snapshot, existing_review in prepared:
            review = existing_review or self._get_or_create_review(
                paper_id, canonical_type, payload.target_id, field_name
            )
            expected_version = self._expected_mark_verified_version(payload, field_name, len(field_names))
            self._guard_expected_write_version(
                review,
                expected_version,
                created=existing_review is None and getattr(review, "_created_by_get_or_create", False),
            )
            writable.append((field_name, field_snapshot, review))

        saved: list[ExtractionFieldReviewResponse] = []
        for field_name, field_snapshot, review in writable:
            review.original_value = field_snapshot["value"]
            if review.reviewed_value is None:
                review.reviewed_value = field_snapshot["value"]
            review.unit = field_snapshot["unit"]
            review.evidence_text = field_snapshot["evidence_text"]
            review.reviewer_status = "verified"
            review.reviewer = payload.reviewer
            review.reviewer_note = payload.reviewer_note
            review.review_payload = {
                "human_verification": {
                    "reviewer": payload.reviewer,
                    "reviewer_note": payload.reviewer_note,
                    "decision": "verified",
                    "writes_final_truth": True,
                }
            }
            review.target_resolution_status = "active"
            review.remapped_from_target_id = None
            review.last_resolved_target_id = payload.target_id
            self.resolver._refresh_review_identity(review, canonical_type, target)
            self.session.add(review)
            self._flush_review_write()
            saved.append(self._serialize(review))
        self.session.commit()
        return saved

    def reviews_by_target(self, paper_id: UUID) -> dict[tuple[str, str, str], ExtractionFieldReviewResponse]:
        rows = self.list_reviews(paper_id)
        return {(row.target_type, row.target_id, row.field_name): row for row in rows}

    @staticmethod
    def canonical_target_type(value: str) -> str:
        return canonical_target_type(value)

    def get_target_or_raise(self, paper_id: UUID, canonical_type: str, target_id: str):
        model = TARGET_TYPE_MODELS[canonical_type]
        normalized_target_id = UUID(str(target_id))
        row = self.session.scalar(select(model).where(model.paper_id == paper_id, model.id == normalized_target_id))
        if row is None:
            raise LookupError(f"Target not found for {canonical_type}:{target_id}")
        return row

    def get_target_field_snapshot(self, canonical_type: str, row: Any) -> dict[str, dict[str, Any]]:
        return FIELD_SNAPSHOT_BUILDERS[canonical_type](row)

    def _get_or_create_review(self, paper_id: UUID, canonical_type: str, target_id: str, field_name: str) -> ExtractionFieldReview:
        identity_filter = (
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_type == canonical_type,
            ExtractionFieldReview.target_id == target_id,
            ExtractionFieldReview.field_name == field_name,
        )
        review = self.session.scalar(select(ExtractionFieldReview).where(*identity_filter))
        if review is not None:
            review._created_by_get_or_create = False
            return review
        values = {
            "paper_id": paper_id,
            "target_type": canonical_type,
            "target_id": target_id,
            "field_name": field_name,
            "target_resolution_status": "active",
            "last_resolved_target_id": target_id,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect in {"sqlite", "postgresql"}:
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            result = self.session.execute(
                dialect_insert(ExtractionFieldReview)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["paper_id", "target_type", "target_id", "field_name"]
                )
            )
            winner = self.session.scalar(select(ExtractionFieldReview).where(*identity_filter))
            if winner is None:
                raise RuntimeError("extraction_review_upsert_failed")
            winner._created_by_get_or_create = result.rowcount == 1
            return winner
        review = ExtractionFieldReview(**values)
        try:
            with self.session.begin_nested():
                self.session.add(review)
                self.session.flush()
            review._created_by_get_or_create = True
            return review
        except IntegrityError:
            winner = self.session.scalar(select(ExtractionFieldReview).where(*identity_filter))
            if winner is None:
                raise
            winner._created_by_get_or_create = False
            return winner

    def _find_review(
        self,
        paper_id: UUID,
        canonical_type: str,
        target_id: str,
        field_name: str,
    ) -> ExtractionFieldReview | None:
        return self.session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == canonical_type,
                ExtractionFieldReview.target_id == target_id,
                ExtractionFieldReview.field_name == field_name,
            )
        )

    @staticmethod
    def _fill_missing_prepare_fields(
        review: ExtractionFieldReview,
        field_snapshot: dict[str, Any],
        target_id: str,
    ) -> None:
        if review.original_value is None:
            review.original_value = field_snapshot["value"]
        if review.unit is None:
            review.unit = field_snapshot["unit"]
        if not review.evidence_text:
            review.evidence_text = field_snapshot["evidence_text"]
        if not review.reviewer_note:
            review.reviewer_note = "prepared_from_extraction"
        if not review.target_resolution_status:
            review.target_resolution_status = "active"
        if not review.last_resolved_target_id:
            review.last_resolved_target_id = target_id

    def _flush_review_write(self) -> None:
        try:
            self.session.flush()
        except StaleDataError as exc:
            self.session.rollback()
            raise ValueError("write_conflict:extraction_review_version_stale") from exc

    @staticmethod
    def _is_blank(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict)):
            return len(value) == 0
        return False

    @staticmethod
    def _guard_verified_review_mutation(
        review: ExtractionFieldReview,
        incoming_reviewed_value: Any,
        incoming_status: str,
    ) -> None:
        if review.id is None or review.reviewer_status != "verified":
            return
        if incoming_status != "verified":
            raise ValueError("Verified reviews cannot be downgraded through save")
        if incoming_reviewed_value != review.reviewed_value:
            raise ValueError("Verified reviews cannot overwrite reviewed_value")

    @staticmethod
    def _guard_expected_write_version(
        review: ExtractionFieldReview,
        expected_write_version: int | None,
        *,
        created: bool,
    ) -> None:
        if expected_write_version is None:
            if created:
                return
            raise ValueError("write_conflict:extraction_review_version_required")
        if int(review.write_version or 1) != int(expected_write_version):
            raise ValueError("write_conflict:extraction_review_version_stale")

    @staticmethod
    def _expected_mark_verified_version(
        payload: ExtractionReviewMarkVerifiedRequest,
        field_name: str,
        field_count: int,
    ) -> int | None:
        if field_name in payload.expected_write_versions:
            return payload.expected_write_versions[field_name]
        if field_count == 1:
            return payload.expected_write_version
        return None

    @staticmethod
    def result_key(canonical_type: str) -> str:
        return RESULT_KEY_BY_TARGET_TYPE[canonical_type]

    @staticmethod
    def _serialize(row: ExtractionFieldReview) -> ExtractionFieldReviewResponse:
        created = row.created_at.replace(tzinfo=UTC).isoformat() if row.created_at else ""
        updated = row.updated_at.replace(tzinfo=UTC).isoformat() if row.updated_at else ""
        # D1 Phase 3: verified flag is strictly determined by is_safe_verified_review
        # This ensures stale/ambiguous/unresolved/unknown can never be serialized as verified
        return ExtractionFieldReviewResponse(
            id=row.id,
            paper_id=row.paper_id,
            target_type=row.target_type,
            target_id=row.target_id,
            target_fingerprint=row.target_fingerprint,
            target_label=row.target_label,
            field_path=row.field_path,
            target_resolution_status=row.target_resolution_status,  # type: ignore[arg-type]
            remapped_from_target_id=row.remapped_from_target_id,
            last_resolved_target_id=row.last_resolved_target_id,
            field_name=row.field_name,
            original_value=row.original_value,
            reviewed_value=row.reviewed_value,
            unit=row.unit,
            evidence_text=row.evidence_text,
            reviewer_status=row.reviewer_status,  # type: ignore[arg-type]
            reviewer=row.reviewer,
            reviewer_note=row.reviewer_note,
            review_payload=row.review_payload,
            verified=is_safe_verified_review(row),
            created_at=created,
            updated_at=updated,
            write_version=row.write_version,
        )
