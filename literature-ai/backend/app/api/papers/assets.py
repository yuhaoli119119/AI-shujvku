from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings

router = APIRouter()


@router.get("/assets/{filename:path}")
async def get_asset(filename: str):
    settings = get_settings()
    candidates = [
        settings.storage_paths["figures"] / filename,
        settings.storage_paths["tables"] / filename,
    ]
    file_path = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(candidate.parents[1].resolve())
        except (OSError, ValueError, IndexError):
            continue
        if resolved.exists() and resolved.is_file():
            file_path = resolved
            break
    if file_path is None:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))
