from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    MechanismClaim,
    Paper,
    PaperCorrection,
    WritingCard,
)


@dataclass(frozen=True)
class StructuredTargetSpec:
    model: type
    allowed_fields: frozenset[str]


class ReviewService:
    ALLOWED_PAPER_FIELDS = {
        "doi",
        "title",
        "year",
        "journal",
        "authors",
        "abstract",
        "oa_status",
        "license",
    }
    ALLOWED_DFT_RESULT_FIELDS = {
        "adsorbate",
        "property_type",
        "value",
        "unit",
        "reaction_step",
        "source_section",
        "source_figure",
        "evidence_text",
        "confidence",
    }
    STRUCTURED_TARGETS = {
        "dft_results": StructuredTargetSpec(
            model=DFTResult,
            allowed_fields=frozenset(ALLOWED_DFT_RESULT_FIELDS),
        ),
        "mechanism_claims": StructuredTargetSpec(
            model=MechanismClaim,
            allowed_fields=frozenset({"claim_type", "claim_text", "evidence_types", "confidence", "evidence_text"}),
        ),
        "electrochemical_performance": StructuredTargetSpec(
            model=ElectrochemicalPerformance,
            allowed_fields=frozenset(
                {
                    "sulfur_loading_mg_cm2",
                    "sulfur_content_wt_percent",
                    "electrolyte_sulfur_ratio",
                    "capacity_value",
                    "cycle_number",
                    "rate",
                    "decay_per_cycle",
                    "evidence_text",
                }
            ),
        ),
        "catalyst_samples": StructuredTargetSpec(
            model=CatalystSample,
            allowed_fields=frozenset(
                {
                    "name",
                    "catalyst_type",
                    "metal_centers",
                    "coordination",
                    "support",
                    "synthesis_method",
                    "evidence_strength",
                }
            ),
        ),
        "dft_settings": StructuredTargetSpec(
            model=DFTSetting,
            allowed_fields=frozenset(
                {
                    "software",
                    "functional",
                    "dispersion_correction",
                    "pseudopotential",
                    "cutoff_energy_ev",
                    "k_points",
                    "convergence_settings",
                    "vacuum_thickness_a",
                    "raw_json",
                }
            ),
        ),
        "writing_cards": StructuredTargetSpec(
            model=WritingCard,
            allowed_fields=frozenset(
                {
                    "paper_type",
                    "research_gap",
                    "proposed_solution",
                    "core_hypothesis",
                    "evidence_chain",
                    "section_strategy",
                    "figure_logic",
                    "abstract_logic",
                    "introduction_logic",
                    "discussion_logic",
                }
            ),
        ),
    }

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_corrections(self, status: str | None = "pending") -> list[PaperCorrection]:
        stmt = select(PaperCorrection).order_by(PaperCorrection.created_at.desc())
        if status:
            stmt = stmt.where(PaperCorrection.status == status)
        return self.session.scalars(stmt).all()

    def approve_correction(self, correction_id: UUID, reviewer: str) -> PaperCorrection:
        correction = self._get_correction(correction_id)
        if correction.status != "pending":
            raise ValueError("Correction is not pending")

        self._apply_correction(correction)
        correction.status = "approved"
        correction.reviewed_by = reviewer
        correction.reviewed_at = datetime.utcnow()
        self.session.add(correction)
        self.session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action="approve_correction",
                source=reviewer,
                target_type="paper_correction",
                target_id=str(correction.id),
                payload={
                    "field_name": correction.field_name,
                    "target_path": correction.target_path,
                    "operation": correction.operation,
                },
            )
        )
        self.session.flush()
        self.session.refresh(correction)
        return correction

    def get_correction_detail(self, correction_id: UUID) -> dict[str, Any]:
        correction = self._get_correction(correction_id)
        try:
            current_value = self._resolve_current_value(correction)
            target_exists = True
        except ValueError:
            current_value = None
            target_exists = False

        return {
            "correction": correction,
            "current_value": current_value,
            "target_exists": target_exists,
        }

    def reject_correction(self, correction_id: UUID, reviewer: str, reason: str | None = None) -> PaperCorrection:
        correction = self._get_correction(correction_id)
        if correction.status != "pending":
            raise ValueError("Correction is not pending")

        correction.status = "rejected"
        correction.reviewed_by = reviewer
        correction.reviewed_at = datetime.utcnow()
        self.session.add(correction)
        self.session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action="reject_correction",
                source=reviewer,
                target_type="paper_correction",
                target_id=str(correction.id),
                payload={"reason": reason} if reason else None,
            )
        )
        self.session.flush()
        self.session.refresh(correction)
        return correction

    def _apply_correction(self, correction: PaperCorrection) -> None:
        if correction.operation != "replace":
            raise ValueError("Only replace corrections are supported in the current review flow")

        if correction.target_path == correction.field_name and correction.field_name in self.ALLOWED_PAPER_FIELDS:
            paper = self.session.get(Paper, correction.paper_id)
            if not paper:
                raise ValueError("Paper not found")

            setattr(paper, correction.field_name, correction.proposed_value)
            self.session.add(paper)
            return

        if correction.field_name in self.STRUCTURED_TARGETS:
            self._apply_structured_correction(correction)
            return

        raise ValueError(f"Correction field is not review-applicable yet: {correction.field_name}")

    def _apply_structured_correction(self, correction: PaperCorrection) -> None:
        record, _, attribute = self._resolve_structured_target(correction)
        setattr(record, attribute, correction.proposed_value)
        self.session.add(record)

    def _resolve_current_value(self, correction: PaperCorrection) -> Any:
        if correction.target_path == correction.field_name and correction.field_name in self.ALLOWED_PAPER_FIELDS:
            paper = self.session.get(Paper, correction.paper_id)
            if not paper:
                raise ValueError("Paper not found")
            return getattr(paper, correction.field_name)

        if correction.field_name in self.STRUCTURED_TARGETS:
            record, _, attribute = self._resolve_structured_target(correction)
            return getattr(record, attribute)

        raise ValueError("Correction target cannot be resolved")

    def _resolve_structured_target(self, correction: PaperCorrection) -> tuple[Any, StructuredTargetSpec, str]:
        collection, row_id_text, attribute = self._parse_structured_target_path(correction.target_path)
        spec = self.STRUCTURED_TARGETS.get(collection)
        if spec is None:
            raise ValueError("Unsupported structured correction target")
        if correction.field_name != collection:
            raise ValueError("Correction field_name must match structured target collection")
        if attribute not in spec.allowed_fields:
            raise ValueError(f"Structured correction field is not review-applicable yet: {attribute}")

        record = self.session.get(spec.model, UUID(row_id_text))
        if not record:
            raise ValueError(f"{collection} row not found")
        if getattr(record, "paper_id", None) != correction.paper_id:
            raise ValueError(f"{collection} row does not belong to the target paper")
        return record, spec, attribute

    @staticmethod
    def _parse_structured_target_path(target_path: str) -> tuple[str, str, str]:
        parts = [part.strip() for part in target_path.split(":")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("Structured correction target path must use format <collection>:<row_id>:<field>")
        return parts[0], parts[1], parts[2]

    def _get_correction(self, correction_id: UUID) -> PaperCorrection:
        correction = self.session.get(PaperCorrection, correction_id)
        if not correction:
            raise ValueError("Correction not found")
        return correction
