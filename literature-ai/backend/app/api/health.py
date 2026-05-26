import logging

from fastapi import APIRouter

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health() -> dict:
    from pathlib import Path

    from app.utils.active_database import get_active_database_info

    info = get_active_database_info()
    active_library_path = None
    active_library_db_path = info.get("active_library_db_path")
    if active_library_db_path:
        parts = Path(active_library_db_path).parent.as_posix().split("/")
        active_library_path = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]

    return {
        "status": "ok",
        "db_kind": info["db_kind"],
        "db_url_masked": info["db_url_masked"],
        "db_path": info["db_path"],
        "active_library": info["active_library"],
        "active_library_db_path": info["active_library_db_path"],
        "active_library_path_hint": active_library_path,
        "is_active_library_sqlite": info["is_active_library_sqlite"],
        "matches_active_library_db_path": info["matches_active_library_db_path"],
        "effective_db_path": info.get("effective_db_path"),
        "effective_storage_root": info.get("effective_storage_root"),
        "effective_db_papers_total": info.get("effective_db_papers_total"),
        "effective_matches_active_library_db_path": info.get("effective_matches_active_library_db_path"),
        "recovered_from_candidate_scan": info.get("recovered_from_candidate_scan"),
    }
