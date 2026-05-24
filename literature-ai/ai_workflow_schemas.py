
class AIWorkflowPayload(BaseModel):
    query: str
    model: str | None = None
    library_name: str | None = None
    providers: list[str] | None = None
    max_results: int = 10
    max_downloads: int = 5
    skip_existing: bool = True

class AISearchPayload(BaseModel):
    query: str
    model: str | None = None
    providers: list[str] | None = None
    max_results: int = 10

class AISearchResponse(BaseModel):
    query: str
    prompt_used: str
    providers: list[str]
    results: list[dict[str, Any]]
    llm_status: str | None = None
    llm_error: str | None = None
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)

class AIWorkflowFailedItemResponse(BaseModel):
    identifier: str
    title: str | None = None
    code: str
    reason: str

class AIWorkflowIngestedPaperResponse(BaseModel):
    paper_id: UUID
    title: str | None = None
    status: str
    identifier: str
    doi: str | None = None

class AIWorkflowResponse(BaseModel):
    query: str
    prompt_used: str
    providers: list[str]
    searched_total: int
    attempted_downloads: int
    ingested: list[AIWorkflowIngestedPaperResponse] = Field(default_factory=list)
    failed: list[AIWorkflowFailedItemResponse] = Field(default_factory=list)
    llm_status: str | None = None
    llm_error: str | None = None
    llm_diagnostics: dict[str, Any] = Field(default_factory=dict)
