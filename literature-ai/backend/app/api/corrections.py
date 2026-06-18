from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.mcp.auth import require_request_mcp_capability
from app.mcp.context import MCPAuthInfo
from app.schemas.mcp import MCPCorrectionDecisionRequest, MCPCorrectionDetailResponse, MCPCorrectionResponse
from app.services.review_service import ReviewService

router = APIRouter()


@router.get("", response_model=list[MCPCorrectionResponse])
async def list_corrections(
    status: str | None = "pending",
    _: MCPAuthInfo = Depends(require_request_mcp_capability("review_corrections")),
    session: Session = Depends(get_db_session),
) -> list[MCPCorrectionResponse]:
    items = ReviewService(session).list_corrections(status=status)
    return [MCPCorrectionResponse.model_validate(item) for item in items]


@router.get("/{correction_id}", response_model=MCPCorrectionDetailResponse)
async def get_correction_detail(
    correction_id: UUID,
    _: MCPAuthInfo = Depends(require_request_mcp_capability("review_corrections")),
    session: Session = Depends(get_db_session),
) -> MCPCorrectionDetailResponse:
    try:
        detail = ReviewService(session).get_correction_detail(correction_id)
        return MCPCorrectionDetailResponse(
            **MCPCorrectionResponse.model_validate(detail["correction"]).model_dump(),
            current_value=detail["current_value"],
            target_exists=detail["target_exists"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{correction_id}/approve", response_model=MCPCorrectionResponse)
async def approve_correction(
    correction_id: UUID,
    payload: MCPCorrectionDecisionRequest | None = None,
    auth: MCPAuthInfo = Depends(require_request_mcp_capability("review_corrections")),
    session: Session = Depends(get_db_session),
) -> MCPCorrectionResponse:
    try:
        write_lock_tokens = [*(payload.write_lock_tokens if payload else [])]
        if payload and payload.write_lock_token:
            write_lock_tokens.append(payload.write_lock_token)
        item = ReviewService(session).approve_correction(
            correction_id,
            reviewer=auth.source_prefix,
            write_lock_tokens=write_lock_tokens,
        )
        session.commit()
        return MCPCorrectionResponse.model_validate(item)
    except ValueError as exc:
        session.rollback()
        status_code = 409 if str(exc).startswith("module_write_lock_required") else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/{correction_id}/reject", response_model=MCPCorrectionResponse)
async def reject_correction(
    correction_id: UUID,
    payload: MCPCorrectionDecisionRequest,
    auth: MCPAuthInfo = Depends(require_request_mcp_capability("review_corrections")),
    session: Session = Depends(get_db_session),
) -> MCPCorrectionResponse:
    try:
        item = ReviewService(session).reject_correction(
            correction_id,
            reviewer=auth.source_prefix,
            reason=payload.reason,
        )
        session.commit()
        return MCPCorrectionResponse.model_validate(item)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
