from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings


def require_exports_enabled(settings: Settings | None = None) -> None:
    if not (settings or get_settings()).exports_enabled:
        raise HTTPException(status_code=403, detail="Exports are disabled by server policy")


def require_mcp_exports_enabled(settings: Settings | None = None) -> None:
    if not (settings or get_settings()).exports_enabled:
        raise PermissionError("Exports are disabled by server policy")


def _is_export_path(path: str) -> bool:
    return (
        path.startswith("/api/papers/export/")
        or path == "/api/writing/export"
        or path.startswith("/api/writing/word/")
    )


async def enforce_export_boundary(request: Request, call_next):
    if _is_export_path(request.url.path) and not get_settings().exports_enabled:
        return JSONResponse(
            {"detail": "Exports are disabled by server policy"},
            status_code=403,
        )
    return await call_next(request)
