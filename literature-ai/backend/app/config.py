from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # NOTE: The database is PostgreSQL (with pgvector), NOT SQLite.
    # In production this can be pinned with LITAI_FORCE_CONFIGURED_DATABASE=true
    # so the app never falls back to per-library SQLite databases.
    database_url: str = "postgresql+psycopg://literature_ai:literature_ai@postgres:5432/literature_ai"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"

    grobid_url: str = "http://grobid:8070"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "literature-ai"
    minio_secure: bool = False

    storage_root: Path = Field(default=Path("data/libraries/default/storage"))
    pdf_dir: str = "pdf"
    tei_dir: str = "tei"
    docling_json_dir: str = "docling_json"
    figures_dir: str = "figures"
    tables_dir: str = "tables"
    markdown_dir: str = "markdown"
    docling_enabled: bool = True
    docling_do_ocr: bool = False
    docling_force_full_page_ocr: bool = False
    docling_num_threads: int = 4
    docling_document_timeout: float | None = 120.0
    docling_artifacts_path: Path | None = None

    embedding_provider: str = "openai_compatible"
    embedding_api_base: str | None = "https://api.siliconflow.cn/v1"
    embedding_api_key: str | None = None
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimension: int = 1024
    use_minio: bool = False
    writer_backend: str = "rule"
    writer_prompt_path: Path = Field(default=Path("prompts/paper_writer.yaml"))
    writer_model: str = "gpt-4.1-mini"
    writer_api_base: str | None = None
    writer_api_key: str | None = None
    writer_timeout_seconds: float = 30.0
    writer_fallback_backend: str = "rule"
    mcp_enabled: bool = True
    mcp_api_keys: str = ""
    mcp_server_name: str = "Literature AI MCP"
    force_configured_database: bool = False
    settings_admin_token: str | None = None
    browse_roots: str = "/host/users,/data,/legacy"

    # When False, ingestion only runs basic extraction (metadata/sections/tables/figures/chunks).
    # Stage-2 deep extraction (DFT/electrochemical/mechanism/writing-card) is skipped
    # and must be triggered later by AI via MCP or manual rerun_stage2.
    auto_run_stage2_extraction: bool = True

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="LITAI_",
        extra="ignore",
    )

    @property
    def storage_paths(self) -> dict[str, Path]:
        return {
            "root": self.storage_root,
            "pdf": self.storage_root / self.pdf_dir,
            "tei": self.storage_root / self.tei_dir,
            "docling_json": self.storage_root / self.docling_json_dir,
            "figures": self.storage_root / self.figures_dir,
            "tables": self.storage_root / self.tables_dir,
            "markdown": self.storage_root / self.markdown_dir,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
