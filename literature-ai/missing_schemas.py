
class LibraryCreateRequest(BaseModel):
    name: str
    root_path: str
    description: str | None = None


class LibraryImportRequest(BaseModel):
    root_path: str


class LibraryInfoResponse(BaseModel):
    name: str
    root_path: str
    description: str = ""
    paper_count: int = 0
    is_active: bool = False
    created_at: str | None = None
