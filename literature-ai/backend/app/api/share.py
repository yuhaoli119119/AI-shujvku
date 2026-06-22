"""Read-only share API — all endpoints are GET-only, token-scoped, and strictly read.

This project uses PostgreSQL with pgvector.
"""
import mimetypes
import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    AuditLog,
    DFTResult,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperNote,
    ShareToken,
    utcnow,
)
from app.db.session import get_db_session
from app.utils.artifact_paths import resolve_persisted_artifact_path

router = APIRouter(prefix="/share", tags=["Share"])

# ---------------------------------------------------------------------------
# Token verification & scope helpers
# ---------------------------------------------------------------------------

def verify_share_token(share_token: str, session: Session = Depends(get_db_session)) -> ShareToken:
    token_record = session.scalar(
        select(ShareToken).where(ShareToken.token == share_token)
    )
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid share token")
    if token_record.expires_at and token_record.expires_at < utcnow():
        raise HTTPException(status_code=401, detail="Share token expired")
    return token_record


def _library_scope_name(scope: str) -> str | None:
    if not scope.startswith("library:"):
        return None
    library_name = scope.split(":", 1)[1].strip()
    return library_name or None


def _check_scope(token_record: ShareToken, paper_id: str, session: Session):
    if token_record.scope == "all":
        return
    if token_record.scope == f"paper:{paper_id}":
        return
    library_name = _library_scope_name(token_record.scope)
    if library_name:
        accessible = session.scalar(
            select(Paper.id)
            .where(Paper.id == _paper_uuid(paper_id))
            .where(Paper.library_name == library_name)
            .limit(1)
        )
        if accessible:
            return
    raise HTTPException(status_code=403, detail="Token does not have access to this paper")


def _page_limit(requested: int) -> int:
    return min(requested, max(1, min(get_settings().share_max_page_size, 100)))


def _artifact_basename(value: str | None) -> str:
    parts = re.split(r"[/\\]+", str(value or "").strip())
    return parts[-1] if parts else ""


