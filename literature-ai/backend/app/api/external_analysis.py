from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.settings import sync_writer_settings_from_session
from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.schemas.api import InternalAIParseRequest, InternalAIParseResponse
from app.schemas.external_analysis import (
    ExternalAnalysisCandidateResponse,
    ExternalAnalysisImportRequest,
    ExternalAnalysisMaterializeRequest,
    ExternalAnalysisRunResponse,
)
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.external_analysis_service import ExternalAnalysisNormalizedModel
from app.services.external_analysis_service import (
    _truncate,
    build_internal_ai_review_blob as _build_internal_ai_review_blob,
    sanitize_internal_corrections as _sanitize_internal_corrections,
)
from app.services.paper_query import PaperQueryService
from app.services.verification_session_service import VerificationSessionService

router = APIRouter()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _serialize_run(service: ExternalAnalysisService, run) -> ExternalAnalysisRunResponse:
    base = ExternalAnalysisRunResponse.model_validate(run).model_dump(exclude={"candidates"})
    candidates = service.list_candidates(run.id)
    return ExternalAnalysisRunResponse(
        **{**base, "created_at": _as_utc(run.created_at)},
        candidates=[
            ExternalAnalysisCandidateResponse.model_validate(item).model_copy(
                update={"created_at": _as_utc(item.created_at)}
            )
            for item in candidates
        ],
    )


