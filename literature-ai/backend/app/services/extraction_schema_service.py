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
)
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

    def schemas(self) -> dict[str, Any]:
        return {name: model.model_json_schema() for name, model in SCHEMA_MODELS.items()}

    def results(self, paper_id: UUID) -> ExtractionResultsResponse:
        results = {
            "CatalystSample": [self._catalyst(row) for row in self.session.scalars(select(CatalystSample).where(CatalystSample.paper_id == paper_id)).all()],
            "DFTSetting": [self._dft_setting(row) for row in self.session.scalars(select(DFTSetting).where(DFTSetting.paper_id == paper_id)).all()],
            "DFTResult": [self._dft_result(row) for row in self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()],
            "MechanismClaim": [self._mechanism(row) for row in self.session.scalars(select(MechanismClaim).where(MechanismClaim.paper_id == paper_id)).all()],
            "ElectrochemicalPerformance": [
                self._electrochemical(row)
                for row in self.session.scalars(select(ElectrochemicalPerformance).where(ElectrochemicalPerformance.paper_id == paper_id)).all()
            ],
        }
        warnings = self.validator.validate_payload(results)
        status = "needs_review" if any(w.severity in {"warning", "error"} for w in warnings) else "validated"
        return ExtractionResultsResponse(
            paper_id=paper_id,
            schemas=self.schemas(),
            results={name: [item.model_dump(mode="json") for item in items] for name, items in results.items()},
            validation_warnings=warnings,
            validation_status=status,
        )

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
        return DFTSettingSchema(
            software=self._field(row.software, evidence_text=ev, confidence=0.65),
            functional=self._field(row.functional, evidence_text=ev, confidence=0.65),
            dispersion_correction=self._field(row.dispersion_correction, evidence_text=ev, confidence=0.55),
            pseudopotential=self._field(row.pseudopotential, evidence_text=ev, confidence=0.55),
            cutoff_energy=self._field(row.cutoff_energy_ev, unit="eV", evidence_text=ev, confidence=0.65),
            k_points=self._field(row.k_points, evidence_text=ev, confidence=0.65),
            convergence_settings=self._field(row.convergence_settings or {}, evidence_text=ev, confidence=0.55),
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

