from functools import lru_cache
import logging
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils.project_paths import BACKEND_ROOT, PROJECT_ROOT

logger = logging.getLogger(__name__)
_reported_storage_root_conflicts: set[tuple[str, str, str]] = set()


def _looks_explicit_absolute_path(value: str | Path) -> bool:
    text = str(value).strip()
    if not text:
        return False
    return Path(text).is_absolute() or text.startswith(("/", "\\"))


def _resolve_storage_root(value: str | Path) -> Path:
    text = str(value).strip()
    if not text:
        return (PROJECT_ROOT / "data" / "storage").resolve()
    if _looks_explicit_absolute_path(text):
        return Path(text)
    return (PROJECT_ROOT / text).resolve()


def _warn_if_shadow_storage_roots_exist(active_root: Path) -> None:
    repo_storage_root = (PROJECT_ROOT / "data" / "storage").resolve()
    backend_storage_root = (BACKEND_ROOT / "data" / "storage").resolve()
    if repo_storage_root == backend_storage_root:
        return
    if not repo_storage_root.exists() or not backend_storage_root.exists():
        return
    key = (str(repo_storage_root), str(backend_storage_root), str(active_root))
    if key in _reported_storage_root_conflicts:
        return
    _reported_storage_root_conflicts.add(key)
    logger.warning(
        "Detected multiple storage roots on host: repo=%s backend=%s; using %s",
        repo_storage_root,
        backend_storage_root,
        active_root,
    )


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
    # Automatically enable OCR only when preflight says native text is insufficient.
    # OCR output still remains subject to the human-confirmation safety boundary.
    docling_auto_ocr: bool = True
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
    # HTTP MCP is key-gated.  In-process IDE integrations use mcp_auth_context
    # and are intentionally independent from this transport setting.
    mcp_allow_unauthenticated: bool = False
    mcp_api_keys: str = ""
    mcp_server_name: str = "Literature AI MCP"
    owner_api_token: str | None = None
    exports_enabled: bool = False
    local_ingest_roots: str = "/host/users"
    share_max_page_size: int = 50
    share_rate_limit_per_minute: int = 120
    share_max_concurrency: int = 8
    share_public_base_url: str | None = None
    force_configured_database: bool = True
    enable_deprecated_db_endpoints: bool = False
    settings_admin_token: str | None = None
    browse_roots: str = "/host/users,/data,/legacy"

    # Compatibility flag for legacy backend stage-2 extraction. The primary workflow
    # does NOT require backend-owned LLM parsing. When False, ingestion only runs
    # basic extraction (metadata/sections/tables/figures/chunks), and later AI work
    # should happen through MCP after the workspace and AI reading package are prepared.
    auto_run_stage2_extraction: bool = True
    auto_enrich_ingested_metadata: bool = True
    metadata_enrichment_timeout_seconds: float = 5.0
    workflow_fallback_max_workers: int = 2

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="LITAI_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _normalize_storage_root(self) -> "Settings":
        normalized_root = _resolve_storage_root(self.storage_root)
        object.__setattr__(self, "storage_root", normalized_root)
        _warn_if_shadow_storage_roots_exist(normalized_root)
        return self

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
