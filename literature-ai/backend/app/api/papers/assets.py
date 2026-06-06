from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings
from app.utils.artifact_paths import resolve_persisted_artifact_path

router = APIRouter()


@router.get("/assets/{filename:path}")
async def get_asset(filename: str):
    settings = get_settings()
    file_path = resolve_persisted_artifact_path(
        filename,
        category="figures",
        settings=settings,
        must_exist=True,
    )
    if file_path is None:
        file_path = resolve_persisted_artifact_path(
            filename,
            category="tables",
            settings=settings,
            must_exist=True,
        )
    if file_path is None:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(file_path))
