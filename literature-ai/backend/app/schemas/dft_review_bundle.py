from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ReviewSourceType = Literal["web_ai", "local_ai", "human", "other"]
ReviewDecision = Literal["PASS", "REVISE", "REJECT", "NEEDS_HUMAN", "new_candidate"]
OverallReviewStatus = Literal["completed", "needs_human", "uncertain"]
DFTReviewMode = Literal["target_review", "gap_discovery"]


class OfflineReviewSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_source_type: ReviewSourceType
    reviewer_label: str = Field(min_length=1, max_length=160)
    reviewer_model: str | None = Field(default=None, max_length=255)
    tool_capabilities: list[str] = Field(default_factory=lambda: ["none"])

    @field_validator("reviewer_label")
    @classmethod
    def strip_reviewer_label(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reviewer_label must not be blank")
        return stripped

    @field_validator("reviewer_model")
    @classmethod
    def strip_reviewer_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("tool_capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip().lower() for item in value if str(item).strip()))
        return normalized or ["none"]


class OfflineObjectReviewAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["dft_results"] = "dft_results"
    target_id: str = Field(min_length=1, max_length=64)
    temporary_id: str | None = Field(default=None, max_length=96)
    field_name: str = Field(default="dft_results", min_length=1, max_length=128)
    decision: ReviewDecision
    evidence_checked: bool
    evidence_ids: list[str] = Field(min_length=1)
    corrected_value: Any = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    blocking_errors: list[str] = Field(default_factory=list)
    recommended_action: str | None = None
    dedupe_analysis: dict[str, Any] | None = None

    @field_validator("target_id", "field_name", "reason")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("recommended_action")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("temporary_id")
    @classmethod
    def strip_temporary_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("evidence_ids")
    @classmethod
    def normalize_evidence_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not normalized:
            raise ValueError("at least one evidence_id is required")
        return normalized

    @field_validator("blocking_errors")
    @classmethod
    def normalize_blocking_errors(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @model_validator(mode="after")
    def validate_decision_shape(self) -> "OfflineObjectReviewAudit":
        if self.decision == "new_candidate":
            if self.target_id.lower() != "new" or self.field_name != "dft_results":
                raise ValueError("new_candidate requires target_id='new' and field_name='dft_results'")
            if not isinstance(self.corrected_value, dict):
                raise ValueError("new_candidate requires corrected_value to be an object")
            missing = [
                field
                for field in ("material_identity", "property_type", "value", "unit")
                if self.corrected_value.get(field) in (None, "")
            ]
            if missing:
                raise ValueError(f"new_candidate corrected_value is missing: {', '.join(missing)}")
            if not self.temporary_id:
                canonical = json.dumps(self.corrected_value, ensure_ascii=False, sort_keys=True, default=str)
                self.temporary_id = "new-dft-auto-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        elif self.target_id.lower() == "new":
            raise ValueError("target_id='new' is only valid for decision='new_candidate'")
        if self.decision == "REVISE" and self.corrected_value is None:
            raise ValueError("REVISE requires corrected_value")
        return self


class OfflineDFTReviewCoverageAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_target_ids: list[str] = Field(default_factory=list)
    reviewed_target_ids: list[str] = Field(default_factory=list)
    coverage_complete: bool = False

    @field_validator("expected_target_ids", "reviewed_target_ids")
    @classmethod
    def normalize_target_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


class OfflineDFTReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["offline_dft_review_result_v1"] = "offline_dft_review_result_v1"
    bundle_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    figure_table_completed_snapshot_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "The completed/not_required figure-table review snapshot bound to this DFT review result. "
            "If the figure/table snapshot changes, this result must be regenerated."
        ),
    )
    paper_id: str = Field(min_length=1, max_length=64)
    paper_code: str = Field(min_length=1, max_length=64)
    chart_scope_type: Literal["paper", "external_analysis_run", "paper_reviewed_aggregate"] = "paper_reviewed_aggregate"
    chart_run_id: str | None = Field(default=None, max_length=64)
    review_mode: DFTReviewMode
    review_source: OfflineReviewSource
    overall_status: OverallReviewStatus = Field(
        description=(
            "In target_review, use completed only after reviewing every current writable main-paper DFT target. "
            "In gap_discovery, completed means the missing-data search is complete, not that terminal rows were re-reviewed."
        )
    )
    coverage_acknowledgement: OfflineDFTReviewCoverageAck | None = Field(
        default=None,
        description=(
            "Optional explicit coverage checklist copied from return_template.json. "
            "The server still derives final coverage from object_review_audits."
        ),
    )
    object_review_audits: list[OfflineObjectReviewAudit] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("bundle_fingerprint", "paper_id", "paper_code")
    @classmethod
    def strip_identity(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("identity value must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_chart_scope(self) -> "OfflineDFTReviewResult":
        if self.chart_scope_type == "external_analysis_run" and not self.chart_run_id:
            raise ValueError("external_analysis_run DFT review requires chart_run_id")
        if self.chart_scope_type in {"paper", "paper_reviewed_aggregate"} and self.chart_run_id:
            raise ValueError("paper-level DFT review must not include chart_run_id")
        return self

    @field_validator("uncertainties", "notes")
    @classmethod
    def normalize_notes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
