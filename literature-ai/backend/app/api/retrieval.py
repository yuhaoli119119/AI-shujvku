from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.schemas.retrieval import RetrievalSearchRequest, RetrievalSearchResponse
from app.services.retrieval_service import RetrievalService

router = APIRouter()


@router.post("/search", response_model=RetrievalSearchResponse)
async def search_retrieval(
    payload: RetrievalSearchRequest,
    session: Session = Depends(get_db_session),
) -> RetrievalSearchResponse:
    return RetrievalService(session).search(payload)

