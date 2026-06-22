from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.services.impact_metadata_import_service import (
    ImpactMetadataImportService,
    parse_impact_metadata_csv,
    parse_impact_metadata_json,
)
from app.utils.active_database import get_active_database_info

router = APIRouter()


@router.post("/import")
async def import_impact_metadata(
    request: Request,
    dry_run: bool = Query(default=False),
    expected_papers_total: int | None = Query(default=None),
    session: Session = Depends(get_db_session),
) -> dict:
    content_type = request.headers.get("content-type", "").lower()
    raw_body = (await request.body()).decode("utf-8-sig")
    if "json" in content_type:
        items, invalid = parse_impact_metadata_json(raw_body)
    elif "csv" in content_type or raw_body.lstrip().lower().startswith("journal,"):
        items, invalid = parse_impact_metadata_csv(raw_body)
    else:
        raise HTTPException(status_code=415, detail="Use application/json or text/csv")

    active_db_info = get_active_database_info()
    expected_total = expected_papers_total
    service = ImpactMetadataImportService(session)
    try:
        result = service.import_items(
            items,
            dry_run=dry_run,
            expected_papers_total=expected_total,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not dry_run:
        session.commit()
    result["invalid_items"] = [item.__dict__ for item in invalid]
    result["active_database"] = {
        "db_kind": active_db_info.get("db_kind"),
        "active_library": active_db_info.get("active_library"),
        "expected_papers_total": expected_total,
    }
    return result
