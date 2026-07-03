from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.services.dft_review_bundle_service import DFTReviewBundleService


router = APIRouter()


@router.post("/{paper_id}/dft-review-bundle")
def export_dft_review_bundle(
    paper_id: UUID,
    include_figure_files: bool = Query(default=True),
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Generate an offline review ZIP in memory and stream it to the owner client."""

    try:
        bundle = DFTReviewBundleService(session, settings).build_zip(
            paper_id,
            include_figure_files=include_figure_files,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    content = bundle["content"]
    return StreamingResponse(
        BytesIO(content),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle["filename"]}"',
            "Content-Length": str(len(content)),
            "Cache-Control": "no-store",
            "X-LitAI-Bundle-Fingerprint": bundle["manifest"]["bundle_fingerprint"],
        },
    )


@router.post("/{paper_id}/dft-review-result/validate")
def validate_dft_review_result(
    paper_id: UUID,
    payload: dict[str, Any],
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate an offline proposal without writing any database records."""

    try:
        return DFTReviewBundleService(session, settings).validate_result(paper_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
