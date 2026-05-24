
class WriterSettingsResponse(BaseModel):
    writer_backend: str | None = None
    writer_model: str | None = None
    writer_api_base: str | None = None
    writer_api_key: str | None = None
    writer_fallback_backend: str | None = None

class WriterSettingsUpdateRequest(BaseModel):
    writer_backend: str
    writer_model: str
    writer_api_base: str | None = None
    writer_api_key: str | None = None
