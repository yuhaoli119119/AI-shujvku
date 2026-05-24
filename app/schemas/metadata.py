from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class AuthorMetadata(BaseModel):
    name: str
    position: Optional[int | str] = None
    orcid: Optional[str] = None
    institution: Optional[str] = None

class PaperMetadata(BaseModel):
    title: str
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    publication_year: Optional[int] = None
    journal: Optional[str] = None
    publisher: Optional[str] = None
    abstract: Optional[str] = None
    authors: List[AuthorMetadata] = []
    source: str
    openalex_id: Optional[str] = None
    is_oa: bool = False
    oa_status: Optional[str] = None
    license: Optional[str] = None
    pdf_url: Optional[str] = None

class OALocation(BaseModel):
    url: str
    url_for_pdf: Optional[str] = None
    is_best: bool = False
    license: Optional[str] = None
    version: Optional[str] = None
    host_type: Optional[str] = None
