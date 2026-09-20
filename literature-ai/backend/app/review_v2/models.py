from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FigureActionName = Literal["KEEP", "UPDATE", "RECROP", "COMPOSE_PAGES", "CREATE", "DELETE_FALSE_POSITIVE", "HOLD"]
TableActionName = Literal["KEEP", "UPDATE", "CREATE", "DELETE_FALSE_POSITIVE", "HOLD"]

class EvidenceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_id: str | None = Field(default=None, description="Main or explicitly linked SI paper UUID.")
    page: int | None = Field(default=None, ge=1)
    section_id: str | None = None
    section: str | None = None
    quote: str | None = Field(default=None, description="Verbatim caption or body-text evidence.")
    evidence_type: Literal["caption", "body_text", "visual"] | None = None
    visual_observation: str | None = Field(default=None, description="What is visibly observed; never invent values.")
    @model_validator(mode="after")
    def require_anchor(self):
        if not any((self.quote, self.visual_observation, self.section_id, self.section, self.page)):
            raise ValueError("evidence locator requires a real caption/body/visual anchor")
        return self

class PanelType(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=32)
    figure_types: list[str] = Field(min_length=1)
    raw_type_name: str | None = None

class SubfigureReading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, description="Non-empty Chinese explanation for this actual panel.")
    methods: str | None = None
    findings: str | None = None

    @field_validator("description")
    @classmethod
    def description_must_be_chinese(cls, value):
        text = str(value).strip()
        if not text or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            raise ValueError("subfigure description must contain non-empty Chinese text")
        return text

class FigureTyping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    figure_type_primary: str
    figure_types: list[str] = Field(min_length=1)
    panel_types: list[PanelType] = Field(default_factory=list)
    figure_role: str | None = Field(default=None, description="Scientific role, separate from standardized type.")
    type_confidence: float = Field(ge=0.0, le=1.0)
    type_evidence: list[EvidenceLocator] = Field(min_length=1)
    raw_type_name: str | None = Field(default=None, description="Required for uncovered type stored as other.")
    @model_validator(mode="after")
    def primary_in_all(self):
        if self.figure_type_primary not in self.figure_types:
            raise ValueError("figure_type_primary must appear in figure_types")
        if self.figure_type_primary == "other" and not (self.raw_type_name or "").strip():
            raise ValueError("other requires raw_type_name")
        return self

class PageCrop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(ge=1)
    bbox_norm: list[float] = Field(min_length=4, max_length=4)
    @field_validator("bbox_norm")
    @classmethod
    def bbox(cls, value):
        vals = [float(x) for x in value]
        if not (0 <= vals[0] < vals[2] <= 1 and 0 <= vals[1] < vals[3] <= 1):
            raise ValueError("bbox_norm must be [left, top, right, bottom] in 0..1")
        return vals

class FigureReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: FigureActionName
    figure_id: str | None = None
    source_paper_id: str | None = None
    expected_object_version: str | None = Field(default=None, description="Version returned by get_paper_review_task.")
    page: int | None = Field(default=None, ge=1)
    bbox_norm: list[float] | None = Field(default=None, min_length=4, max_length=4)
    pages: list[PageCrop] = Field(default_factory=list, description="Ordered crops for COMPOSE_PAGES.")
    figure_label: str | None = None
    caption: str | None = None
    figure_role: str | None = None
    content_summary: str | None = None
    key_elements: list[str] | None = None
    typing: FigureTyping | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[EvidenceLocator] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    @model_validator(mode="after")
    def shape(self):
        existing = {"KEEP", "UPDATE", "RECROP", "COMPOSE_PAGES", "DELETE_FALSE_POSITIVE", "HOLD"}
        if self.action in existing and (not self.figure_id or not self.expected_object_version):
            raise ValueError(f"{self.action} requires figure_id and expected_object_version")
        if self.action == "CREATE" and (self.figure_id or not self.source_paper_id):
            raise ValueError("CREATE requires source_paper_id and no figure_id")
        if self.action in {"RECROP", "CREATE"} and (self.page is None or self.bbox_norm is None):
            raise ValueError(f"{self.action} requires page and bbox_norm")
        if self.action == "COMPOSE_PAGES" and len(self.pages) < 2:
            raise ValueError("COMPOSE_PAGES requires at least two ordered page crops")
        if self.action == "COMPOSE_PAGES" and any(self.pages[index].page >= self.pages[index + 1].page for index in range(len(self.pages) - 1)):
            raise ValueError("COMPOSE_PAGES page crops must be strictly increasing without duplicates")
        if self.action == "DELETE_FALSE_POSITIVE" and not self.evidence:
            raise ValueError("DELETE_FALSE_POSITIVE requires PDF evidence")
        return self

class TableReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: TableActionName
    table_id: str | None = None
    source_paper_id: str | None = None
    expected_object_version: str | None = None
    caption: str | None = None
    markdown_content: str | None = None
    page: int | None = Field(default=None, ge=1)
    evidence: list[EvidenceLocator] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    @model_validator(mode="after")
    def shape(self):
        if self.action in {"KEEP", "UPDATE", "DELETE_FALSE_POSITIVE", "HOLD"} and (not self.table_id or not self.expected_object_version):
            raise ValueError(f"{self.action} requires table_id and expected_object_version")
        if self.action == "CREATE" and (self.table_id or not self.source_paper_id):
            raise ValueError("CREATE requires source_paper_id and no table_id")
        if self.action == "DELETE_FALSE_POSITIVE" and not self.evidence:
            raise ValueError("DELETE_FALSE_POSITIVE requires PDF evidence")
        return self

class FigureReadingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    figure_id: str
    source_paper_id: str
    expected_object_version: str
    summary_zh: str = Field(min_length=1)
    detailed_explanation_zh: str = Field(min_length=1)
    evidence_locators: list[EvidenceLocator] = Field(min_length=1)
    uncertainties_zh: list[str] = Field(default_factory=list)
    subfigures: list[SubfigureReading] = Field(default_factory=list)
    typing: FigureTyping
    @field_validator("summary_zh", "detailed_explanation_zh")
    @classmethod
    def must_be_chinese_led(cls, value):
        text = str(value).strip()
        if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            raise ValueError("Chinese reading fields must contain Chinese text")
        return text

    @model_validator(mode="after")
    def subfigures_match_panel_types(self):
        panel_labels = [item.label.strip() for item in self.typing.panel_types]
        subfigure_labels = [item.label.strip() for item in self.subfigures]
        if len(panel_labels) != len(set(panel_labels)):
            raise ValueError("typing.panel_types contains duplicate labels")
        if len(subfigure_labels) != len(set(subfigure_labels)):
            raise ValueError("subfigures contains duplicate labels")
        if set(panel_labels) != set(subfigure_labels):
            raise ValueError(
                "subfigure labels must exactly match typing.panel_types labels:"
                f"panel_types={panel_labels};subfigures={subfigure_labels}"
            )
        return self

class PaperReviewBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper_id: str
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    task_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_label: str = Field(default="paper_review_v2_ai", min_length=1, max_length=64)
    figure_actions: list[FigureReviewAction] = Field(default_factory=list)
    table_actions: list[TableReviewAction] = Field(default_factory=list)
    figure_readings: list[FigureReadingInput] = Field(default_factory=list)
    final_state: Literal["completed", "completed_with_issues", "blocked"] = "completed"
    notes: list[str] = Field(default_factory=list)
