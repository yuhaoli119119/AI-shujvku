from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator


# The MCP handler intentionally receives each item as a plain dictionary so a
# malformed item can be reported alongside valid items in the same batch.  The
# published schema is nevertheless explicit: web AIs must be able to discover
# every direct-apply field without a trial submission.
AI_FIELD_VERIFICATION_SUBMISSION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "target_type", "target_id", "field_name", "decision", "confidence",
        "evidence_text", "page", "expected_target_fingerprint",
    ],
    "properties": {
        "target_type": {"type": "string", "const": "dft_results"},
        "target_id": {"type": "string", "description": "DFT record UUID supplied by the task."},
        "field_name": {"type": "string", "description": "Pending required DFT field supplied by the task."},
        "decision": {"type": "string", "enum": ["accept", "defer", "reject"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_text": {"type": "string", "description": "Exact supporting text from the supplied source page or table cell."},
        "page": {"type": "integer", "minimum": 1},
        "expected_target_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "expected_write_version": {"type": ["integer", "null"], "minimum": 1},
        "source_paper_id": {"type": ["string", "null"]},
        "evidence_paper_id": {"type": ["string", "null"]},
        "table_id": {"type": ["string", "null"]},
        "source_row_index": {"type": ["integer", "null"], "minimum": 0},
        "source_column_index": {"type": ["integer", "null"], "minimum": 0},
        "blocked_reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "counter_evidence_text": {"type": "string", "description": "Required for reject; must be from the same real source page."},
        "reasoning_summary": {"type": "string", "maxLength": 2000},
    },
    "allOf": [
        {"if": {"properties": {"decision": {"const": "defer"}}}, "then": {"required": ["blocked_reasons"]}},
        {"if": {"properties": {"decision": {"const": "reject"}}}, "then": {"required": ["counter_evidence_text"]}},
    ],
}

AIVerificationSubmissionWire = Annotated[
    dict[str, Any],
    WithJsonSchema(AI_FIELD_VERIFICATION_SUBMISSION_SCHEMA),
]


class AIVerificationSubmission(BaseModel):
    """One decision produced by the single authenticated AI caller."""

    model_config = ConfigDict(extra="forbid")

    target_type: str
    target_id: str
    field_name: str
    # ``defer`` records a non-authoritative evidence block.  It deliberately
    # does not share the verified/rejected state machine: an AI may say that
    # current evidence is insufficient without making a scientific rejection.
    decision: Literal["accept", "correct", "reject", "exception", "defer"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_text: str = ""
    page: int | None = Field(default=None, ge=1)
    proposed_value: Any = None
    reasoning_summary: str = Field(default="", max_length=2000)
    counter_evidence_text: str = Field(default="", max_length=12000)
    expected_target_fingerprint: str
    expected_write_version: int | None = Field(default=None, ge=1)
    source_paper_id: str | None = None
    evidence_paper_id: str | None = None
    table_id: str | None = None
    source_row_index: int | None = Field(default=None, ge=0)
    source_column_index: int | None = Field(default=None, ge=0)
    blocked_reasons: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_table_cell_reference(self) -> AIVerificationSubmission:
        reference = (self.table_id, self.source_row_index, self.source_column_index)
        if any(value is not None for value in reference) and not all(value is not None for value in reference):
            raise ValueError(
                "table_id, source_row_index, and source_column_index must be supplied together"
            )
        if self.decision == "defer" and not any(str(reason).strip() for reason in self.blocked_reasons):
            raise ValueError("defer requires at least one blocked_reasons entry")
        return self


class AIVerificationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submissions: list[AIVerificationSubmission] = Field(default_factory=list, max_length=50)
    dry_run: bool = True


class AIFieldVerificationSubmission(AIVerificationSubmission):
    """The direct-apply protocol deliberately cannot mutate source DFT data."""

    decision: Literal["accept", "defer", "reject"]

    @model_validator(mode="after")
    def validate_direct_reject_evidence(self) -> AIFieldVerificationSubmission:
        if self.decision == "reject" and not self.counter_evidence_text.strip():
            raise ValueError("reject requires counter_evidence_text from a real source page")
        return self


class AIFieldVerificationApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    submissions: list[dict[str, Any]] = Field(min_length=1, max_length=20)


class SectionPageFragmentCandidateRef(BaseModel):
    """Opaque reference to one server-recovered page-fragment candidate."""

    model_config = ConfigDict(extra="forbid")

    fragment_id: str
    fragment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SectionPageFragmentMaterializationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    parent_section_id: str
    candidates: list[SectionPageFragmentCandidateRef] = Field(min_length=1, max_length=20)
    dry_run: bool = True
