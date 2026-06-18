from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.schemas.api import (
    RAGWriteRequest,
    RAGWriteResponse,
    WriterStatusResponse,
    WriterSettingsResponse,
    WriterSettingsUpdateRequest,
)

router = APIRouter()


def update_env_file(
    writer_backend: str,
    writer_model: str,
    writer_api_base: str | None,
    writer_api_key: str | None,
    writer_fallback_backend: str,
):
    return None


@router.get("/status", response_model=WriterStatusResponse)
async def writer_status(
    settings: Settings = Depends(get_settings),
) -> WriterStatusResponse:
    return WriterStatusResponse(
        backend_used="disabled",
        llm_status="disabled",
        llm_error="网页端写作模型已停用；请在 IDE / MCP AI 中完成写作整理。",
    )


@router.post("/draft", response_model=RAGWriteResponse)
async def draft_paper_sections(
    payload: RAGWriteRequest,
    settings: Settings = Depends(get_settings),
) -> RAGWriteResponse:
    raise HTTPException(
        status_code=410,
        detail="网页端写作模型已停用；请在 IDE / MCP AI 中读取证据并完成写作整理。",
    )


@router.get("/settings", response_model=WriterSettingsResponse)
async def get_writer_settings(
    settings: Settings = Depends(get_settings),
) -> WriterSettingsResponse:
    return WriterSettingsResponse(
        writer_backend="disabled",
        writer_model="IDE/MCP AI",
        writer_api_base=None,
        writer_api_key=None,
        writer_fallback_backend="disabled",
    )


@router.post("/settings", response_model=WriterSettingsResponse)
async def update_writer_settings(
    payload: WriterSettingsUpdateRequest,
    settings: Settings = Depends(get_settings),
) -> WriterSettingsResponse:
    return WriterSettingsResponse(
        writer_backend="disabled",
        writer_model="IDE/MCP AI",
        writer_api_base=None,
        writer_api_key=None,
        writer_fallback_backend="disabled",
    )

