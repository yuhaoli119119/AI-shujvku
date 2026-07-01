import time
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["health"])
_HEALTH_INFO_TTL_SECONDS = 30.0
_health_info_cache: tuple[float, dict[str, Any]] | None = None


def _cached_active_database_info() -> dict[str, Any]:
    global _health_info_cache

    now = time.monotonic()
    if _health_info_cache is not None and now - _health_info_cache[0] < _HEALTH_INFO_TTL_SECONDS:
        return _health_info_cache[1]
    from app.utils.active_database import get_active_database_info

    info = get_active_database_info()
    _health_info_cache = (now, info)
    return info


@router.get("/health")
async def health() -> dict:
    info = _cached_active_database_info()
    return {
        "status": "ok",
        "db_kind": info["db_kind"],
        "db_url_masked": info["db_url_masked"],
        "active_library": info["active_library"],
        "active_library_root": info.get("active_library_root"),
        "storage_root": info.get("storage_root"),
        "papers_total": info.get("papers_total"),
    }
