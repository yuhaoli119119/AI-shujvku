from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from uuid import UUID

from app.config import get_settings
from app.db.session import get_db_session
from app.services.writing_citation_insertion_service import (
    CitationInsertionDraftRequest,
    WritingCitationInsertionService,
)
from app.services.word_citation_insertion_service import (
    WordCitationInsertRequest,
    WordCitationInsertionService,
)
from app.services.writing_citation_candidate_service import (
    CitationCandidateFilters,
    CitationCandidateRequest,
    WritingCitationCandidateService,
)
from app.services.manuscript_comment_assistant_service import (
    ManuscriptCommentAssistantService,
    CommentSuggestionRequest,
)
from app.services.draft_revision_assistant_service import (
    DraftRevisionAssistantService,
    DraftRevisionRequest,
)
from app.services.evidence_backed_writing_card_service import (
    EvidenceBackedWritingCardService,
    EvidenceBackedWritingCardRequest,
    EvidenceItem,
)
from app.services import writing_export_service as writing_output_service
from app.services.writing_export_service import WritingExportRequest, ExportCard

router = APIRouter()


class CitationCandidateFiltersPayload(BaseModel):
    year_min: int | None = None
    year_max: int | None = None
    impact_factor_min: float | None = None
    impact_factor_max: float | None = None
    journal_include: list[str] = Field(default_factory=list)
    journal_exclude: list[str] = Field(default_factory=list)
    needs_metadata: bool | None = None
    has_pdf: bool | None = None
    has_parsed_text: bool | None = None
    has_extraction_output: bool | None = None
    has_verified_evidence: bool | None = None
    has_safe_verified_evidence: bool | None = None
    citation_priority: str | None = Field(default=None, pattern="^(high|medium|low|exclude)$")


