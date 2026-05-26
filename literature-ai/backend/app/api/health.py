import logging

from fastapi import APIRouter

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health() -> dict:
    from app.config import get_settings
    from app.services.library_manager import LibraryManager

    settings = get_settings()
    url = settings.database_url
    # Determine dialect without exposing credentials
    if url.startswith("postgresql"):
        db_kind = "postgresql"
        # Mask credentials: keep only host:port/dbname
        masked_url = url.split("@")[-1] if "@" in url else "postgresql://***"
    elif url.startswith("sqlite"):
        db_kind = "sqlite"
        # Show only filename, not full path
        path_part = url.removeprefix("sqlite:///")
        masked_url = f"sqlite:///{path_part.split('/')[-1]}"
    else:
        db_kind = "unknown"
        masked_url = "***"

    active_library: str | None = None
    active_library_path: str | None = None
    try:
        mgr = LibraryManager()
        active_lib = mgr.get_active_library()
        if active_lib:
            active_library = active_lib.name
            # Show only last two path segments
            parts = active_lib.root_path.replace("\\", "/").split("/")
            active_library_path = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    except Exception:
        pass

    return {
        "status": "ok",
        "db_kind": db_kind,
        "db_url_masked": masked_url,
        "active_library": active_library,
        "active_library_path_hint": active_library_path,
    }
