from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    from app.utils.active_database import get_active_database_info

    info = get_active_database_info()
    return {
        "status": "ok",
        "db_kind": info["db_kind"],
        "db_url_masked": info["db_url_masked"],
        "active_library": info["active_library"],
        "active_library_root": info.get("active_library_root"),
        "storage_root": info.get("storage_root"),
        "papers_total": info.get("papers_total"),
    }