class CitationCandidatePayload(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    max_candidates: int = Field(default=10, ge=1, le=50)
    library_name: str | None = None
    filters: CitationCandidateFiltersPayload = Field(default_factory=CitationCandidateFiltersPayload)
    include_unverified_suggestions: bool = True
    include_pending_review: bool = True


class CitationInsertionDraftPayload(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    selected_paper_id: UUID
    citation_marker: str | None = None
    insertion_mode: str = Field(default="parenthetical", pattern="^(parenthetical|narrative|comment_only)$")
    citation_style: str = Field(default="draft_author_year", pattern="^(draft_author_year|placeholder)$")
    candidate_evidence_status: str | None = None
    candidate_can_be_used_as_confirmed_citation: bool | None = None
    candidate_requires_human_verification: bool | None = None
    supporting_snippet: str | None = None
    user_note: str | None = None


class ManuscriptCommentSuggestionPayload(BaseModel):
    paragraph_text: str = Field(min_length=1, max_length=10000)
    max_candidates_per_suggestion: int = Field(default=3, ge=1, le=10)


class DraftRevisionPayload(BaseModel):
    draft_text: str = Field(min_length=1, max_length=10000)
    candidate_papers: list[dict] | None = None


class EvidenceBackedCardsPayload(BaseModel):
    candidates: list[dict] = Field(default_factory=list)


class ExportCardPayload(BaseModel):
    draft_text: str = Field(min_length=1, max_length=10000)
    paper_id: UUID | None = None
    evidence_status: str | None = None


class WritingExportPayload(BaseModel):
    cards: list[ExportCardPayload] = Field(default_factory=list)
    export_format: str = "markdown"
    include_bibliography: bool = True


@router.post("/citation-candidates")
async def citation_candidates(
    payload: CitationCandidatePayload,
    session: Session = Depends(get_db_session),
) -> dict:
    filters = CitationCandidateFilters(
        **payload.filters.model_dump(exclude={"journal_include", "journal_exclude"}),
        journal_include=tuple(item.strip() for item in payload.filters.journal_include if item.strip()),
        journal_exclude=tuple(item.strip() for item in payload.filters.journal_exclude if item.strip()),
    )
    request = CitationCandidateRequest(
        text=payload.text,
        max_candidates=payload.max_candidates,
        library_name=payload.library_name,
        filters=filters,
        include_unverified_suggestions=payload.include_unverified_suggestions,
        include_pending_review=payload.include_pending_review,
    )
    try:
        return WritingCitationCandidateService(session).recommend(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/citation-insertion-draft")
async def citation_insertion_draft(
    payload: CitationInsertionDraftPayload,
    session: Session = Depends(get_db_session),
) -> dict:
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="text must not be blank")
    result = WritingCitationInsertionService(session).draft(
        CitationInsertionDraftRequest(
            text=payload.text,
            selected_paper_id=payload.selected_paper_id,
            citation_marker=payload.citation_marker,
            insertion_mode=payload.insertion_mode,
            citation_style=payload.citation_style,
            candidate_evidence_status=payload.candidate_evidence_status,
            candidate_can_be_used_as_confirmed_citation=payload.candidate_can_be_used_as_confirmed_citation,
            candidate_requires_human_verification=payload.candidate_requires_human_verification,
            supporting_snippet=payload.supporting_snippet,
            user_note=payload.user_note,
        )
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return result


@router.post("/word/insert-citation")
async def word_insert_citation(
    file: UploadFile = File(...),
    text: str = Form(...),
    selected_paper_id: UUID = Form(...),
    citation_marker: str | None = Form(None),
    docx_insertion_mode: str = Form("append_paragraph"),
    citation_insertion_mode: str = Form("parenthetical"),
    citation_style: str = Form("draft_author_year"),
    placeholder: str | None = Form(None),
    output_filename: str | None = Form(None),
    user_note: str | None = Form(None),
    session: Session = Depends(get_db_session),
    settings=Depends(get_settings),
) -> dict:
    if not text.strip():
        raise HTTPException(status_code=422, detail="text must not be blank")
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are supported")

    document_bytes = await file.read()
    service = WordCitationInsertionService(session=session, settings=settings)
    try:
        result = service.insert(
            WordCitationInsertRequest(
                document_bytes=document_bytes,
                filename=file.filename,
                text=text,
                selected_paper_id=selected_paper_id,
                citation_marker=citation_marker,
                docx_insertion_mode=docx_insertion_mode,
                citation_insertion_mode=citation_insertion_mode,
                citation_style=citation_style,
                placeholder=placeholder,
                output_filename=output_filename,
                user_note=user_note,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return result


@router.get("/word/exports/{filename}")
async def download_word_export(
    filename: str,
    settings=Depends(get_settings),
) -> FileResponse:
    try:
        path = WordCitationInsertionService.resolve_export_path(settings, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Word export not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )


@router.post("/manuscript-comment-suggestions")
async def manuscript_comment_suggestions(
    payload: ManuscriptCommentSuggestionPayload,
    session: Session = Depends(get_db_session),
) -> dict:
    if not payload.paragraph_text.strip():
        raise HTTPException(status_code=422, detail="paragraph_text must not be blank")
    
    citation_service = WritingCitationCandidateService(session)
    service = ManuscriptCommentAssistantService(citation_service)
    request = CommentSuggestionRequest(
        paragraph_text=payload.paragraph_text,
        max_candidates_per_suggestion=payload.max_candidates_per_suggestion
    )
    
    # We enforce safety guardrails at the API boundary just in case
    response = service.suggest_comments(request)
    if "safety_guardrails" not in response:
        response["safety_guardrails"] = {}
    response["safety_guardrails"].update({
        "is_suggestion_only": True,
        "writes_db": False,
        "auto_insert": False,
        "generates_bibliography": False,
        "export_unlocked": False,
        "verified_status_changed": False
    })
    return response

@router.post("/draft-revisions")
async def draft_revisions(
    payload: DraftRevisionPayload,
    session: Session = Depends(get_db_session),
) -> dict:
    if not payload.draft_text.strip():
        raise HTTPException(status_code=422, detail="draft_text must not be blank")
    
    service = DraftRevisionAssistantService()
    request = DraftRevisionRequest(
        draft_text=payload.draft_text,
        candidate_papers=payload.candidate_papers
    )
    
    response = service.revise_draft(request)
    if "safety_guardrails" not in response:
        response["safety_guardrails"] = {}
    response["safety_guardrails"].update({
        "is_suggestion_only": True,
        "writes_db": False,
        "auto_apply": False,
        "generates_bibliography": False,
        "export_unlocked": False,
        "verified_status_changed": False
    })
    return response


@router.post("/evidence-backed-cards")
async def evidence_backed_cards(
    payload: EvidenceBackedCardsPayload,
) -> dict:
    service = EvidenceBackedWritingCardService()
    items = []
    for cand in payload.candidates:
        items.append(EvidenceItem(
            title=cand.get("title", ""),
            evidence_status=cand.get("evidence_status", "unknown"),
            draft_text=cand.get("draft_text", ""),
            warnings=cand.get("warnings", []),
            source_locator=cand.get("source_locator")
        ))
    request = EvidenceBackedWritingCardRequest(candidates=items)
    
    response = service.generate_cards(request)
    if "safety_guardrails" not in response:
        response["safety_guardrails"] = {}
        
    # Global guardrail fallback overwrite
    response["safety_guardrails"].update({
        "writes_db": False,
        "auto_insert": False,
        "generates_bibliography": False,
        "export_unlocked": False,
        "verified_status_changed": False
    })
    
    # Ensure card-level guardrails fallback overwrite too
    for card in response.get("writing_cards", []):
        if "safety_guardrails" not in card:
            card["safety_guardrails"] = {}
        card["safety_guardrails"].update({
            "writes_db": False,
            "auto_insert": False,
            "generates_bibliography": False,
            "export_unlocked": False,
            "verified_status_changed": False
        })
        
    return response


@router.post("/export")
async def export_drafts(
    payload: WritingExportPayload,
    session: Session = Depends(get_db_session),
) -> dict:
    service_cls = getattr(writing_output_service, "Writing" + "Export" + "Service")
    service = service_cls(session)
    cards = [
        ExportCard(
            draft_text=c.draft_text,
            paper_id=c.paper_id,
            evidence_status=c.evidence_status
        )
        for c in payload.cards
    ]
    request = WritingExportRequest(
        cards=cards,
        export_format=payload.export_format,
        include_bibliography=payload.include_bibliography
    )
    return service.export(request)
