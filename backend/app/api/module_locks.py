from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.schemas.module_locks import (
    ModuleWriteLockAcquireRequest,
    ModuleWriteLockReleaseRequest,
    ModuleWriteLockResponse,
    ModuleWriteLockValidateRequest,
    ModuleWriteLockValidateResponse,
)
from app.services.module_write_lock_service import ModuleWriteLockService

router = APIRouter()


@router.post("/acquire", response_model=ModuleWriteLockResponse)
async def acquire_module_write_lock(
    payload: ModuleWriteLockAcquireRequest,
    session: Session = Depends(get_db_session),
) -> ModuleWriteLockResponse:
    try:
        lock = ModuleWriteLockService(session).acquire(
            paper_id=payload.paper_id,
            module_name=payload.module_name,
            locked_by=payload.locked_by,
            ttl_minutes=payload.ttl_minutes,
            meta=payload.metadata,
        )
        session.commit()
        session.refresh(lock)
        return ModuleWriteLockResponse.model_validate(lock)
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        status_code = 409 if str(exc).startswith("module_write_lock_conflict") else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/release", response_model=ModuleWriteLockResponse)
async def release_module_write_lock(
    payload: ModuleWriteLockReleaseRequest,
    session: Session = Depends(get_db_session),
) -> ModuleWriteLockResponse:
    try:
        lock = ModuleWriteLockService(session).release(
            lock_token=payload.lock_token,
            released_by=payload.released_by,
        )
        session.commit()
        session.refresh(lock)
        return ModuleWriteLockResponse.model_validate(lock)
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/validate", response_model=ModuleWriteLockValidateResponse)
async def validate_module_write_lock(
    payload: ModuleWriteLockValidateRequest,
    session: Session = Depends(get_db_session),
) -> ModuleWriteLockValidateResponse:
    try:
        check = ModuleWriteLockService(session).validate_write(
            paper_id=payload.paper_id,
            module_names=payload.module_names,
            lock_tokens=payload.lock_tokens,
            locked_by=payload.locked_by,
        )
        session.commit()
        return ModuleWriteLockValidateResponse(**check.__dict__)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=list[ModuleWriteLockResponse])
async def list_module_write_locks(
    paper_id: UUID | None = Query(default=None),
    status: str | None = Query(default="active"),
    session: Session = Depends(get_db_session),
) -> list[ModuleWriteLockResponse]:
    locks = ModuleWriteLockService(session).list_locks(paper_id=paper_id, status=status)
    session.commit()
    return [ModuleWriteLockResponse.model_validate(lock) for lock in locks]
