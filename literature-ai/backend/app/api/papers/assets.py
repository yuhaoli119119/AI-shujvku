from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings

router = APIRouter()


@router.get("/assets/{filename}")
async def get_asset(filename: str):
    settings = get_settings()
    file_path = settings.storage_paths["figures"] / filename
    if not file_path.exists():
        file_path = settings.storage_paths["tables"] / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))
