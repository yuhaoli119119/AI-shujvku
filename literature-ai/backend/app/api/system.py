from fastapi import APIRouter

from app.mcp.auth import parse_mcp_api_keys, validate_mcp_capability_assignments


from app.services.website_contract import website_connection_contract
from fastapi import Request

router = APIRouter()


@router.get("/db-info")
async def get_db_info() -> dict:
    from app.config import get_settings
    from app.utils.active_database import get_active_database_info

    settings = get_settings()
    info = get_active_database_info()
    configured_db_papers_total = info.get("configured_db_papers_total")
    effective_db_papers_total = info.get("effective_db_papers_total")
    if info.get("db_kind") == "postgresql":
        try:
            from sqlalchemy import text

            from app.db.session import get_engine

            with get_engine(settings.database_url).connect() as connection:
                configured_db_papers_total = int(
                    connection.execute(text("SELECT COUNT(*) FROM papers")).scalar() or 0
                )
                if info.get("active_library"):
                    effective_db_papers_total = int(
                        connection.execute(
                            text("SELECT COUNT(*) FROM papers WHERE library_name = :library_name"),
                            {"library_name": info["active_library"]},
                        ).scalar()
                        or 0
                    )
                else:
                    effective_db_papers_total = configured_db_papers_total
        except Exception:
            pass

    return {
        "database_url_masked": info["db_url_masked"],
        "dialect": info["db_kind"],
        "storage_root": str(settings.storage_root),
        "active_library": info["active_library"],
        "active_library_root": info.get("active_library_root"),
        "papers_total": effective_db_papers_total,
        "configured_db_papers_total": configured_db_papers_total,
    }


@router.get("/agent-guide")
async def get_agent_guide(request: Request) -> dict:
    from app.api.settings import _advertised_base_url
    base_url = _advertised_base_url(request, fallback_host=request.url.hostname or "localhost", fallback_port=8000)
    return {"system_name":"Literature AI", "positioning":"服务器 PostgreSQL 是文献与来源化数据真源。",
            **website_connection_contract(request, base_url),
            "http_endpoints":[{"method":"GET","path":p} for p in ("/api/papers", "/api/rebuild/rows", "/api/rebuild/templates")]}
