"""Scoped historical evidence, never an acceptance or native-control request."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.schemas.staged_extraction import CheckModel, CheckBinding, CheckScope, Digest

Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.:-]+$')]


class HistoryModel(CheckModel):
    @model_validator(mode='after')
    def no_database_nul(self):
        pending=[self.model_dump(mode='json')]
        while pending:
            item=pending.pop()
            if isinstance(item,str) and chr(0) in item:raise ValueError('history_text_contains_nul')
            if isinstance(item,dict):pending.extend(item.keys());pending.extend(item.values())
            elif isinstance(item,list):pending.extend(item)
        return self


class EvidenceFile(HistoryModel):
    path: str = Field(min_length=1, max_length=512)
    sha256: Digest


class CatalogEntry(HistoryModel):
    report_id: Identifier
    paper_id: UUID
    evidence: EvidenceFile


class EvidenceCatalog(HistoryModel):
    format: Literal['history_catalog_v1']
    entries: list[CatalogEntry] = Field(max_length=1000)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({e.report_id for e in self.entries}) != len(self.entries):
            raise ValueError('duplicate_report_id')
        return self


class HistoryScope(CheckScope):
    role: Literal['main', 'si']


class AttemptLink(HistoryModel):
    old_job_id: Identifier
    new_job_id: Identifier
    old_run_id: str | None = Field(max_length=160)
    new_run_id: str | None = Field(max_length=160)
    old_attempt_token: str | None = Field(max_length=160)
    new_attempt_token: str | None = Field(max_length=160)


class HistoricalEvidence(HistoryModel):
    format: Literal['history_evidence_v1']
    paper_id: UUID
    original: EvidenceFile
    historical_verdict: str = Field(min_length=1, max_length=128)
    observed_output_revision: Digest | None
    source_scope: list[HistoryScope] = Field(default_factory=list, max_length=200)
    limitations: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(default_factory=list, max_length=100)
    attempt_links: list[AttemptLink] = Field(default_factory=list, max_length=100)


class HistoryRegistration(HistoryModel):
    request_id: Identifier
    expected_revision: Digest
    expected_binding: CheckBinding
    report_id: Identifier
    expected_report_sha256: Digest
    kind: Literal['report_index', 'attempt_supersession'] = 'report_index'
    old_job_id: Identifier | None = None
    new_job_id: Identifier | None = None
    note: str = Field(min_length=1, max_length=3000)

    @model_validator(mode='after')
    def relation_scope(self):
        if self.kind == 'attempt_supersession':
            if not self.old_job_id or not self.new_job_id or self.old_job_id == self.new_job_id:
                raise ValueError('two_distinct_attempts_required')
        elif self.old_job_id is not None or self.new_job_id is not None:
            raise ValueError('attempt_fields_require_supersession')
        return self
