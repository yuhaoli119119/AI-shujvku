from __future__ import annotations

import re
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings
from app.utils.artifact_paths import resolve_persisted_artifact_path

router = APIRouter()

_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _validate_asset_reference(filename: str) -> str:
    decoded = filename
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parts = [part for part in re.split(r"[\\/]+", decoded) if part]
    if (
        not decoded
        or "\x00" in decoded
        or decoded.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_RE.match(decoded)
        or any(part == ".." for part in parts)
    ):
        raise HTTPException(status_code=400, detail="Invalid asset path")
    return decoded


@router.get("/assets/{filename:path}")
async def get_asset(filename: str):
    settings = get_settings()
    filename = _validate_asset_reference(filename)
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
