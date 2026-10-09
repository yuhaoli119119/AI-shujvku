from __future__ import annotations

from fastapi import APIRouter


from .aggregation import router as aggregation_router
from .assets import router as assets_router
from .classification import router as classification_router
from .common import normalize_library_name
from .corrections_public import router as corrections_public_router
from .ingestion import router as ingestion_router
from .listing import router as listing_router
from .detail import router as detail_router

router = APIRouter()

router.include_router(ingestion_router)
router.include_router(aggregation_router)
router.include_router(assets_router)
router.include_router(classification_router)
router.include_router(listing_router)
router.include_router(detail_router)
router.include_router(corrections_public_router)

__all__ = [
    "DEFAULT_LIBRARY_NAME",
    "normalize_library_name",
    "router",
]
