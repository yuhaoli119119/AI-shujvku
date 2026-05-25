from __future__ import annotations

from datetime import UTC
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    ExtractionFieldReview,
    MechanismClaim,
)
from app.schemas.extraction import (
    ExtractionFieldReviewResponse,
    ExtractionFieldReviewSaveItem,
    ExtractionReviewMarkVerifiedRequest,
)


TARGET_TYPE_ALIASES = {
    "catalystsample": "catalyst_samples",
    "catalyst_samples": "catalyst_samples",
    "catalystsample_schema": "catalyst_samples",
    "dftsetting": "dft_settings",
    "dft_settings": "dft_settings",
    "dftresult": "dft_results",
    "dft_results": "dft_results",
    "mechanismclaim": "mechanism_claims",
    "mechanism_claims": "mechanism_claims",
    "electrochemicalperformance": "electrochemical_performance",
    "electrochemical_performance": "electrochemical_performance",
}

TARGET_TYPE_MODELS = {
    "catalyst_samples": CatalystSample,
    "dft_settings": DFTSetting,
    "dft_results": DFTResult,
    "mechanism_claims": MechanismClaim,
    "electrochemical_performance": ElectrochemicalPerformance,
}

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

    def save_reviews(self, paper_id: UUID, items: list[ExtractionFieldReviewSaveItem]) -> list[ExtractionFieldReviewResponse]:
        saved: list[ExtractionFieldReviewResponse] = []
        for item in items:
            canonical_type = self.canonical_target_type(item.target_type)
            target = self.get_target_or_raise(paper_id, canonical_type, item.target_id)
            snapshot = self.get_target_field_snapshot(canonical_type, target)
            if item.field_name not in snapshot:
                raise ValueError(f"Unsupported field for {canonical_type}: {item.field_name}")
            field_snapshot = snapshot[item.field_name]
            review = self._get_or_create_review(paper_id, canonical_type, item.target_id, item.field_name)
            review.original_value = item.original_value if item.original_value is not None else field_snapshot["value"]
            review.reviewed_value = item.reviewed_value
            review.unit = item.unit if item.unit is not None else field_snapshot["unit"]
            review.evidence_text = item.evidence_text if item.evidence_text is not None else field_snapshot["evidence_text"]
            review.reviewer_status = item.reviewer_status
            review.reviewer = item.reviewer
            review.reviewer_note = item.reviewer_note
            self.session.add(review)
            self.session.flush()
            saved.append(self._serialize(review))
        self.session.commit()
        return saved

    def mark_verified(self, paper_id: UUID, payload: ExtractionReviewMarkVerifiedRequest) -> list[ExtractionFieldReviewResponse]:
        canonical_type = self.canonical_target_type(payload.target_type)
        target = self.get_target_or_raise(paper_id, canonical_type, payload.target_id)
        snapshot = self.get_target_field_snapshot(canonical_type, target)
        field_names = payload.field_names or list(snapshot.keys())

        saved: list[ExtractionFieldReviewResponse] = []
        for field_name in field_names:
            if field_name not in snapshot:
                raise ValueError(f"Unsupported field for {canonical_type}: {field_name}")
            field_snapshot = snapshot[field_name]
            review = self._get_or_create_review(paper_id, canonical_type, payload.target_id, field_name)
            review.original_value = field_snapshot["value"]
            if review.reviewed_value is None:
                review.reviewed_value = field_snapshot["value"]
            review.unit = field_snapshot["unit"]
            review.evidence_text = field_snapshot["evidence_text"]
            review.reviewer_status = "verified"
            review.reviewer = payload.reviewer
            review.reviewer_note = payload.reviewer_note
            self.session.add(review)
            self.session.flush()
            saved.append(self._serialize(review))
        self.session.commit()
        return saved

    def reviews_by_target(self, paper_id: UUID) -> dict[tuple[str, str, str], ExtractionFieldReviewResponse]:
        rows = self.list_reviews(paper_id)
        return {(row.target_type, row.target_id, row.field_name): row for row in rows}

    @staticmethod
    def canonical_target_type(value: str) -> str:
        key = (value or "").replace("-", "_").replace(" ", "_").lower()
        canonical = TARGET_TYPE_ALIASES.get(key)
        if canonical is None:
            raise ValueError(f"Unsupported target_type: {value}")
        return canonical

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
        review = self.session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == paper_id,
                ExtractionFieldReview.target_type == canonical_type,
                ExtractionFieldReview.target_id == target_id,
                ExtractionFieldReview.field_name == field_name,
            )
        )
        if review is not None:
            return review
        return ExtractionFieldReview(
            paper_id=paper_id,
            target_type=canonical_type,
            target_id=target_id,
            field_name=field_name,
        )

    @staticmethod
    def result_key(canonical_type: str) -> str:
        return RESULT_KEY_BY_TARGET_TYPE[canonical_type]

    @staticmethod
    def _serialize(row: ExtractionFieldReview) -> ExtractionFieldReviewResponse:
        created = row.created_at.replace(tzinfo=UTC).isoformat() if row.created_at else ""
        updated = row.updated_at.replace(tzinfo=UTC).isoformat() if row.updated_at else ""
        return ExtractionFieldReviewResponse(
            id=row.id,
            paper_id=row.paper_id,
            target_type=row.target_type,
            target_id=row.target_id,
            field_name=row.field_name,
            original_value=row.original_value,
            reviewed_value=row.reviewed_value,
            unit=row.unit,
            evidence_text=row.evidence_text,
            reviewer_status=row.reviewer_status,  # type: ignore[arg-type]
            reviewer=row.reviewer,
            reviewer_note=row.reviewer_note,
            verified=row.reviewer_status == "verified",
            created_at=created,
            updated_at=updated,
        )
