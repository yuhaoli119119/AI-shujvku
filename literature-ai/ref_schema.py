
class ReferenceEntryCreate(BaseModel):
    title: str | None = None
    authors: str | None = None
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    volume: str | None = None
    pages: str | None = None
    reference_number: int | None = None
    citation_context: str | None = None
    linked_paper_id: UUID | None = None
