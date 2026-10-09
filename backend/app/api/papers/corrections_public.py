"""Paper-level public read-only corrections endpoint.

用于对外只读站展示某篇文献的元数据更正历史（已批准/待审/已拒绝）。
纯 GET，不做任何写入。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PaperCorrection
from app.db.session import get_db_session

router = APIRouter()


@router.get("/{paper_id}/corrections")
def list_paper_corrections(
    paper_id: UUID,
    status: str | None = Query(
        default=None,
        description="按状态过滤：pending / approved / rejected / requires_resolution",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
):
    base = select(PaperCorrection).where(PaperCorrection.paper_id == paper_id)
    if status:
        base = base.where(PaperCorrection.status == status)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    items = session.scalars(
        base.order_by(PaperCorrection.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "total": total,
        "items": [
            {
                "id": str(c.id),
                "source": c.source,
                "field_name": c.field_name,
                "proposed_value": c.proposed_value,
                "status": c.status,
                "reason": c.reason,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in items
        ],
    }