def _paper_uuid(paper_id: str) -> UUID:
    try:
        return UUID(paper_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid paper id") from exc


# ---------------------------------------------------------------------------
# Path-traversal guard
# ---------------------------------------------------------------------------

_DANGEROUS_PATH_RE = re.compile(r"(\.\.|[/\\])")  # reject .. and any slash


def _safe_filename(filename: str) -> str:
    """Reject filenames that could cause path traversal.

    Only bare filenames like ``abc123.png`` are allowed — no directories,
    no ``..``, no leading dots.
    """
    if _DANGEROUS_PATH_RE.search(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return filename


# ---------------------------------------------------------------------------
# GET /{token}/papers — list accessible papers
# ---------------------------------------------------------------------------

@router.get("/{share_token}/papers")
def list_papers(
    share_token: str,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
):
    token_record = verify_share_token(share_token, session)
    query = select(Paper)
    if token_record.scope == "all":
        pass
    elif token_record.scope.startswith("paper:"):
        pid = token_record.scope.split(":", 1)[1]
        query = query.where(Paper.id == _paper_uuid(pid))
    elif library_name := _library_scope_name(token_record.scope):
        query = query.where(Paper.library_name == library_name)
    else:
        raise HTTPException(status_code=403, detail="Invalid share token scope")

    page_limit = _page_limit(limit)
    papers = session.scalars(query.order_by(Paper.created_at.desc()).offset(offset).limit(page_limit)).all()
    return {
        "limit": page_limit,
        "offset": offset,
        "items": [
            {
                "id": str(p.id),
                "title": p.title,
                "authors": p.authors,
                "year": p.year,
                "journal": p.journal,
                "doi": p.doi if hasattr(p, "doi") else None,
            }
            for p in papers
        ]
    }


# ---------------------------------------------------------------------------
# GET /{token}/papers/{paper_id} — paper detail
# ---------------------------------------------------------------------------

@router.get("/{share_token}/papers/{paper_id}")
def get_paper(share_token: str, paper_id: str, session: Session = Depends(get_db_session)):
    token_record = verify_share_token(share_token, session)
    _check_scope(token_record, paper_id, session)
    paper = session.get(Paper, _paper_uuid(paper_id))
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return {
        "id": str(paper.id),
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors,
        "year": paper.year,
        "journal": paper.journal,
        "doi": paper.doi,
    }


# ---------------------------------------------------------------------------
# GET /{token}/figures/{paper_id} — list figures for a paper
# ---------------------------------------------------------------------------

@router.get("/{share_token}/figures/{paper_id}")
def list_figures(
    share_token: str,
    paper_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
):
    token_record = verify_share_token(share_token, session)
    _check_scope(token_record, paper_id, session)
    figures = session.scalars(
        select(PaperFigure)
        .where(PaperFigure.paper_id == _paper_uuid(paper_id))
        .order_by(PaperFigure.page, PaperFigure.figure_label)
        .offset(offset)
        .limit(_page_limit(limit))
    ).all()
    return {
        "items": [
            {
                "id": str(f.id),
                "figure_label": f.figure_label,
                "caption": f.caption,
                "page": f.page,
                "figure_role": f.figure_role,
                "content_summary": f.content_summary,
                "crop_status": f.crop_status,
                "image_path": _artifact_basename(f.image_path),
            }
            for f in figures
        ]
    }


# ---------------------------------------------------------------------------
# GET /{token}/figures/{paper_id}/{filename} — serve figure image (guarded)
# ---------------------------------------------------------------------------

@router.get("/{share_token}/figures/{paper_id}/{filename}")
def get_figure_image(
    share_token: str,
    paper_id: str,
    filename: str,
    session: Session = Depends(get_db_session),
):
    token_record = verify_share_token(share_token, session)
    _check_scope(token_record, paper_id, session)
    safe_name = _safe_filename(filename)

    image_paths = session.scalars(
        select(PaperFigure.image_path)
        .where(PaperFigure.paper_id == _paper_uuid(paper_id))
        .limit(1000)
    ).all()
    stored_path = next(
        (path for path in image_paths if _artifact_basename(path) == safe_name),
        None,
    )
    if not stored_path:
        raise HTTPException(status_code=404, detail="Figure not found in database")
    file_path = resolve_persisted_artifact_path(
        stored_path,
        category="figures",
        settings=get_settings(),
        must_exist=True,
    )
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Figure file not found on disk")

    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(file_path, media_type=media_type or "image/png")


# ---------------------------------------------------------------------------
# GET /{token}/notes/{paper_id} — AI analysis notes (Blackboard "雁过留声")
# ---------------------------------------------------------------------------

@router.get("/{share_token}/notes/{paper_id}")
def list_notes(
    share_token: str,
    paper_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
):
    token_record = verify_share_token(share_token, session)
    _check_scope(token_record, paper_id, session)
    notes = session.scalars(
        select(PaperNote)
        .where(PaperNote.paper_id == _paper_uuid(paper_id))
        .order_by(PaperNote.created_at.desc())
        .offset(offset)
        .limit(_page_limit(limit))
    ).all()
    return {
        "items": [
            {
                "id": str(n.id),
                "source": n.source,
                "content": n.content,
                "field_name": n.field_name,
                "page": n.page,
                "section_title": n.section_title,
                "quoted_text": n.quoted_text,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notes
        ]
    }


# ---------------------------------------------------------------------------
# GET /{token}/corrections/{paper_id} — review corrections
# ---------------------------------------------------------------------------

@router.get("/{share_token}/corrections/{paper_id}")
def list_corrections(
    share_token: str,
    paper_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
):
    token_record = verify_share_token(share_token, session)
    _check_scope(token_record, paper_id, session)
    corrections = session.scalars(
        select(PaperCorrection)
        .where(PaperCorrection.paper_id == _paper_uuid(paper_id))
        .order_by(PaperCorrection.created_at.desc())
        .offset(offset)
        .limit(_page_limit(limit))
    ).all()
    return {
        "items": [
            {
                "id": str(c.id),
                "source": c.source,
                "field_name": c.field_name,
                "proposed_value": c.proposed_value,
                "status": c.status,
                "review_comment": c.review_comment,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in corrections
        ]
    }


# ---------------------------------------------------------------------------
# GET /{token}/dft/{paper_id} — DFT results
# ---------------------------------------------------------------------------

@router.get("/{share_token}/dft/{paper_id}")
def get_dft_results(
    share_token: str,
    paper_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
):
    token_record = verify_share_token(share_token, session)
    _check_scope(token_record, paper_id, session)
    results = session.scalars(
        select(DFTResult)
        .where(DFTResult.paper_id == _paper_uuid(paper_id))
        .order_by(DFTResult.id)
        .offset(offset)
        .limit(_page_limit(limit))
    ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "catalyst_sample_id": str(r.catalyst_sample_id) if r.catalyst_sample_id else None,
                "adsorbate": r.adsorbate,
                "property_type": r.property_type,
                "value": r.value,
                "unit": r.unit,
                "reaction_step": r.reaction_step,
                "source_section": r.source_section,
                "source_figure": r.source_figure,
                "evidence_text": r.evidence_text,
                "confidence": r.confidence,
                "candidate_status": r.candidate_status,
            }
            for r in results
        ]
    }


# ---------------------------------------------------------------------------
# GET /{token}/audit/{paper_id} — audit logs
# ---------------------------------------------------------------------------

@router.get("/{share_token}/audit/{paper_id}")
def get_audit_logs(
    share_token: str,
    paper_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
):
    token_record = verify_share_token(share_token, session)
    _check_scope(token_record, paper_id, session)
    logs = session.scalars(
        select(AuditLog)
        .where(AuditLog.paper_id == _paper_uuid(paper_id))
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(_page_limit(limit))
    ).all()
    return {
        "items": [
            {
                "id": str(l.id),
                "action": l.action,
                "source": l.source,
                "target_type": l.target_type,
                "target_id": l.target_id,
                "payload": l.payload,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ]
    }
