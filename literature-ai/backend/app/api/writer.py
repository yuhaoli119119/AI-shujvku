from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.rag.writer import Writer
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
    from pathlib import Path

    # 查找 env 路径
    env_paths = [
        Path(".env"),
        Path(__file__).resolve().parents[2] / ".env",  # literature-ai/.env
        Path(__file__).resolve().parents[3] / ".env",  # 根目录/.env
    ]
    env_path = None
    for p in env_paths:
        if p.exists():
            env_path = p
            break
    if not env_path:
        env_path = Path(".env")

    lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    kv_to_update = {
        "LITAI_WRITER_BACKEND": writer_backend,
        "LITAI_WRITER_MODEL": writer_model,
        "LITAI_WRITER_API_BASE": writer_api_base or "",
        "LITAI_WRITER_API_KEY": writer_api_key or "",
        "LITAI_WRITER_FALLBACK_BACKEND": writer_fallback_backend,
    }

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            key = key.strip()
            if key in kv_to_update:
                new_lines.append(f"{key}={kv_to_update[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    for k, v in kv_to_update.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


@router.get("/status", response_model=WriterStatusResponse)
async def writer_status(
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> WriterStatusResponse:
    result = Writer(session, settings=settings).status()
    return WriterStatusResponse(**result)


@router.post("/draft", response_model=RAGWriteResponse)
async def draft_paper_sections(
    payload: RAGWriteRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> RAGWriteResponse:
    result = Writer(session, settings=settings).write(
        topic=payload.topic,
        paper_ids=payload.paper_ids or None,
        user_notes=payload.user_notes,
        sections=payload.sections,
        limit_per_type=payload.limit_per_type,
        target_paper_type=payload.target_paper_type,
    )
    return RAGWriteResponse(**result)


@router.get("/settings", response_model=WriterSettingsResponse)
async def get_writer_settings(
    settings: Settings = Depends(get_settings),
) -> WriterSettingsResponse:
    # 遮蔽 API KEY 保护安全
    masked_key = "******" if settings.writer_api_key else None
    return WriterSettingsResponse(
        writer_backend=settings.writer_backend,
        writer_model=settings.writer_model,
        writer_api_base=settings.writer_api_base,
        writer_api_key=masked_key,
        writer_fallback_backend=settings.writer_fallback_backend,
    )


@router.post("/settings", response_model=WriterSettingsResponse)
async def update_writer_settings(
    payload: WriterSettingsUpdateRequest,
    settings: Settings = Depends(get_settings),
) -> WriterSettingsResponse:
    # 1. 内存中即时生效更新
    settings.writer_backend = payload.writer_backend.strip()
    settings.writer_model = payload.writer_model.strip()

    api_base = payload.writer_api_base.strip() if payload.writer_api_base else None
    settings.writer_api_base = api_base or None

    # 特殊处理 api_key：如果是 ******，说明用户没有修改它，使用旧的值
    if payload.writer_api_key and payload.writer_api_key.strip() == "******":
        # 保持不变
        pass
    else:
        api_key = payload.writer_api_key.strip() if payload.writer_api_key else None
        settings.writer_api_key = api_key or None

    settings.writer_fallback_backend = payload.writer_fallback_backend.strip()

    # 2. 写入 .env 文件以持久化
    try:
        update_env_file(
            writer_backend=settings.writer_backend,
            writer_model=settings.writer_model,
            writer_api_base=settings.writer_api_base,
            writer_api_key=settings.writer_api_key,
            writer_fallback_backend=settings.writer_fallback_backend,
        )
    except Exception as e:
        print(f"Failed to persistently write to .env file: {e}")

    # 3. 清理 get_settings.cache_clear()，防止后续依赖缓存旧的 Settings 导致数据不同步
    try:
        get_settings.cache_clear()
    except Exception:
        pass

    masked_key = "******" if settings.writer_api_key else None
    return WriterSettingsResponse(
        writer_backend=settings.writer_backend,
        writer_model=settings.writer_model,
        writer_api_base=settings.writer_api_base,
        writer_api_key=masked_key,
        writer_fallback_backend=settings.writer_fallback_backend,
    )

