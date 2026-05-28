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
from app.utils.project_paths import default_library_root

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
    if expected_total is None and _is_workspace_default_database(active_db_info.get("db_path")):
        expected_total = 15
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
        "db_path": active_db_info.get("db_path"),
        "active_library": active_db_info.get("active_library"),
        "matches_active_library_db_path": active_db_info.get("matches_active_library_db_path"),
        "effective_matches_active_library_db_path": active_db_info.get("effective_matches_active_library_db_path"),
        "workspace_default_database": _is_workspace_default_database(active_db_info.get("db_path")),
        "expected_papers_total": expected_total,
    }
    return result


def _is_workspace_default_database(db_path: object) -> bool:
    if not isinstance(db_path, str) or not db_path:
        return False
    expected = (default_library_root() / "database.sqlite").resolve()
    try:
        from pathlib import Path

        return Path(db_path).resolve() == expected
    except OSError:
        return False