@router.post("/import", response_model=ExternalAnalysisRunResponse)
async def import_external_analysis(
    payload: ExternalAnalysisImportRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ExternalAnalysisRunResponse:
    try:
        service = ExternalAnalysisService(session=session, settings=settings)
        run = service.import_run(
            paper_id=payload.paper_id,
            source=payload.source,
            source_label=payload.source_label,
            raw_text=payload.raw_text,
            raw_payload=payload.raw_payload,
        )
        if payload.auto_apply_review_rules:
            VerificationSessionService(session, settings).apply_import_rules_for_paper(
                paper_id=payload.paper_id,
                reviewer=payload.reviewer,
            )
        session.commit()
        return _serialize_run(service, run)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs", response_model=list[ExternalAnalysisRunResponse])
async def list_external_analysis_runs(
    paper_id: UUID | None = None,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> list[ExternalAnalysisRunResponse]:
    service = ExternalAnalysisService(session=session, settings=settings)
    runs = service.list_runs(paper_id=paper_id)
    return [_serialize_run(service, run) for run in runs]


@router.get("/runs/{run_id}", response_model=ExternalAnalysisRunResponse)
async def get_external_analysis_run(
    run_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ExternalAnalysisRunResponse:
    service = ExternalAnalysisService(session=session, settings=settings)
    try:
        run = service.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_run(service, run)


@router.delete("/runs/{run_id}")
async def delete_external_analysis_run(
    run_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    service = ExternalAnalysisService(session=session, settings=settings)
    try:
        run = service.delete_run(run_id)
        session.commit()
        return {"deleted": True, "run_id": str(run.id)}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/delete")
async def delete_external_analysis_run_via_post(
    run_id: UUID,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    return await delete_external_analysis_run(run_id, session, settings)


@router.post("/runs/{run_id}/materialize")
async def materialize_external_analysis_run(
    run_id: UUID,
    payload: ExternalAnalysisMaterializeRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    service = ExternalAnalysisService(session=session, settings=settings)
    try:
        result = service.materialize_candidates(
            run_id=run_id,
            candidate_ids=payload.candidate_ids,
            explicit_all=payload.explicit_all,
            created_by=payload.created_by,
        )
        session.commit()
        return {
            "run_id": str(run_id),
            "created_notes": result.created_notes,
            "created_corrections": result.created_corrections,
            "created_relationships": result.created_relationships,
            "skipped_candidates": result.skipped_candidates,
        }
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/papers/{paper_id}/internal-parse", response_model=InternalAIParseResponse)
def internal_ai_parse_paper(
    paper_id: UUID,
    payload: InternalAIParseRequest,
    session: Session = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> InternalAIParseResponse:
    sync_writer_settings_from_session(session, settings)
    service = ExternalAnalysisService(session=session, settings=settings)
    detail = PaperQueryService(session).get_paper_detail(paper_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not service.llm.is_configured():
        raise HTTPException(status_code=400, detail="Internal AI is not configured")

    review_blob = _build_internal_ai_review_blob(detail)
    system_prompt = (
        "You are an internal scientific curation agent for a literature database. "
        "Review the provided parsed-paper bundle and produce a detailed, human-readable scientific review in review_notes. "
        "Always include several substantive review_notes when the bundle contains enough text: main contribution, methods/materials, "
        "DFT or electrochemical evidence when present, mechanism logic, figure/table extraction quality, extraction gaps, and data-quality risks. "
        "Write review_notes in clear Chinese unless the source field itself must be quoted. "
        "If figure crops look like publisher logos, CrossMark badges, headers, or other decorative/non-scientific snippets, flag them as extraction noise. "
        "Do not treat noisy figure crops as scientific figures; instead explain that the PDF page or caption must be checked. "
        "For catalyst support, synthesis method, coordination, DFT settings, energy values, and writing cards, prefer concrete quoted evidence. "
        "Use correction_proposals only for concrete field fixes, "
        "and supporting_papers only when an existing linked paper can be inferred from DOI/title clues already present. "
        "Do not invent evidence, identifiers, values, or target paths. If evidence is incomplete, explain the gap in review_notes instead of guessing. "
        "For top-level paper fields, only use these correction field_name values: doi, title, year, journal, authors, abstract, oa_status, license. "
        "For those top-level fields, set target_path exactly equal to field_name. "
        "For structured corrections, only use field_name values from dft_results, mechanism_claims, electrochemical_performance, catalyst_samples, dft_settings, writing_cards, "
        "and set target_path strictly as <collection>:<row_id>:<field> using row ids that already exist in the provided bundle."
    )
    user_prompt = (
        "Analyze this parsed literature record and extraction output. "
        "First create detailed review_notes that a researcher can read directly. Then identify any clear normalization corrections "
        "and supporting-paper relationships that are safe to keep as candidates. For every note or correction, include page, section_title, "
        "quoted_text, confidence, and mapping_reason when the bundle provides enough evidence.\n\n"
        f"{review_blob}"
    )

    normalized = service.llm.structured_extract(system_prompt, user_prompt, ExternalAnalysisNormalizedModel)
    if normalized is None:
        raise HTTPException(status_code=502, detail="Internal AI failed to produce structured output")
    normalized = _sanitize_internal_corrections(normalized)

    service.delete_runs_for_paper_source(paper_id, "internal_ai")
    run = service.import_run(
        paper_id=paper_id,
        source="internal_ai",
        source_label=payload.source_label,
        raw_text=None,
        raw_payload=normalized.model_dump(mode="json"),
    )
    created_notes = 0
    created_corrections = 0
    created_relationships = 0
    skipped_candidates = 0
    auto_applied_corrections = 0

    if payload.auto_apply:
        materialized = service.materialize_candidates(
            run_id=run.id,
            candidate_ids=None,
            explicit_all=True,
            created_by="internal_ai",
        )
        created_notes = materialized.created_notes
        created_corrections = materialized.created_corrections
        created_relationships = materialized.created_relationships
        skipped_candidates = materialized.skipped_candidates
        # D2-1 safety boundary: internal AI may suggest and materialize pending
        # corrections, but only the human correction/review routes may approve or
        # verify them. Keep auto_applied_corrections at 0 for compatibility.

    session.commit()
    return InternalAIParseResponse(
        run_id=run.id,
        mapping_status=run.mapping_status,
        created_notes=created_notes,
        created_corrections=created_corrections,
        created_relationships=created_relationships,
        auto_applied_corrections=auto_applied_corrections,
        skipped_candidates=skipped_candidates,
        llm_status="ok",
        llm_error=None,
    )
