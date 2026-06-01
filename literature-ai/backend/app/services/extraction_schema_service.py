from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample, DFTResult, DFTSetting, ElectrochemicalPerformance, MechanismClaim
from app.schemas.evidence import PageSpan
from app.schemas.extraction import (
    CatalystSampleSchema,
    DFTResultSchema,
    DFTSettingSchema,
    ElectrochemicalPerformanceSchema,
    EvidenceField,
    ExtractionResultsResponse,
    MechanismClaimSchema,
    ValidationWarning,
)
from app.services.extraction_review_service import ExtractionReviewService
from app.services.review_target_resolver import ACTIVE_REVIEW_STATUSES
from app.services.evidence_locator_service import EvidenceLocatorService, LOCATOR_WARNING_CODES
from app.services.extraction_validator import ExtractionValidator


SCHEMA_MODELS = {
    "CatalystSample": CatalystSampleSchema,
    "DFTSetting": DFTSettingSchema,
    "DFTResult": DFTResultSchema,
    "MechanismClaim": MechanismClaimSchema,
    "ElectrochemicalPerformance": ElectrochemicalPerformanceSchema,
}


class ExtractionSchemaService:
    """Schema-driven extraction facade.

    Extraction is represented as typed raw field payloads. Normalization and
    validation remain separate services, so future LLM prompts can fill these
    same schemas without mixing extraction and cleaning in one step.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.validator = ExtractionValidator()
        self.review_service = ExtractionReviewService(session)
        self.locators = EvidenceLocatorService(session)

    def schemas(self) -> dict[str, Any]:
        return {name: model.model_json_schema() for name, model in SCHEMA_MODELS.items()}

    def results(self, paper_id: UUID) -> ExtractionResultsResponse:
        payload = self.result_payload(paper_id)
        field_reviews = self.review_service.list_reviews(paper_id)
        warnings = self.validator.validate_payload(payload)
        warnings.extend(self._review_resolution_warnings(field_reviews))
        warnings.extend(self._locator_warnings(payload))
        status = "needs_review" if any(w.severity in {"warning", "error"} for w in warnings) else "validated"
        return ExtractionResultsResponse(
            paper_id=paper_id,
            schemas=self.schemas(),
            results=payload,
            field_reviews=field_reviews,
            validation_warnings=warnings,
            validation_status=status,
        )

    def result_payload(self, paper_id: UUID) -> dict[str, list[dict[str, Any]]]:
        reviews = self.review_service.reviews_by_target(paper_id)
        results = {
            "CatalystSample": [
                self._with_reviews(paper_id, "catalyst_samples", row.id, self._catalyst(row).model_dump(mode="json"), reviews)
                for row in self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()
            ],
            "DFTSetting": [
                self._with_reviews(paper_id, "dft_settings", row.id, self._dft_setting(row).model_dump(mode="json"), reviews)
                for row in self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id == paper_id)).all()
            ],
            "DFTResult": [
                self._with_reviews(paper_id, "dft_results", row.id, self._dft_result(row).model_dump(mode="json"), reviews)
                for row in self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
            ],
            "MechanismClaim": [
                self._with_reviews(paper_id, "mechanism_claims", row.id, self._mechanism(row).model_dump(mode="json"), reviews)
                for row in self.session.scalars(select(MechanismClaim).where(MechanismClaim.paper_id == paper_id)).all()
            ],
            "ElectrochemicalPerformance": [
                self._with_reviews(paper_id, "electrochemical_performance", row.id, self._electrochemical(row).model_dump(mode="json"), reviews)
                for row in self.session.scalars(select(ElectrochemicalPerformance).where(ElectrochemicalPerformance.paper_id == paper_id)).all()
            ],
        }
        return results

    @staticmethod
    def _field(
        value: Any,
        *,
        unit: str | None = None,
        evidence_text: str | None = None,
        source_section: str | None = None,
        confidence: float | None = None,
    ) -> EvidenceField:
        return EvidenceField(
            value=value,
            unit=unit,
            evidence_text=evidence_text or "",
            source_section=source_section,
            page_span=PageSpan(),
            confidence=confidence,
        )

    def _catalyst(self, row: CatalystSample) -> CatalystSampleSchema:
        evidence = row.evidence_strength or row.synthesis_method or row.name or ""
        return CatalystSampleSchema(
            name=self._field(row.name, evidence_text=evidence, confidence=0.7),
            catalyst_type=self._field(row.catalyst_type, evidence_text=evidence, confidence=0.65),
            metal_centers=self._field(row.metal_centers or [], evidence_text=evidence, confidence=0.65),
            coordination=self._field(row.coordination, evidence_text=evidence, confidence=0.65),
            support=self._field(row.support, evidence_text=evidence, confidence=0.65),
            synthesis_method=self._field(row.synthesis_method, evidence_text=row.synthesis_method or evidence, confidence=0.55),
        )

    def _dft_setting(self, row: DFTSetting) -> DFTSettingSchema:
        raw = row.raw_json or {}
        ev = str(raw.get("supporting_text") or raw.get("extracted") or "")
        convergence_settings = dict(row.convergence_settings or {})
        # Reproducibility is a derived QA summary, not an extracted paper field.
        convergence_settings.pop("reproducibility", None)
        return DFTSettingSchema(
            software=self._field(row.software, evidence_text=ev, confidence=0.65),
            functional=self._field(row.functional, evidence_text=ev, confidence=0.65),
            dispersion_correction=self._field(row.dispersion_correction, evidence_text=ev, confidence=0.55),
            pseudopotential=self._field(row.pseudopotential, evidence_text=ev, confidence=0.55),
            cutoff_energy=self._field(row.cutoff_energy_ev, unit="eV", evidence_text=ev, confidence=0.65),
            k_points=self._field(row.k_points, evidence_text=ev, confidence=0.65),
            convergence_settings=self._field(convergence_settings or None, evidence_text=ev, confidence=0.55),
            vacuum_thickness=self._field(row.vacuum_thickness_a, unit="A", evidence_text=ev, confidence=0.55),
        )

    def _dft_result(self, row: DFTResult) -> DFTResultSchema:
        return DFTResultSchema(
            catalyst=self._field(str(row.catalyst_sample_id) if row.catalyst_sample_id else None, evidence_text=row.evidence_text, confidence=row.confidence),
            adsorbate=self._field(row.adsorbate, evidence_text=row.evidence_text, source_section=row.source_section, confidence=row.confidence),
            energy_type=self._field(row.property_type, evidence_text=row.evidence_text, source_section=row.source_section, confidence=row.confidence),
            value=self._field(row.value, unit=row.unit, evidence_text=row.evidence_text, source_section=row.source_section, confidence=row.confidence),
            reaction_step=self._field(row.reaction_step, evidence_text=row.evidence_text, source_section=row.source_section, confidence=row.confidence),
        )

    def _mechanism(self, row: MechanismClaim) -> MechanismClaimSchema:
        return MechanismClaimSchema(
            claim_type=self._field(row.claim_type, evidence_text=row.evidence_text, confidence=row.confidence),
            claim_text=self._field(row.claim_text, evidence_text=row.evidence_text, confidence=row.confidence),
            key_species=self._field(row.evidence_types or [], evidence_text=row.evidence_text, confidence=row.confidence),
            mechanism_direction=self._field(None, evidence_text=row.evidence_text, confidence=row.confidence),
        )

    def _electrochemical(self, row: ElectrochemicalPerformance) -> ElectrochemicalPerformanceSchema:
        ev = row.evidence_text or ""
        return ElectrochemicalPerformanceSchema(
            sulfur_loading=self._field(row.sulfur_loading_mg_cm2, unit="mg/cm2", evidence_text=ev, confidence=0.65),
            sulfur_content=self._field(row.sulfur_content_wt_percent, unit="wt%", evidence_text=ev, confidence=0.65),
            electrolyte_sulfur_ratio=self._field(row.electrolyte_sulfur_ratio, evidence_text=ev, confidence=0.55),
            capacity=self._field(row.capacity_value, unit="mAh/g", evidence_text=ev, confidence=0.65),
            cycle_number=self._field(row.cycle_number, evidence_text=ev, confidence=0.65),
            rate=self._field(row.rate, evidence_text=ev, confidence=0.65),
            decay_per_cycle=self._field(row.decay_per_cycle, unit="%/cycle", evidence_text=ev, confidence=0.55),
        )

    def _with_reviews(
        self,
        paper_id: UUID,
        canonical_type: str,
        target_id: UUID,
        payload: dict[str, Any],
        reviews: dict[tuple[str, str, str], Any],
    ) -> dict[str, Any]:
        merged = {"target_id": str(target_id), "target_type": canonical_type, **payload}
        for field_name, field_value in payload.items():
            if not isinstance(field_value, dict):
                continue
            locator = self.locators.resolve_field_locator(
                paper_id=paper_id,
                target_type=canonical_type,
                target_id=str(target_id),
                field_name=field_name,
                evidence_text=str(field_value.get("evidence_text") or ""),
                source_section=field_value.get("source_section"),
                page_span=PageSpan.model_validate(field_value.get("page_span") or {}),
            )
            review = reviews.get((canonical_type, str(target_id), field_name))
            if review is None:
                merged[field_name] = {**field_value, "evidence_locator": locator.model_dump(mode="json"), "review": None, "verified": False}
                continue
            is_applicable = review.target_resolution_status in ACTIVE_REVIEW_STATUSES
            merged[field_name] = {
                **field_value,
                "evidence_locator": locator.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
                "verified": review.verified if is_applicable else False,
            }
        return merged

    @staticmethod
    def _review_resolution_warnings(field_reviews: list[Any]) -> list[Any]:
        warnings: list[ValidationWarning] = []
        for review in field_reviews:
            if review.target_resolution_status in ACTIVE_REVIEW_STATUSES:
                continue
            warnings.append(
                ValidationWarning(
                    severity="warning",
                    code="review_target_stale",
                    message=f"Review target is {review.target_resolution_status} after re-extraction and was not applied.",
                    target_type=review.target_type,
                    target_id=review.target_id,
                    field=review.field_name,
                    value={
                        "review_id": str(review.id),
                        "review_resolution_status": review.target_resolution_status,
                    },
                )
            )
        return warnings

    @staticmethod
    def _locator_warnings(payload: dict[str, list[dict[str, Any]]]) -> list[ValidationWarning]:
        warnings: list[ValidationWarning] = []
        for target_type, items in payload.items():
            for item in items:
                target_id = item.get("target_id")
                for field_name, field_value in item.items():
                    if not isinstance(field_value, dict):
                        continue
                    if field_value.get("value") in (None, "", []):
                        continue
                    locator = field_value.get("evidence_locator")
                    if not isinstance(locator, dict):
                        continue
                    status = locator.get("locator_status")
                    code = LOCATOR_WARNING_CODES.get(str(status))
                    if code is None:
                        continue
                    warnings.append(
                        ValidationWarning(
                            severity="warning",
                            code=code,
                            message=f"{target_type}.{field_name} locator status is {status}.",
                            target_type=target_type,
                            target_id=str(target_id) if target_id is not None else None,
                            field=field_name,
                            value={
                                "page": locator.get("page"),
                                "locator_status": status,
                                "warning_reason": locator.get("warning_reason"),
                            },
                        )
                    )
        return warnings
