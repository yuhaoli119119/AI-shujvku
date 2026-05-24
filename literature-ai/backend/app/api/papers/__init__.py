from __future__ import annotations

from fastapi import APIRouter

from app.services.discovery_service import DiscoveryService
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.workflow_jobs import DEFAULT_LIBRARY_NAME

from .aggregation import router as aggregation_router
from .assets import router as assets_router
from .classification import router as classification_router
from .common import normalize_library_name, rewrite_ai_search_query
from .discovery import ai_search, discovery_download_and_ingest, discovery_search
from .discovery import router as discovery_router
from .ingestion import router as ingestion_router
from .listing import router as listing_router
from .workflow import ai_workflow, get_ai_workflow_job, start_ai_workflow_job
from .workflow import router as workflow_router

router = listing_router
router.include_router(ingestion_router)
router.include_router(discovery_router)
router.include_router(aggregation_router)
router.include_router(assets_router)
router.include_router(classification_router)
router.include_router(workflow_router)

__all__ = [
    "DEFAULT_LIBRARY_NAME",
    "DiscoveryService",
    "PaperIngestionService",
    "PaperReprocessingService",
    "ai_search",
    "ai_workflow",
    "discovery_download_and_ingest",
    "discovery_search",
    "get_ai_workflow_job",
    "normalize_library_name",
    "rewrite_ai_search_query",
    "router",
    "start_ai_workflow_job",
]
