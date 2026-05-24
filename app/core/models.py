from typing import Optional, List
from datetime import datetime
from sqlmodel import SQLModel, Field, Relationship
from uuid import uuid4, UUID

class Paper(SQLModel, table=True):
    __tablename__ = "papers"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    paper_number: Optional[int] = Field(default=None, index=True)
    title: str
    chinese_title: Optional[str] = None
    normalized_title: Optional[str] = Field(default=None, index=True)
    doi: Optional[str] = Field(default=None, index=True)
    arxiv_id: Optional[str] = Field(default=None, index=True)
    pmid: Optional[str] = Field(default=None, index=True)
    pmcid: Optional[str] = Field(default=None, index=True)
    remote_paper_id: Optional[str] = Field(default=None, index=True)
    year: Optional[int] = None
    journal: Optional[str] = None
    impact_factor: Optional[float] = None
    publisher: Optional[str] = None
    abstract: Optional[str] = None
    authors: Optional[str] = None
    source: Optional[str] = None
    is_oa: int = Field(default=0)
    oa_status: Optional[str] = None
    oa_url: Optional[str] = None
    license: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

class File(SQLModel, table=True):
    __tablename__ = "files"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    paper_id: str = Field(foreign_key="papers.id")
    file_type: str # 'pdf', 'xml', 'txt'
    file_path: str
    original_url: Optional[str] = None
    sha256: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    status: str = Field(default="queued") # 'queued', 'downloading', 'downloaded', 'failed'
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

class Chunk(SQLModel, table=True):
    __tablename__ = "chunks"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    paper_id: str = Field(foreign_key="papers.id")
    file_id: str = Field(foreign_key="files.id")
    section_title: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    chunk_index: int
    text: str
    token_count: int
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

class ExtractionJob(SQLModel, table=True):
    __tablename__ = "extraction_jobs"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    paper_id: str = Field(foreign_key="papers.id")
    schema_name: str
    model_name: str
    status: str = Field(default="queued") # 'queued', 'running', 'success', 'failed'
    error_message: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None

class ExtractedRecord(SQLModel, table=True):
    __tablename__ = "extracted_records"
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    paper_id: str = Field(foreign_key="papers.id")
    job_id: str = Field(foreign_key="extraction_jobs.id")
    schema_name: str
    data_json: str
    confidence_score: float = Field(default=0.0)
    needs_review: int = Field(default=1)
    review_status: str = Field(default="pending") # 'pending', 'approved', 'rejected'
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
