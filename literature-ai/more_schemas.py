
class DiscoveryDownloadRequest(BaseModel):
    identifier: str
    providers: list[str] = Field(default_factory=list)
    library_name: str | None = None

class DiscoverySearchResponse(BaseModel):
    query: str
    providers: list[str]
    total: int
    items: list[dict[str, Any]]

class ExtractionRunResponse(BaseModel):
    paper_id: UUID
    status: str
    dft_settings: int = 0
    catalyst_samples: int = 0
    dft_results: int = 0
    electrochemical_performance: int = 0
    mechanism_claims: int = 0
    writing_cards: int = 0

class IngestFromPathRequest(BaseModel):
    pdf_path: str
    title: str | None = None
    doi: str | None = None
    authors: str | None = None
    year: int | None = None
    journal: str | None = None
    abstract: str | None = None

class IngestResponse(BaseModel):
    paper_id: UUID
    title: str | None = None
    status: str

class PaperLibraryResponse(BaseModel):
    name: str
    paper_count: int = 0

class PaperListFilterParams(BaseModel):
    q: str | None = None
    library_name: str | None = None
    source_path: str | None = None
    year: int | None = None
    journal: str | None = None
    has_dft_results: bool | None = None
    paper_type: str | None = None
