from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class GuideSection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    background_gap: str = Field(default='', max_length=5000)
    research_objective: str = Field(default='', max_length=4000)
    study_design: str = Field(default='', max_length=6000)
    innovation: str = Field(default='', max_length=5000)
    conclusions: str = Field(default='', max_length=6000)
    limitations: str = Field(default='', max_length=5000)


class GuideEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    asset_id: UUID
    question: str = Field(min_length=1, max_length=1500)
    finding: str = Field(min_length=1, max_length=2000)


class GuideTerm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    term: str = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=1, max_length=1500)


class ReadingAssistanceSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    save_mode: Literal['full'] = 'full'
    abstract_source_revision: str | None = Field(default=None, min_length=64, max_length=64)
    request_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=100)
    input_revision: str = Field(min_length=64, max_length=64)
    abstract_zh: str = Field(default='', max_length=15000)
    abstract_missing_reason: str = Field(default='', max_length=1000)
    one_sentence: str = Field(min_length=1, max_length=1500)
    problem: str = Field(default='', max_length=2000)
    main_finding: str = Field(default='', max_length=2500)
    guide: GuideSection
    argument_flow: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[GuideEvidence] = Field(default_factory=list, max_length=5)
    terms: list[GuideTerm] = Field(default_factory=list, max_length=8)
    content_scope: Literal['abstract', 'main_text', 'main_and_si', 'partial_text']
    missing: list[str] = Field(default_factory=list, max_length=15)


class AbstractTranslationSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    save_mode: Literal['abstract_only']
    request_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=100)
    input_revision: str = Field(min_length=64, max_length=64)
    abstract_source_revision: str = Field(min_length=64, max_length=64)
    abstract_zh: str = Field(min_length=1, max_length=15000)
