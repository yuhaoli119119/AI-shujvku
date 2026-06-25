from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi.concurrency import run_in_threadpool
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import and_, func, or_, select, update

from app.config import get_settings
from app.db.models import AuditLog, DFTResult, ElectrochemicalPerformance, ExternalAnalysisCandidate, Paper, PaperCorrection, PaperFigure, PaperNote, PaperSection, PaperTable, ParseJob, ShareToken, utcnow
from app.db.session import session_scope
from app.mcp.auth import require_mcp_capability, require_mcp_capability_any
from app.rag.retriever import Retriever
from app.schemas.mcp import MCPCorrectionDetailResponse, MCPCorrectionResponse, MCPNoteResponse, MCPParseJobResponse
from app.services.discovery_service import DiscoveryService
from app.services.embedding import get_embedding_service
from app.services.codex_context_service import CodexContextService
from app.services.dft_export_service import build_dft_csv_rows, build_dft_ml_dataset
from app.services.dft_review_queue_service import DFTReviewQueueService
from app.services.dft_review_service import DFTResultReviewService
from app.services.external_analysis_service import ExternalAnalysisService
from app.services.local_pdf_service import LocalPdfService
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_knowledge_service import PaperKnowledgeService
from app.services.paper_query import PaperQueryService
from app.services.review_conflict_service import ReviewConflictAggregationService
from app.services.review_service import ReviewService
from app.services.verification_session_service import VerificationSessionService
from app.services.word_citation_insertion_service import WordCitationInsertRequest, WordCitationInsertionService
from app.security.exports import require_mcp_exports_enabled
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.figure_summary import normalize_figure_content_summary, normalize_figure_key_elements
from app.utils.library_names import DEFAULT_LIBRARY_NAME, build_library_name_clause, normalize_library_name


def _allowed_mcp_hosts() -> list[str]:
    hosts = {
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "::1",
        "[::1]",
        "[::1]:*",
    }
    for raw in (
        os.environ.get("LITAI_PUBLIC_BASE_URL"),
        os.environ.get("LITAI_MCP_PUBLIC_BASE_URL"),
    ):
        if not raw:
            continue
        parsed = urlparse(str(raw).strip())
        host = (parsed.hostname or "").strip()
        if not host:
            continue
        hosts.add(host)
        hosts.add(f"{host}:*")
    if os.environ.get("PYTEST_CURRENT_TEST"):
        hosts.add("testserver")
        hosts.add("testserver:*")
    return sorted(hosts)


mcp_server = FastMCP(
    get_settings().mcp_server_name,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_mcp_hosts(),
    ),
)
mcp_http_app = mcp_server.streamable_http_app()


def _log_action(
    *,
    action: str,
    source: str,
    paper_id: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | list[Any] | str | None = None,
) -> None:
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        session.add(
            AuditLog(
                paper_id=paper_id,
                action=action,
                source=source,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
            )
        )


def _serialize_note(note: PaperNote) -> dict[str, Any]:
    return MCPNoteResponse.model_validate(note).model_dump(mode="json")


def _serialize_correction(correction: PaperCorrection) -> dict[str, Any]:
    return MCPCorrectionResponse.model_validate(correction).model_dump(mode="json")


def _serialize_parse_job(job: ParseJob) -> dict[str, Any]:
    return MCPParseJobResponse.model_validate(job).model_dump(mode="json")


def _serialize_correction_detail(detail: dict[str, Any]) -> dict[str, Any]:
    correction = MCPCorrectionResponse.model_validate(detail["correction"]).model_dump(mode="json")
    return MCPCorrectionDetailResponse(
        **correction,
        current_value=detail["current_value"],
        target_exists=detail["target_exists"],
    ).model_dump(mode="json")


def _ensure_paper_exists(session, paper_id: UUID) -> Paper:
    paper = session.get(Paper, paper_id)
    if not paper:
        raise ValueError("Paper not found")
    return paper


@mcp_server.tool(
    name="query_papers",
    description=(
        "Query parsed papers in the local library. "
        "Supports sorting by year_serial (default), created_at (newest first), or title. "
        "Use sort_by='created_at' + sort_order='desc' to get the most recently ingested papers."
    ),
)
def query_papers(
    q: str | None = None,
    year: int | None = None,
    journal: str | None = None,
    has_dft_results: bool | None = None,
    has_writing_cards: bool | None = None,
    sort_by: str = "year_serial",
    sort_order: str = "desc",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        from app.schemas.api import PaperListFilterParams

        items = PaperQueryService(session).list_papers(
            PaperListFilterParams(
                q=q,
                year=year,
                journal=journal,
                has_dft_results=has_dft_results,
                has_writing_cards=has_writing_cards,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=offset,
            )
        )
        return {
            "returned": len(items),
            "items": [item.model_dump(mode="json") for item in items],
        }


@mcp_server.tool(name="scan_local_pdfs", description="Scan a local folder for PDF files and report which ones are already parsed.")
def scan_local_pdfs(folder_path: str, recursive: bool = True, limit: int = 100) -> dict[str, Any]:
    require_mcp_capability("request_parse")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return LocalPdfService(session=session, settings=settings).scan_folder(
            folder_path=folder_path,
            recursive=recursive,
            limit=limit,
        )


@mcp_server.tool(name="get_paper", description="Get full parsed data for a paper.")
def get_paper(paper_id: str) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        detail = PaperQueryService(session).get_paper_detail(UUID(paper_id))
        if not detail:
            raise ValueError("Paper not found")
        notes = session.scalars(
            select(PaperNote)
            .where(PaperNote.paper_id == UUID(paper_id))
            .order_by(PaperNote.created_at.desc())
        ).all()
        payload = detail.model_dump(mode="json")
        payload["notes"] = [_serialize_note(note) for note in notes]
        return payload


@mcp_server.tool(name="get_codex_context", description="Get a compact Codex-ready paper bundle with metadata, sections, figures, tables, candidates, warnings, notes, and Markdown.")
def get_codex_context(
    paper_id: str,
    max_sections: int = 8,
    max_chars_per_section: int = 1800,
    max_figures: int = 12,
    max_tables: int = 8,
    max_candidates: int = 20,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        context = CodexContextService(session).build_context(
            UUID(paper_id),
            max_sections=max(1, min(max_sections, 20)),
            max_chars_per_section=max(300, min(max_chars_per_section, 6000)),
            max_figures=max(0, min(max_figures, 40)),
            max_tables=max(0, min(max_tables, 30)),
            max_candidates=max(1, min(max_candidates, 100)),
        )
        if context is None:
            raise ValueError("Paper not found")
        return context.model_dump(mode="json")


@mcp_server.tool(name="get_codex_item", description="Get low-token, evidence-aware context for one section, figure, table, DFT candidate, mechanism claim, writing card, or other supported paper item.")
def get_codex_item(
    paper_id: str,
    item_type: str,
    item_id: str,
    max_chars_per_section: int = 1600,
    max_related_sections: int = 3,
    max_locators: int = 12,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        context = CodexContextService(session).build_item_context(
            UUID(paper_id),
            item_type,
            UUID(item_id),
            max_chars_per_section=max(300, min(max_chars_per_section, 6000)),
            max_related_sections=max(0, min(max_related_sections, 8)),
            max_locators=max(0, min(max_locators, 40)),
        )
        if context is None:
            raise ValueError("Paper or item not found")
        return context.model_dump(mode="json")


@mcp_server.tool(name="get_paper_knowledge", description="Get Codex-ready knowledge candidates from mechanism claims, writing cards, external AI imports, notes, and section fallbacks.")
def get_paper_knowledge(
    paper_id: str,
    max_candidates: int = 60,
    max_chars_per_candidate: int = 1200,
    category: str | None = None,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        context = PaperKnowledgeService(session).build_context(
            UUID(paper_id),
            max_candidates=max(1, min(max_candidates, 120)),
            max_chars_per_candidate=max(300, min(max_chars_per_candidate, 4000)),
            category=category,
        )
        if context is None:
            raise ValueError("Paper not found")
        return context


@mcp_server.tool(
    name="search_external_papers",
    description=(
        "Search external literature databases (OpenAlex, arXiv, etc.) for papers matching a query. "
        "Supports year filtering and target type classification (computational/experimental/review). "
        "Results include title, DOI, year, journal, abstract, and open-access status. "
        "This tool only searches; use the controlled Literature Intake endpoints to review and approve candidates before import."
    ),
)
def search_external_papers(
    query: str,
    providers: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    target_types: list[str] | None = None,
    max_results: int = 20,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()

    service = DiscoveryService()
    active_providers = providers or service.DEFAULT_SEARCH_PROVIDERS

    # Validate target_types
    valid_types = {"computational", "experimental", "review"}
    if target_types:
        target_types = [t for t in target_types if t in valid_types]

    raw_results = service.search(
        query=query,
        providers=active_providers,
        limit=max(1, min(max_results * 2, 100)),  # Fetch more to allow post-filtering
        target_types=target_types or None,
    )

    # Apply year filtering (client-side; not all provider APIs expose year filters)
    filtered = []
    for item in raw_results:
        year = item.get("year")
        if year_min is not None and (year is None or year < year_min):
            continue
        if year_max is not None and (year is None or year > year_max):
            continue
        filtered.append(item)

    # Trim to requested limit after filtering
    filtered = filtered[:max_results]

    # Record search in audit log for traceability
    with session_scope(settings.database_url) as session:
        audit = AuditLog(
            action="search_external_papers",
            source="mcp",
            target_id=None,
            payload={
                "query": query,
                "providers": active_providers,
                "year_min": year_min,
                "year_max": year_max,
                "target_types": target_types,
                "max_results_requested": max_results,
                "raw_results": len(raw_results),
                "filtered_results": len(filtered),
            },
        )
        session.add(audit)
        session.flush()

    return {
        "query": query,
        "providers": active_providers,
        "year_min": year_min,
        "year_max": year_max,
        "target_types": target_types,
        "total_found": len(raw_results),
        "results_after_filter": len(filtered),
        "results": filtered,
    }


@mcp_server.tool(name="insert_word_citation", description="Insert a safe draft citation into a DOCX copy using the local literature database citation guardrails.")
def insert_word_citation(
    docx_path: str,
    selected_paper_id: str,
    text: str,
    output_filename: str | None = None,
    docx_insertion_mode: str = "append_paragraph",
    placeholder: str | None = None,
    citation_marker: str | None = None,
    citation_insertion_mode: str = "parenthetical",
    citation_style: str = "draft_author_year",
    user_note: str | None = None,
) -> dict[str, Any]:
    require_mcp_capability("export_data")
    require_mcp_exports_enabled()
    input_path = Path(docx_path).expanduser()
    if not input_path.exists() or not input_path.is_file():
        raise ValueError(f"DOCX file not found: {docx_path}")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        result = WordCitationInsertionService(session=session, settings=settings).insert(
            WordCitationInsertRequest(
                document_bytes=input_path.read_bytes(),
                filename=input_path.name,
                text=text,
                selected_paper_id=UUID(selected_paper_id),
                citation_marker=citation_marker,
                docx_insertion_mode=docx_insertion_mode,
                citation_insertion_mode=citation_insertion_mode,
                citation_style=citation_style,
                placeholder=placeholder,
                output_filename=output_filename,
                user_note=user_note,
            )
        )
        if result is None:
            raise ValueError("Paper not found")
        return result


@mcp_server.tool(name="verify_dft_result", description="Mark one evidence-backed DFT result candidate as reviewed after Codex/human PDF evidence verification.")
def verify_dft_result(
    paper_id: str,
    dft_result_id: str,
    confirm_reviewed_against_pdf: bool,
    reviewer_note: str | None = None,
    field_names: list[str] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability_any("review_corrections", "review_dft")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return DFTResultReviewService(session).verify_result(
            paper_id=UUID(paper_id),
            result_id=UUID(dft_result_id),
            confirm_reviewed_against_pdf=confirm_reviewed_against_pdf,
            reviewer=auth.source_prefix,
            reviewer_note=reviewer_note,
            field_names=field_names,
        )


@mcp_server.tool(name="reject_dft_result", description="Mark one DFT result candidate as rejected so it stays blocked from ML export and leaves the active review queue.")
def reject_dft_result(
    paper_id: str,
    dft_result_id: str,
    confirm_reject_candidate: bool,
    reviewer_note: str | None = None,
    field_names: list[str] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability_any("review_corrections", "review_dft")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return DFTResultReviewService(session).reject_result(
            paper_id=UUID(paper_id),
            result_id=UUID(dft_result_id),
            confirm_reject_candidate=confirm_reject_candidate,
            reviewer=auth.source_prefix,
            reviewer_note=reviewer_note,
            field_names=field_names,
        )


@mcp_server.tool(
    name="verify_dft_results_batch",
    description="Batch-verify multiple DFT result candidates for the same paper in one call. Skips individual failures and reports them.",
)
def verify_dft_results_batch(
    paper_id: str,
    dft_result_ids: list[str],
    confirm_reviewed_against_pdf: bool,
    reviewer_note: str | None = None,
    field_names: list[str] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability_any("review_corrections", "review_dft")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return DFTResultReviewService(session).verify_results_batch(
            paper_id=UUID(paper_id),
            result_ids=[UUID(rid) for rid in dft_result_ids],
            confirm_reviewed_against_pdf=confirm_reviewed_against_pdf,
            reviewer=auth.source_prefix,
            reviewer_note=reviewer_note,
            field_names=field_names,
        )


@mcp_server.tool(
    name="reject_dft_results_batch",
    description="Batch-reject multiple DFT result candidates for the same paper in one call. Skips individual failures and reports them.",
)
def reject_dft_results_batch(
    paper_id: str,
    dft_result_ids: list[str],
    confirm_reject_candidate: bool,
    reviewer_note: str | None = None,
    field_names: list[str] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability_any("review_corrections", "review_dft")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return DFTResultReviewService(session).reject_results_batch(
            paper_id=UUID(paper_id),
            result_ids=[UUID(rid) for rid in dft_result_ids],
            confirm_reject_candidate=confirm_reject_candidate,
            reviewer=auth.source_prefix,
            reviewer_note=reviewer_note,
            field_names=field_names,
        )


@mcp_server.tool(name="propose_dft_result_correction", description="Create a pending correction proposal for one DFT result field without applying it.")
def propose_dft_result_correction(
    paper_id: str,
    dft_result_id: str,
    field_name: str,
    proposed_value: Any,
    reason: str,
    confirm_correction_proposal: bool,
    evidence_payload: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("propose_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return DFTResultReviewService(session).propose_correction(
            paper_id=UUID(paper_id),
            result_id=UUID(dft_result_id),
            confirm_correction_proposal=confirm_correction_proposal,
            field_name=field_name,
            proposed_value=proposed_value,
            reason=reason,
            reviewer=auth.source_prefix,
            evidence_payload=evidence_payload,
        )


@mcp_server.tool(name="get_dft_review_queue", description="List DFT result candidates that need Codex/human verification before ML export.")
def get_dft_review_queue(
    property_type: str | None = None,
    adsorbate: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    paper_id: str | None = None,
    library_name: str | None = None,
    reason: str | None = None,
    status: str = "needs_review",
    limit: int = 50,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return DFTReviewQueueService(session).list_queue(
            property_type=property_type,
            adsorbate=adsorbate,
            year_min=year_min,
            year_max=year_max,
            paper_id=UUID(paper_id) if paper_id else None,
            library_name=library_name,
            reason=reason,
            status=status,
            limit=max(1, min(limit, 200)),
        )


@mcp_server.tool(
    name="get_review_conflicts",
    description=(
        "Read-only multi-AI/reviewer conflict aggregation grouped by paper_id, target_type, target_id, "
        "and field_name. Does not approve, merge, or verify anything."
    ),
)
def get_review_conflicts(
    paper_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    field_name: str | None = None,
    include_non_conflicts: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return ReviewConflictAggregationService(session).list_conflicts(
            paper_id=UUID(paper_id) if paper_id else None,
            target_type=target_type,
            target_id=target_id,
            field_name=field_name,
            include_non_conflicts=include_non_conflicts,
            limit=max(1, min(limit, 1000)),
        )


@mcp_server.tool(name="list_notes", description="List shared notes for a paper.")
def list_notes(paper_id: str, source: str | None = None) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        _ensure_paper_exists(session, UUID(paper_id))
        stmt = (
            select(PaperNote)
            .where(PaperNote.paper_id == UUID(paper_id))
            .order_by(PaperNote.created_at.desc())
        )
        if source:
            stmt = stmt.where(PaperNote.source == source)
        items = session.scalars(stmt).all()
        return {"items": [_serialize_note(item) for item in items]}


@mcp_server.tool(name="append_note", description="Append a shared review note for a paper.")
def append_note(
    paper_id: str,
    content: str,
    field_name: str | None = None,
    page: int | None = None,
    section_title: str | None = None,
    quoted_text: str | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("append_notes")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        _ensure_paper_exists(session, UUID(paper_id))
        note = PaperNote(
            paper_id=UUID(paper_id),
            source=auth.source_prefix,
            content=content,
            field_name=field_name,
            page=page,
            section_title=section_title,
            quoted_text=quoted_text,
        )
        session.add(note)
        session.flush()
        session.add(
            AuditLog(
                paper_id=note.paper_id,
                action="append_note",
                source=auth.source_prefix,
                target_type="paper_note",
                target_id=str(note.id),
                payload={
                    "field_name": field_name,
                    "page": page,
                    "section_title": section_title,
                },
            )
        )
        session.refresh(note)
        return _serialize_note(note)


@mcp_server.tool(
    name="propose_correction",
    description=(
        "Apply a non-DFT correction immediately with last-writer-wins semantics. "
        "DFT data/settings corrections remain pending for the dedicated review flow."
    ),
)
def propose_correction(
    paper_id: str,
    field_name: str,
    target_path: str,
    operation: str,
    proposed_value: Any,
    reason: str,
    evidence_payload: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("propose_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        _ensure_paper_exists(session, UUID(paper_id))
        correction = PaperCorrection(
            paper_id=UUID(paper_id),
            source=auth.source_prefix,
            field_name=field_name,
            target_path=target_path,
            operation=operation,
            proposed_value=proposed_value,
            reason=reason,
            evidence_payload=evidence_payload,
            status="pending",
        )
        session.add(correction)
        session.flush()
        session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action="propose_correction",
                source=auth.source_prefix,
                target_type="paper_correction",
                target_id=str(correction.id),
                payload={
                    "field_name": field_name,
                    "target_path": target_path,
                    "operation": operation,
                },
            )
        )
        dft_fields = {"dft_result", "dft_results", "dft_setting", "dft_settings"}
        target_root = str(target_path or "").strip().lower().replace(".", ":").split(":", 1)[0]
        if str(field_name or "").strip().lower() not in dft_fields and target_root not in dft_fields:
            correction = ReviewService(session).approve_correction(
                correction.id,
                reviewer=auth.source_prefix,
            )
        session.refresh(correction)
        return _serialize_correction(correction)


@mcp_server.tool(name="get_parse_status", description="Check the status of a parse job.")
def get_parse_status(job_id: str) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        job = session.get(ParseJob, UUID(job_id))
        if not job:
            raise ValueError("Parse job not found")
        return _serialize_parse_job(job)


@mcp_server.tool(name="get_correction_queue", description="List correction proposals pending curator review.")
def get_correction_queue(status: str | None = "pending") -> dict[str, Any]:
    require_mcp_capability("review_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        items = ReviewService(session).list_corrections(status=status)
        return {"items": [_serialize_correction(item) for item in items]}


@mcp_server.tool(name="get_correction_detail", description="Get a correction proposal with current target value for review.")
def get_correction_detail(correction_id: str) -> dict[str, Any]:
    require_mcp_capability("review_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        detail = ReviewService(session).get_correction_detail(UUID(correction_id))
        return _serialize_correction_detail(detail)


@mcp_server.tool(name="approve_correction", description="Approve a pending correction proposal and apply it.")
def approve_correction(correction_id: str, write_lock_token: str | None = None) -> dict[str, Any]:
    auth = require_mcp_capability("review_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        item = ReviewService(session).approve_correction(
            UUID(correction_id),
            reviewer=auth.source_prefix,
            write_lock_tokens=[write_lock_token] if write_lock_token else None,
        )
        return _serialize_correction(item)


@mcp_server.tool(name="reject_correction", description="Reject a pending correction proposal.")
def reject_correction(correction_id: str, reason: str | None = None) -> dict[str, Any]:
    auth = require_mcp_capability("review_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        item = ReviewService(session).reject_correction(
            UUID(correction_id),
            reviewer=auth.source_prefix,
            reason=reason,
        )
        return _serialize_correction(item)


@mcp_server.tool(
    name="approve_corrections_batch",
    description="Approve multiple pending correction proposals in one call. Skips non-pending or failed items and reports them.",
)
def approve_corrections_batch(correction_ids: list[str], write_lock_token: str | None = None) -> dict[str, Any]:
    auth = require_mcp_capability("review_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return ReviewService(session).approve_corrections_batch(
            correction_ids=[UUID(cid) for cid in correction_ids],
            reviewer=auth.source_prefix,
            write_lock_tokens=[write_lock_token] if write_lock_token else None,
        )


@mcp_server.tool(
    name="reject_corrections_batch",
    description="Reject multiple pending correction proposals in one call. Skips non-pending or failed items and reports them.",
)
def reject_corrections_batch(correction_ids: list[str], reason: str | None = None) -> dict[str, Any]:
    auth = require_mcp_capability("review_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return ReviewService(session).reject_corrections_batch(
            correction_ids=[UUID(cid) for cid in correction_ids],
            reviewer=auth.source_prefix,
            reason=reason,
        )


@mcp_server.tool(
    name="acquire_module_write_lock",
    description=(
        "Acquire a lease before directly applying non-DFT AI edits to a paper module. "
        "Use module_name values such as sections, writing_cards, figures, content, or all_non_dft."
    ),
)
def acquire_module_write_lock(
    paper_id: str,
    module_name: str,
    locked_by: str | None = None,
    ttl_minutes: int = 30,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("propose_corrections")
    owner = str(locked_by or auth.source_prefix or "ide_ai").strip() or "ide_ai"
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        lock = ModuleWriteLockService(session).acquire(
            paper_id=UUID(paper_id),
            module_name=module_name,
            locked_by=owner,
            ttl_minutes=ttl_minutes,
            meta=metadata,
        )
        session.flush()
        return {
            "id": str(lock.id),
            "paper_id": str(lock.paper_id),
            "module_name": lock.module_name,
            "locked_by": lock.locked_by,
            "lock_token": lock.lock_token,
            "status": lock.status,
            "expires_at": lock.expires_at.isoformat(),
        }


@mcp_server.tool(name="release_module_write_lock", description="Release a previously acquired module write lock.")
def release_module_write_lock(lock_token: str, released_by: str | None = None) -> dict[str, Any]:
    auth = require_mcp_capability("propose_corrections")
    releaser = str(released_by or auth.source_prefix or "").strip() or None
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        lock = ModuleWriteLockService(session).release(lock_token=lock_token, released_by=releaser)
        session.flush()
        return {
            "id": str(lock.id),
            "paper_id": str(lock.paper_id),
            "module_name": lock.module_name,
            "locked_by": lock.locked_by,
            "status": lock.status,
            "released_at": lock.released_at.isoformat() if lock.released_at else None,
        }


@mcp_server.tool(name="parse_paper", description="Parse a paper from DOI or arXiv identifier.")
async def parse_paper(identifier: str, providers: list[str] | None = None) -> dict[str, Any]:
    auth = require_mcp_capability("request_parse")
    settings = get_settings()

    with session_scope(settings.database_url) as session:
        job = ParseJob(
            identifier=identifier.strip(),
            providers=providers or [],
            requested_by=auth.source_prefix,
            status="running",
        )
        session.add(job)
        session.flush()
        job_id = job.id

    try:
        service = DiscoveryService()
        raw_paper, metadata = await run_in_threadpool(service.fetch_metadata, identifier, providers)

        with session_scope(settings.database_url) as session:
            job = session.get(ParseJob, job_id)
            if job is None:
                raise ValueError("Parse job not found")

            doi = metadata.get("doi")
            if doi:
                existing = session.scalar(
                    select(Paper).where(
                        Paper.doi == doi,
                        build_library_name_clause(Paper.library_name, DEFAULT_LIBRARY_NAME),
                    )
                )
                if existing:
                    job.status = "completed"
                    job.paper_id = existing.id
                    job.error_message = None
                    session.add(
                        AuditLog(
                            paper_id=existing.id,
                            action="parse_paper_existing",
                            source=auth.source_prefix,
                            target_type="parse_job",
                            target_id=str(job.id),
                            payload={"identifier": identifier, "doi": doi, "library_name": DEFAULT_LIBRARY_NAME},
                        )
                    )
                    session.flush()
                    session.refresh(job)
                    return _serialize_parse_job(job)

            ingestion = PaperIngestionService(session=session, settings=settings)
            with TemporaryDirectory() as tmpdir:
                pdf_path = await run_in_threadpool(service.download_pdf, raw_paper, Path(tmpdir))
                paper = await ingestion.ingest_pdf(
                    source_path=pdf_path,
                    original_filename=pdf_path.name,
                    copy_pdf=True,
                )

            updated = False
            if doi and paper.doi != doi:
                paper.doi = doi
                updated = True
            if metadata.get("title") and (not paper.title or paper.title == pdf_path.name):
                paper.title = metadata["title"]
                updated = True
            if metadata.get("year") and not paper.year:
                paper.year = metadata["year"]
                updated = True
            if metadata.get("journal") and not paper.journal:
                paper.journal = metadata["journal"]
                updated = True
            if metadata.get("authors") and not paper.authors:
                paper.authors = metadata["authors"]
                updated = True
            if metadata.get("abstract") and not paper.abstract:
                paper.abstract = metadata["abstract"]
                updated = True
            if updated:
                session.add(paper)

            job.status = "completed"
            job.paper_id = paper.id
            job.error_message = None
            session.add(
                AuditLog(
                    paper_id=paper.id,
                    action="parse_paper",
                    source=auth.source_prefix,
                    target_type="parse_job",
                    target_id=str(job.id),
                    payload={"identifier": identifier, "providers": providers or []},
                )
            )
            session.flush()
            session.refresh(job)
            return _serialize_parse_job(job)
    except Exception as exc:
        with session_scope(settings.database_url) as session:
            job = session.get(ParseJob, job_id)
            if job is None:
                raise
            job.status = "failed"
            job.error_message = str(exc)
            session.add(
                AuditLog(
                    action="parse_paper_failed",
                    source=auth.source_prefix,
                    target_type="parse_job",
                    target_id=str(job.id),
                    payload={"identifier": identifier, "error": str(exc)},
                )
            )
            session.flush()
            session.refresh(job)
            return _serialize_parse_job(job)


@mcp_server.tool(name="ingest_pdf_batch", description="Batch ingest local PDF files from a folder, optionally skipping already parsed files.")
async def ingest_pdf_batch(
    folder_path: str,
    recursive: bool = True,
    limit: int = 20,
    only_unparsed: bool = True,
) -> dict[str, Any]:
    auth = require_mcp_capability("request_parse")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        return await LocalPdfService(session=session, settings=settings).ingest_folder(
            folder_path=folder_path,
            requested_by=auth.source_prefix,
            recursive=recursive,
            limit=limit,
            only_unparsed=only_unparsed,
        )


# ---------------------------------------------------------------------------
# IDE/MCP analysis integration tools (retrieve_evidence, review_paper,
# import_analysis, compare_papers)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="retrieve_evidence",
    description="Semantic search across parsed papers for structured evidence (DFT results, mechanism claims, electrochemical data, writing cards, sections, figure data points). Use this to find relevant evidence across multiple papers by topic.",
)
def retrieve_evidence(
    query: str,
    paper_ids: list[str] | None = None,
    evidence_types: list[str] | None = None,
    limit_per_type: int = 5,
    target_paper_type: str | None = None,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        embedding = get_embedding_service(
            provider=settings.embedding_provider,
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
        retriever = Retriever(session, embedding_dimension=settings.embedding_dimension, embedding=embedding)
        uuid_paper_ids = [UUID(pid) for pid in paper_ids] if paper_ids else None
        result = retriever.retrieve(
            query=query,
            paper_ids=uuid_paper_ids,
            limit_per_type=limit_per_type,
            target_paper_type=target_paper_type,
        )
        # Convert UUID keys to strings for JSON serialization
        serialized: dict[str, list[dict[str, Any]]] = {}
        for key, items in result.items():
            serialized[key] = []
            for item in items:
                d = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                serialized[key].append(d)
        valid_types = {
            "sections",
            "dft_results",
            "electrochemical_performance",
            "mechanism_claims",
            "writing_cards",
            "figure_data_points",
        }
        if evidence_types:
            filtered = {k: v for k, v in serialized.items() if k in evidence_types and k in valid_types}
            return {"evidence_types_requested": evidence_types, "results": filtered}
        return {"results": serialized}


@mcp_server.tool(
    name="review_paper",
    description="DISABLED and deprecated compatibility tool; it always raises and never runs backend-owned LLM review. Use get_codex_context / get_codex_item / read_paper_page, then import_analysis.",
)
async def review_paper(
    paper_id: str,
    auto_apply: bool = False,
    source_label: str = "mcp_review",
) -> dict[str, Any]:
    require_mcp_capability("propose_corrections")
    raise ValueError(
        "Backend-owned LLM review is disabled. Use get_codex_context / get_codex_item / read_paper_page in the IDE, then call import_analysis with the IDE AI output."
    )


@mcp_server.tool(
    name="import_analysis",
    description=(
        "Import IDE AI analysis results into the library. "
        "Supports free-text or structured JSON, including object-level review audits with target_type, target_id, "
        "field_name, decision, evidence_location, and corrected_value. Non-DFT AI writes are applied directly "
        "with last-writer-wins semantics and do not require a module write lock."
    ),
)
def import_analysis(
    paper_id: str,
    source: str,
    source_label: str = "",
    raw_text: str | None = None,
    raw_payload: dict | None = None,
    auto_apply_review_rules: bool = True,
    reviewer: str | None = None,
    write_lock_token: str | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("propose_corrections")
    has_text = bool(str(raw_text or "").strip())
    has_payload = raw_payload is not None
    if isinstance(raw_payload, str):
        has_payload = bool(raw_payload.strip())
    elif isinstance(raw_payload, (dict, list)):
        has_payload = bool(raw_payload)
    if not has_text and not has_payload:
        raise ValueError("import_analysis requires non-empty raw_text or raw_payload")
    effective_reviewer = str(reviewer or auth.source_prefix or "ide_ai").strip() or str(auth.source_prefix or "ide_ai")
    effective_internal_reviewer = str(auth.source_prefix or effective_reviewer or "ide_ai").strip() or "ide_ai"
    effective_lock_owners = list(
        dict.fromkeys(
            item
            for item in [effective_internal_reviewer, effective_reviewer]
            if str(item or "").strip()
        )
    )
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        service = ExternalAnalysisService(session=session, settings=settings)
        run = service.import_run(
            paper_id=UUID(paper_id),
            source=source,
            source_label=source_label or source,
            raw_text=raw_text,
            raw_payload=raw_payload,
        )
        auto_apply_summary = None
        if auto_apply_review_rules:
            auto_apply_summary = service.apply_review_rules_for_run(
                run.id,
                reviewer=effective_internal_reviewer,
                write_lock_tokens=[write_lock_token] if write_lock_token else None,
                write_lock_owner=effective_lock_owners,
                auto_lock_owner=effective_internal_reviewer,
                lock_meta_source="mcp_import_analysis",
            )
        candidates = service.list_candidates(run.id)
        session.commit()
        return {
            "run_id": str(run.id),
            "mapping_status": run.mapping_status,
            "mapping_error": run.mapping_error,
            "auto_apply_review_rules": auto_apply_review_rules,
            "reviewer": effective_reviewer,
            "auto_apply_summary": auto_apply_summary,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "id": str(c.id),
                    "type": c.candidate_type,
                    "confidence": c.confidence,
                    "status": c.status,
                    "target_type": (c.normalized_payload or {}).get("target_type"),
                    "target_id": (c.normalized_payload or {}).get("target_id"),
                    "field_name": (c.normalized_payload or {}).get("field_name"),
                    "decision": (c.normalized_payload or {}).get("decision") or (c.normalized_payload or {}).get("verdict"),
                    "verification_status": (c.normalized_payload or {}).get("verification_status"),
                    "summary": (
                        (c.normalized_payload or {}).get("content")
                        or (c.normalized_payload or {}).get("reason")
                        or (c.normalized_payload or {}).get("recommended_action")
                        or ""
                    ),
                }
                for c in candidates
            ],
        }


@mcp_server.tool(
    name="compare_papers",
    description="Compare extracted data across multiple papers side-by-side. Returns structured results for DFT settings, catalyst samples, performance metrics, mechanism claims, etc. Useful for finding contradictions or confirming trends.",
)
def compare_papers(
    paper_ids: list[str],
    fields: list[str] | None = None,
) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    if len(paper_ids) < 2 or len(paper_ids) > 10:
        raise ValueError("Must compare 2-10 papers")

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        query_service = PaperQueryService(session)
        papers_data: list[dict[str, Any]] = []
        for pid in paper_ids:
            detail = query_service.get_paper_detail(UUID(pid))
            if not detail:
                raise ValueError(f"Paper {pid} not found")
            papers_data.append({
                "id": str(detail.id),
                "paper_code": detail.paper_code,
                "title": detail.title,
                "year": detail.year,
                "journal": detail.journal,
                "paper_type": detail.comprehensive_analysis.get("paper_type") if detail.comprehensive_analysis else None,
                "dft_settings": [item.model_dump(mode="json") for item in detail.dft_settings_items[:20]],
                "catalyst_samples": [item.model_dump(mode="json") for item in detail.catalyst_samples_items[:20]],
                "dft_results": [item.model_dump(mode="json") for item in detail.dft_results_items[:30]],
                "electrochemical_performance": [item.model_dump(mode="json") for item in detail.electrochemical_performance_items[:20]],
                "mechanism_claims": [item.model_dump(mode="json") for item in detail.mechanism_claims_items[:20]],
                "writing_cards": [item.model_dump(mode="json") for item in detail.writing_cards_items[:15]],
            })

        valid_fields = {"dft_settings", "catalyst_samples", "dft_results", "electrochemical_performance", "mechanism_claims", "writing_cards"}
        active_fields = (set(fields) & valid_fields) if fields else valid_fields

        comparison: list[dict[str, Any]] = []
        for p in papers_data:
            entry: dict[str, Any] = {"id": p["id"], "paper_code": p.get("paper_code"), "title": p["title"], "year": p["year"], "paper_type": p["paper_type"]}
            for f in active_fields:
                entry[f] = p.get(f, [])
            comparison.append(entry)

        return {
            "paper_count": len(comparison),
            "compared_fields": sorted(active_fields),
            "papers": comparison,
        }


# ---------------------------------------------------------------------------
# On-demand atomic tools (read_paper_page, recrop_figure, review_figure)
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="read_paper_page",
    description="Read the exact full layout of a specific page or range of pages from a paper. Returns all sections, tables, and figures whose page range overlaps with the requested pages.",
)
def read_paper_page(paper_id: str, page_start: int, page_end: int | None = None) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    if page_start < 1:
        raise ValueError("page_start must be >= 1")
    if page_end is not None and page_end < page_start:
        raise ValueError("page_end must be >= page_start when provided")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        _ensure_paper_exists(session, UUID(paper_id))

        pid = UUID(paper_id)
        effective_page_end = page_end if page_end is not None else page_start

        # Fetch sections whose known page range overlaps with the requested range.
        # If only one side of the range is known, use that page as a precise anchor;
        # fully unknown sections are excluded to avoid returning the whole paper.
        section_overlap = or_(
            and_(
                PaperSection.page_start.is_not(None),
                PaperSection.page_end.is_not(None),
                PaperSection.page_start <= effective_page_end,
                PaperSection.page_end >= page_start,
            ),
            and_(
                PaperSection.page_start.is_not(None),
                PaperSection.page_end.is_(None),
                PaperSection.page_start >= page_start,
                PaperSection.page_start <= effective_page_end,
            ),
            and_(
                PaperSection.page_start.is_(None),
                PaperSection.page_end.is_not(None),
                PaperSection.page_end >= page_start,
                PaperSection.page_end <= effective_page_end,
            ),
        )
        sections = session.scalars(
            select(PaperSection)
            .where(PaperSection.paper_id == pid)
            .where(section_overlap)
            .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.section_title.asc())
        ).all()

        # Fetch tables on this page range
        tables = session.scalars(
            select(PaperTable)
            .where(PaperTable.paper_id == pid)
            .where(PaperTable.page >= page_start)
            .where(PaperTable.page <= effective_page_end)
        ).all()

        # Fetch figures on this page range (caption, role, and image availability)
        figures = session.scalars(
            select(PaperFigure)
            .where(PaperFigure.paper_id == pid)
            .where(PaperFigure.page >= page_start)
            .where(PaperFigure.page <= effective_page_end)
        ).all()

        page_parts: list[str] = []
        figure_refs: list[dict[str, str]] = []

        for sec in sections:
            label = sec.section_title or sec.section_type or "Section"
            page_parts.append(f"## {label} (pp. {sec.page_start}-{sec.page_end})\n{sec.text}")

        for tbl in tables:
            label = tbl.caption or "Table"
            page_parts.append(f"## {label} (p. {tbl.page})\n{tbl.markdown_content or ''}")

        for fig in figures:
            label = fig.caption or "Figure"
            summary = fig.content_summary or "N/A"
            role = fig.figure_role or "N/A"
            page_parts.append(
                f"## {label} (p. {fig.page})\n"
                f"Role: {role}\n"
                f"Stored Figure Summary: {summary}\n"
                f"Figure ID: {fig.id}. Inspect the image/crop in the IDE workflow if visual evidence matters."
            )
            figure_refs.append({
                "figure_id": str(fig.id),
                "caption": fig.caption or "",
                "figure_role": role,
                "has_image": bool(fig.image_path),
            })

        full_text = "\n\n---\n\n".join(page_parts)

        return {
            "paper_id": paper_id,
            "page_start": page_start,
            "page_end": effective_page_end,
            "section_count": len(sections),
            "table_count": len(tables),
            "figure_count": len(figures),
            "figure_refs": figure_refs,
            "full_text": full_text,
        }


@mcp_server.tool(
    name="recrop_figure",
    description=(
        "Recrop a specific figure from the original PDF and update its image in the database. "
        "Strategies: 'full_page' (safest, returns the whole page), 'wider' (expands original bbox by 30%), "
        "'tight' (shrinks by 10%), 'ai_bbox' (uses new_bbox provided by AI). "
        "IMPORTANT: If using 'ai_bbox', you SHOULD first write a local Python script using PyMuPDF (fitz) "
        "to test the crop locally and visually verify it using view_file before calling this tool!"
    ),
)
def recrop_figure(
    figure_id: str,
    strategy: str = "full_page",
    new_bbox: list[float] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("request_parse")  # Modifying images is a parse-level action
    if strategy not in ("full_page", "wider", "tight", "ai_bbox"):
        raise ValueError("Invalid strategy")
    if strategy == "ai_bbox" and not new_bbox:
        raise ValueError("new_bbox is required when strategy is 'ai_bbox'")
    if new_bbox and len(new_bbox) != 4:
        raise ValueError("new_bbox must be [x0, y0, x1, y1]")

    settings = get_settings()
    import fitz

    # Phase 1: Read figure + paper metadata from DB, then close session immediately.
    # Avoid holding a session
    # open during PDF rendering and file I/O — long-lived transactions prevent connection
    # pool recycling and can cause idle-in-transaction timeouts.
    fig_meta: dict[str, Any] = {}
    with session_scope(settings.database_url) as session:
        from app.db.models import Paper
        fig = session.get(PaperFigure, UUID(figure_id))
        if not fig:
            raise ValueError(f"Figure {figure_id} not found")

        paper = session.get(Paper, fig.paper_id)
        if not paper or not paper.pdf_path:
            raise ValueError("Associated paper or PDF path not found")

        pdf_abs_path = resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=settings,
            trusted_persisted_reference=True,
        )
        if not pdf_abs_path or not pdf_abs_path.exists():
            raise ValueError(f"PDF file not found: {paper.pdf_path}")

        if fig.page is None or fig.page < 1:
            raise ValueError("Figure does not have a valid page number")

        fig_meta = {
            "paper_id": str(paper.id),
            "figure_page": fig.page,
            "old_image_path": fig.image_path,
            "prov": list(fig.prov) if fig.prov else [],
            "write_version": fig.write_version,
        }

    # Phase 2: PDF rendering — session is already closed, safe to do I/O.
    doc = fitz.open(str(pdf_abs_path))
    try:
        page_index = fig_meta["figure_page"] - 1
        if page_index >= len(doc):
            raise ValueError(f"Page {fig_meta['figure_page']} is out of bounds for this PDF")

        page = doc[page_index]
        page_rect = page.rect

        target_rect = None
        orig_bbox = None
        prov = fig_meta["prov"]
        # prov is a list of dicts; the last entry with "bbox" is the most recent crop.
        # bbox format in prov: {"l": ..., "t": ..., "r": ..., "b": ..., "coord_origin": "TOPLEFT"}
        if prov and isinstance(prov, list):
            for entry in reversed(prov):
                if isinstance(entry, dict) and "bbox" in entry:
                    orig_bbox = entry["bbox"]
                    break

        if strategy == "full_page":
            target_rect = page_rect
        elif strategy == "ai_bbox" and new_bbox:
            target_rect = fitz.Rect(new_bbox[0], new_bbox[1], new_bbox[2], new_bbox[3])
        elif strategy in ("wider", "tight"):
            if not orig_bbox:
                raise ValueError(f"Cannot apply '{strategy}' strategy because original bbox is not found in provenance.")
            # orig_bbox from prov uses named keys: {"l": left, "t": top, "r": right, "b": bottom}
            if isinstance(orig_bbox, dict):
                left = float(orig_bbox.get("l", orig_bbox.get("x0", 0)))
                top = float(orig_bbox.get("t", orig_bbox.get("y0", 0)))
                right = float(orig_bbox.get("r", orig_bbox.get("x1", 0)))
                bottom = float(orig_bbox.get("b", orig_bbox.get("y1", 0)))
                r = fitz.Rect(left, top, right, bottom)
            elif isinstance(orig_bbox, (list, tuple)) and len(orig_bbox) == 4:
                r = fitz.Rect(orig_bbox[0], orig_bbox[1], orig_bbox[2], orig_bbox[3])
            else:
                raise ValueError(f"Unsupported bbox format in provenance: {type(orig_bbox)}")

            if strategy == "wider":
                pad_x = r.width * 0.15
                pad_y = r.height * 0.15
                target_rect = fitz.Rect(r.x0 - pad_x, r.y0 - pad_y, r.x1 + pad_x, r.y1 + pad_y)
            else:  # tight
                pad_x = r.width * 0.05
                pad_y = r.height * 0.05
                target_rect = fitz.Rect(r.x0 + pad_x, r.y0 + pad_y, r.x1 - pad_x, r.y1 - pad_y)

        if target_rect:
            target_rect = target_rect.intersect(page_rect)

        if not target_rect or target_rect.is_empty:
            raise ValueError("Calculated crop rectangle is empty or invalid")

        zoom = 2.0  # High res
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, clip=target_rect, alpha=False)

        import uuid as _uuid
        new_filename = f"{fig_meta['paper_id']}_fig_{_uuid.uuid4().hex[:8]}.png"
        new_rel_path = f"{fig_meta['paper_id']}/{new_filename}"
        new_abs_path = settings.storage_paths["figures"] / fig_meta["paper_id"] / new_filename

        new_abs_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(new_abs_path))

        rendered_meta = {
            "new_rel_path": new_rel_path,
            "bbox_used": [target_rect.x0, target_rect.y0, target_rect.x1, target_rect.y1],
            "pixel_size": {"width": pix.width, "height": pix.height},
        }
    finally:
        doc.close()

    # Phase 3: Write back to DB — new session for the update.
    try:
        with session_scope(settings.database_url) as session:
            fig = session.get(PaperFigure, UUID(figure_id))
            if not fig:
                raise ValueError(f"Figure {figure_id} not found during write-back (race condition)")

            old_path = fig.image_path
            prov_entry = {
            "action": "recrop_figure",
            "strategy": strategy,
            "bbox": {"l": rendered_meta["bbox_used"][0], "t": rendered_meta["bbox_used"][1],
                     "r": rendered_meta["bbox_used"][2], "b": rendered_meta["bbox_used"][3],
                     "coord_origin": "TOPLEFT"},
            "pixel_size": rendered_meta["pixel_size"],
            "previous_path": old_path,
            "recropped_by": auth.source_prefix,
            }
            next_prov = list(fig.prov or []) if not isinstance(fig.prov, dict) else [fig.prov]
            next_prov.append(prov_entry)
            updated = session.execute(
                update(PaperFigure)
                .where(
                    PaperFigure.id == UUID(figure_id),
                    PaperFigure.write_version == fig_meta["write_version"],
                )
                .values(
                    image_path=rendered_meta["new_rel_path"],
                    crop_status="recropped",
                    crop_source=f"recrop:{strategy}:{auth.source_prefix}",
                    crop_confidence=0.5 if strategy == "ai_bbox" else 0.8,
                    prov=next_prov,
                    write_version=fig_meta["write_version"] + 1,
                )
            )
            if updated.rowcount != 1:
                raise ValueError("write_conflict:figure_version_stale")

            session.add(
                AuditLog(
                paper_id=UUID(fig_meta["paper_id"]),
                action="recrop_figure",
                source=auth.source_prefix,
                target_type="paper_figure",
                target_id=figure_id,
                payload={
                    "strategy": strategy,
                    "new_bbox": rendered_meta["bbox_used"],
                    "new_image_path": rendered_meta["new_rel_path"],
                    "old_image_path": old_path,
                },
                )
            )
            session.flush()

            result = {
            "figure_id": figure_id,
            "paper_id": fig_meta["paper_id"],
            "strategy": strategy,
            "new_image_path": rendered_meta["new_rel_path"],
            "bbox_used": rendered_meta["bbox_used"],
            "pixel_size": rendered_meta["pixel_size"],
            "crop_confidence": fig.crop_confidence,
            "status": "success",
            }
        return result
    except Exception:
        new_abs_path.unlink(missing_ok=True)
        raise


@mcp_server.tool(
    name="create_figure_from_bbox",
    description=(
        "Create a missing figure object by cropping the original PDF. "
        "Use this when read_paper_page/PDF evidence shows a figure exists but the parser did not create it. "
        "Inputs use PDF page coordinates [x0, y0, x1, y1] with TOPLEFT origin; use strategy='full_page' when bbox is uncertain."
    ),
)
def create_figure_from_bbox(
    paper_id: str,
    page: int,
    caption: str,
    figure_label: str | None = None,
    figure_role: str | None = None,
    content_summary: str | None = None,
    key_elements: list[Any] | None = None,
    strategy: str = "ai_bbox",
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("request_parse")
    if page < 1:
        raise ValueError("page must be >= 1")
    if strategy not in ("ai_bbox", "full_page"):
        raise ValueError("strategy must be 'ai_bbox' or 'full_page'")
    if strategy == "ai_bbox" and (not bbox or len(bbox) != 4):
        raise ValueError("bbox must be [x0, y0, x1, y1] when strategy='ai_bbox'")
    if not caption or not caption.strip():
        raise ValueError("caption is required")

    settings = get_settings()
    import fitz
    import uuid as _uuid

    pid = UUID(paper_id)
    with session_scope(settings.database_url) as session:
        paper = _ensure_paper_exists(session, pid)
        if not paper.pdf_path:
            raise ValueError("Associated paper PDF path not found")
        pdf_abs_path = resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=settings,
            trusted_persisted_reference=True,
        )
        if not pdf_abs_path or not pdf_abs_path.exists():
            raise ValueError(f"PDF file not found: {paper.pdf_path}")

    doc = fitz.open(str(pdf_abs_path))
    try:
        page_index = page - 1
        if page_index >= len(doc):
            raise ValueError(f"Page {page} is out of bounds for this PDF")
        pdf_page = doc[page_index]
        page_rect = pdf_page.rect
        if strategy == "full_page":
            target_rect = page_rect
        else:
            target_rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3]).intersect(page_rect)
        if target_rect.is_empty:
            raise ValueError("Calculated crop rectangle is empty or invalid")
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=target_rect, alpha=False)
        filename = f"{pid}_fig_{_uuid.uuid4().hex[:8]}.png"
        rel_path = f"{pid}/{filename}"
        abs_path = settings.storage_paths["figures"] / str(pid) / filename
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(abs_path))
        pixel_size = {"width": pix.width, "height": pix.height}
        bbox_used = [target_rect.x0, target_rect.y0, target_rect.x1, target_rect.y1]
    finally:
        doc.close()

    with session_scope(settings.database_url) as session:
        _ensure_paper_exists(session, pid)
        figure = PaperFigure(
            paper_id=pid,
            caption=caption.strip(),
            image_path=rel_path,
            page=page,
            figure_label=(figure_label or "").strip() or None,
            figure_role=(figure_role or "").strip() or None,
            content_summary=normalize_figure_content_summary(content_summary, caption),
            key_elements=normalize_figure_key_elements(key_elements)[0],
            crop_status="ai_created_crop",
            crop_source=f"create_figure_from_bbox:{strategy}:{auth.source_prefix}",
            crop_confidence=0.55 if strategy == "ai_bbox" else 0.35,
            prov=[
                {
                    "action": "create_figure_from_bbox",
                    "strategy": strategy,
                    "bbox": {
                        "l": bbox_used[0],
                        "t": bbox_used[1],
                        "r": bbox_used[2],
                        "b": bbox_used[3],
                        "coord_origin": "TOPLEFT",
                    },
                    "page_no": page,
                    "pixel_size": pixel_size,
                    "created_by": auth.source_prefix,
                    "evidence_policy": "AI-created crop; verify against the PDF page before using image-derived claims.",
                }
            ],
        )
        session.add(figure)
        session.flush()
        session.add(
            AuditLog(
                paper_id=pid,
                action="create_figure_from_bbox",
                source=auth.source_prefix,
                target_type="paper_figure",
                target_id=str(figure.id),
                payload={
                    "page": page,
                    "caption": figure.caption,
                    "figure_label": figure.figure_label,
                    "strategy": strategy,
                    "bbox": bbox_used,
                    "image_path": rel_path,
                    "pixel_size": pixel_size,
                },
            )
        )
        session.flush()
        return {
            "status": "success",
            "paper_id": paper_id,
            "figure_id": str(figure.id),
            "caption": figure.caption,
            "figure_label": figure.figure_label,
            "page": page,
            "image_path": rel_path,
            "bbox_used": bbox_used,
            "pixel_size": pixel_size,
            "crop_status": figure.crop_status,
        }


# ---------------------------------------------------------------------------
# Figure review & coverage tools
# ---------------------------------------------------------------------------


@mcp_server.tool(
    name="review_figure",
    description="Record and optionally apply a review verdict for a specific figure. Verdicts: verified (AI summary matches the image), needs_attention (summary is incomplete or misleading), incorrect (summary contradicts the image). For non-DFT figure metadata, IDE AI may directly update figure_role, content_summary, key_elements, and crop_status after checking the PDF/image evidence.",
)
def review_figure(
    figure_id: str,
    verdict: str,
    reasoning: str,
    figure_role: str | None = None,
    content_summary: str | None = None,
    key_elements: list[Any] | None = None,
    crop_status: str | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability_any("review_corrections", "review_dft")
    verdict_aliases = {"needs_attention": "needs_repair", "incorrect": "rejected"}
    normalized_verdict = verdict_aliases.get(verdict, verdict)
    if normalized_verdict not in ("verified", "needs_repair", "rejected"):
        raise ValueError("verdict must be one of: verified, needs_repair, rejected, needs_attention, incorrect")
    allowed_crop_status = {"candidate_crop", "recropped", "ai_created_crop", "needs_recrop", "needs_review", "caption_only", "noisy", "noise", "missing", "failed"}
    if crop_status is not None and crop_status not in allowed_crop_status:
        raise ValueError("crop_status must be one of: " + ", ".join(sorted(allowed_crop_status)))

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        fig = session.get(PaperFigure, UUID(figure_id))
        if not fig:
            raise ValueError(f"Figure {figure_id} not found")
        applied_updates: dict[str, Any] = {}
        if figure_role is not None:
            fig.figure_role = figure_role.strip() or None
            applied_updates["figure_role"] = fig.figure_role
        if content_summary is not None:
            fig.content_summary = normalize_figure_content_summary(content_summary, fig.caption)
            applied_updates["content_summary"] = fig.content_summary
        if key_elements is not None:
            fig.key_elements = normalize_figure_key_elements(key_elements)[0]
            applied_updates["key_elements"] = fig.key_elements
        if crop_status is not None:
            fig.crop_status = crop_status
            applied_updates["crop_status"] = fig.crop_status

        latest_review = session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "review_figure")
            .where(AuditLog.target_id == figure_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        ).first()
        latest_payload = latest_review.payload if latest_review is not None and isinstance(latest_review.payload, dict) else {}
        if latest_payload.get("verdict") == normalized_verdict and latest_payload.get("applied_updates", {}) == applied_updates:
            return {
                "figure_id": figure_id,
                "paper_id": str(fig.paper_id),
                "verdict": normalized_verdict,
                "reviewer": auth.source_prefix,
                "note_created": False,
                "applied_updates": applied_updates,
                "idempotent": True,
            }

        # Write structured review note (Blackboard pattern).
        note = PaperNote(
            paper_id=fig.paper_id,
            source=f"review_figure:{auth.source_prefix}",
            content=f"[Figure Review] Verdict: {normalized_verdict}\nReasoning: {reasoning}\nApplied updates: {applied_updates}",
            field_name="figure_review",
            page=fig.page,
            section_title=fig.caption[:250] if fig.caption else None,
            quoted_text=normalized_verdict,
        )
        session.add(note)
        session.flush()

        session.add(
            AuditLog(
                paper_id=fig.paper_id,
                action="review_figure",
                source=auth.source_prefix,
                target_type="paper_figure",
                target_id=figure_id,
                payload={
                    "verdict": normalized_verdict,
                    "requested_verdict": verdict,
                    "reasoning": reasoning,
                    "note_id": str(note.id),
                    "applied_updates": applied_updates,
                },
            )
        )

    return {
        "figure_id": figure_id,
        "paper_id": str(fig.paper_id),
        "verdict": normalized_verdict,
        "reviewer": auth.source_prefix,
        "note_created": True,
        "applied_updates": applied_updates,
        "idempotent": False,
    }


@mcp_server.tool(
    name="get_review_coverage",
    description="Show which figures, tables, and sections of a paper have been reviewed and which haven't. Aggregates review_figure verdicts, historical chart notes, and PaperCorrection records to produce a coverage report.",
)
def get_review_coverage(paper_id: str) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()

    with session_scope(settings.database_url) as session:
        pid = UUID(paper_id)
        _ensure_paper_exists(session, pid)

        # --- Figures ---
        all_figures = session.scalars(
            select(PaperFigure).where(PaperFigure.paper_id == pid)
        ).all()

        figure_logs = session.scalars(
            select(AuditLog)
            .where(AuditLog.paper_id == pid)
            .where(AuditLog.action.in_(["review_figure", "analyze_chart_auto_note"]))
        ).all()

        reviewed_fig_ids = set()
        fig_verdicts: dict[str, list[str]] = {}
        analyzed_fig_ids = set()

        for log in figure_logs:
            if log.action == "review_figure" and log.target_id:
                fig_id = str(log.target_id)
                reviewed_fig_ids.add(fig_id)
                payload = log.payload or {}
                verdict = payload.get("verdict", "unknown") if isinstance(payload, dict) else "unknown"
                fig_verdicts.setdefault(fig_id, []).append(f"{verdict} (by {log.source})")
            elif log.action == "analyze_chart_auto_note":
                payload = log.payload or {}
                if isinstance(payload, dict) and "figure_id" in payload:
                    analyzed_fig_ids.add(str(payload["figure_id"]))

        figure_report = []
        for fig in all_figures:
            fig_id_str = str(fig.id)
            cap = fig.caption or ""
            figure_report.append({
                "figure_id": fig_id_str,
                "caption": cap,
                "page": fig.page,
                "has_vlm_summary": bool(fig.content_summary),
                "analyzed_via_chart": fig_id_str in analyzed_fig_ids,
                "review_verdicts": fig_verdicts.get(fig_id_str, []),
                "review_status": "reviewed" if fig_id_str in reviewed_fig_ids else ("analyzed" if fig_id_str in analyzed_fig_ids else "unreviewed"),
            })

        # --- Tables ---
        all_tables = session.scalars(
            select(PaperTable).where(PaperTable.paper_id == pid)
        ).all()

        table_corr = session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == pid)
            .where(PaperCorrection.field_name == "table")
        ).all()

        reviewed_table_ids = {c.target_path for c in table_corr if c.status != "pending"}
        table_report = []
        for tbl in all_tables:
            table_report.append({
                "table_id": str(tbl.id),
                "caption": tbl.caption,
                "page": tbl.page,
                "review_status": "has_correction" if str(tbl.id) in reviewed_table_ids else "unreviewed",
            })

        # --- Sections ---
        all_sections = session.scalars(
            select(PaperSection).where(PaperSection.paper_id == pid)
        ).all()

        section_corr = session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == pid)
            .where(PaperCorrection.field_name.in_(["section", "title", "text"]))
        ).all()

        reviewed_section_ids = {c.target_path for c in section_corr if c.status != "pending"}
        section_report = []
        for sec in all_sections:
            section_report.append({
                "section_id": str(sec.id),
                "title": sec.section_title,
                "type": sec.section_type,
                "pages": f"{sec.page_start}-{sec.page_end}",
                "review_status": "has_correction" if str(sec.id) in reviewed_section_ids else "unreviewed",
            })

        external_audit_candidates = session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id == pid)
            .where(ExternalAnalysisCandidate.candidate_type == "external_audit_opinion")
            .order_by(ExternalAnalysisCandidate.created_at.desc())
        ).all()
        external_audit_source_distribution: Counter[str] = Counter()
        latest_external_audits: list[dict[str, Any]] = []
        for candidate in external_audit_candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            source = str(payload.get("source") or "unknown")
            external_audit_source_distribution[source] += 1
            latest_external_audits.append(
                {
                    "candidate_id": str(candidate.id),
                    "candidate_type": candidate.candidate_type,
                    "status": candidate.status,
                    "source": source,
                    "verdict": payload.get("verdict"),
                    "recommended_action": payload.get("recommended_action"),
                    "verification_status": payload.get("verification_status", "unverified"),
                    "normalized_payload": payload,
                    "created_at": str(candidate.created_at) if candidate.created_at else None,
                }
            )

        # --- Summary ---
        fig_reviewed = sum(1 for f in figure_report if f["review_status"] == "reviewed")
        fig_analyzed = sum(1 for f in figure_report if f["review_status"] == "analyzed")
        fig_unreviewed = sum(1 for f in figure_report if f["review_status"] == "unreviewed")

        return {
            "paper_id": paper_id,
            "figures": {
                "total": len(all_figures),
                "reviewed": fig_reviewed,
                "analyzed_only": fig_analyzed,
                "unreviewed": fig_unreviewed,
                "details": figure_report,
            },
            "tables": {
                "total": len(all_tables),
                "with_corrections": len(reviewed_table_ids),
                "unreviewed": len(all_tables) - len(reviewed_table_ids),
                "details": table_report,
            },
            "sections": {
                "total": len(all_sections),
                "with_corrections": len(reviewed_section_ids),
                "unreviewed": len(all_sections) - len(reviewed_section_ids),
                "details": section_report,
            },
            "external_audit_count": len(external_audit_candidates),
            "external_audit_source_distribution": dict(sorted(external_audit_source_distribution.items())),
            "external_audits": {
                "total": len(external_audit_candidates),
                "source_distribution": dict(sorted(external_audit_source_distribution.items())),
            },
            "latest_external_audits": latest_external_audits[:10],
        }


@mcp_server.tool(
    name="get_field_disputes",
    description="Find fields where multiple AIs or reviewers have proposed different values. Returns conflicting PaperCorrection records (including historically resolved ones marked as status='resolved') and figure review disagreements for the same paper.",
)
def get_field_disputes(paper_id: str) -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()

    with session_scope(settings.database_url) as session:
        pid = UUID(paper_id)
        _ensure_paper_exists(session, pid)

        # --- Correction disputes: same target_path, different proposed values ---
        # L-3 fix: include resolved corrections so later AIs see historical disputes
        all_corrections = session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == pid)
        ).all()

        # Group by target_path
        by_path: dict[str, list[PaperCorrection]] = {}
        for c in all_corrections:
            by_path.setdefault(c.target_path, []).append(c)

        # Find paths with >1 correction and differing proposed values
        correction_disputes = []
        for path, corrections in by_path.items():
            if len(corrections) < 2:
                continue
            # Check if proposed values actually differ
            values = set()
            for c in corrections:
                val = str(c.proposed_value) if c.proposed_value is not None else ""
                values.add(val)
            if len(values) <= 1:
                continue

            # Determine dispute status: active if any pending involved, else resolved
            has_pending = any(c.status == "pending" for c in corrections)
            dispute_status = "active" if has_pending else "resolved"

            # Build resolution info from approved corrections (if resolved)
            resolution = None
            if not has_pending:
                approved = [c for c in corrections if c.status == "approved"]
                if approved:
                    # Pick the most recent approved value as the resolution
                    latest = max(approved, key=lambda c: c.created_at)
                    resolution = {
                        "resolved_value": latest.proposed_value,
                        "resolved_by": latest.reviewed_by,
                        "resolved_at": str(latest.reviewed_at) if latest.reviewed_at else None,
                    }

            correction_disputes.append({
                "target_path": path,
                "status": dispute_status,
                "conflict_count": len(corrections),
                "resolution": resolution,
                "proposals": [
                    {
                        "correction_id": str(c.id),
                        "source": c.source,
                        "proposed_value": c.proposed_value,
                        "reason": c.reason,
                        "status": c.status,
                        "created_at": str(c.created_at),
                    }
                    for c in corrections
                ],
            })

        # --- Figure review disputes: conflicting verdicts for same figure ---
        figure_review_logs = session.scalars(
            select(AuditLog)
            .where(AuditLog.paper_id == pid)
            .where(AuditLog.action == "review_figure")
        ).all()

        # Group by figure_id
        by_figure_id: dict[str, list[AuditLog]] = {}
        for log in figure_review_logs:
            target = str(log.target_id)
            if target:
                by_figure_id.setdefault(target, []).append(log)

        figure_disputes = []
        for fig_id, logs in by_figure_id.items():
            verdicts = set()
            for log in logs:
                if isinstance(log.payload, dict) and "verdict" in log.payload:
                    verdicts.add(log.payload["verdict"])
            if len(verdicts) > 1:
                # Fetch figure caption for context
                fig = session.get(PaperFigure, UUID(fig_id))
                caption = fig.caption if fig else "unknown"
                figure_disputes.append({
                    "figure_id": fig_id,
                    "caption": caption,
                    "conflicting_verdicts": list(verdicts),
                    "reviews": [
                        {
                            "source": log.source,
                            "verdict": log.payload.get("verdict") if isinstance(log.payload, dict) else "unknown",
                            "reasoning": log.payload.get("reasoning") if isinstance(log.payload, dict) else "",
                            "created_at": str(log.created_at),
                        }
                        for log in logs
                    ],
                })

        return {
            "paper_id": paper_id,
            "correction_disputes": correction_disputes,
            "correction_dispute_count": len(correction_disputes),
            "figure_disputes": figure_disputes,
            "figure_dispute_count": len(figure_disputes),
            "total_disputes": len(correction_disputes) + len(figure_disputes),
        }


# ---------------------------------------------------------------------------
# export_ml_dataset — export verified data for machine learning
# ---------------------------------------------------------------------------

@mcp_server.tool(
    name="export_ml_dataset",
    description=(
        "Export verified DFT results and electrochemical performance data as a structured dataset "
        "for machine learning. DFT results are filtered through the safety gate (safe_verified_with_required_evidence) "
        "and include catalyst info, DFT settings, and normalized energy units. "
        "Electrochemical performance includes verified records only. "
        "Returns JSON by default; set format='csv' for CSV output."
    ),
)
def export_ml_dataset(
    paper_id: str | None = None,
    library_name: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    target_types: list[str] | None = None,
    format: str = "json",
    limit: int = 1000,
) -> dict[str, Any]:
    require_mcp_capability("export_data")
    require_mcp_exports_enabled()
    settings = get_settings()
    valid_targets = {"dft_results", "electrochemical_performance"}
    targets = [t for t in (target_types or ["dft_results"]) if t in valid_targets]
    if not targets:
        targets = ["dft_results"]
    normalized_library_name = normalize_library_name(library_name) if library_name is not None else None

    with session_scope(settings.database_url) as session:
        dft_data: dict | None = None
        ec_records: list[dict[str, Any]] = []

        # DFT results — reuse the same safety-gated export logic as the REST API
        if "dft_results" in targets:
            if format.lower() == "csv":
                csv_text, gate_summary = build_dft_csv_rows(
                    session,
                    library_name=normalized_library_name,
                    year_min=year_min,
                    year_max=year_max,
                    paper_id=UUID(paper_id) if paper_id else None,
                )
                dft_data = {
                    "metadata": {
                        "target_type": "dft_results",
                        "format": "csv",
                        "gate_summary": gate_summary,
                    },
                    "csv": csv_text,
                }
            else:
                dft_data = build_dft_ml_dataset(
                    session,
                    library_name=normalized_library_name,
                    year_min=year_min,
                    year_max=year_max,
                    paper_id=UUID(paper_id) if paper_id else None,
                    limit=max(1, min(limit, 5000)),
                )

        # Electrochemical performance — MCP-only feature (no REST equivalent yet)
        if "electrochemical_performance" in targets and format.lower() != "csv":
            paper_query = select(Paper)
            if paper_id:
                paper_query = paper_query.where(Paper.id == UUID(paper_id))
            if normalized_library_name:
                paper_query = paper_query.where(build_library_name_clause(Paper.library_name, normalized_library_name))
            if year_min is not None:
                paper_query = paper_query.where(Paper.year >= year_min)
            if year_max is not None:
                paper_query = paper_query.where(Paper.year <= year_max)
            papers = session.scalars(paper_query).all()
            paper_ids = [p.id for p in papers]

            ec_query = select(ElectrochemicalPerformance).where(ElectrochemicalPerformance.paper_id.in_(paper_ids))
            ec_query = ec_query.where(ElectrochemicalPerformance.validation_status == "verified")
            ec_query = ec_query.limit(max(1, min(limit, 5000)))
            for row in session.scalars(ec_query).all():
                paper = next((p for p in papers if p.id == row.paper_id), None)
                ec_records.append({
                    "target_type": "electrochemical_performance",
                    "paper_id": str(row.paper_id),
                    "paper_title": paper.title if paper else None,
                    "paper_year": paper.year if paper else None,
                    "paper_doi": paper.doi if paper else None,
                    "ec_id": str(row.id),
                    "sulfur_loading_mg_cm2": row.sulfur_loading_mg_cm2,
                    "sulfur_content_wt_percent": row.sulfur_content_wt_percent,
                    "electrolyte_sulfur_ratio": row.electrolyte_sulfur_ratio,
                    "capacity_value": row.capacity_value,
                    "cycle_number": row.cycle_number,
                    "rate": row.rate,
                    "decay_per_cycle": row.decay_per_cycle,
                    "evidence_text": row.evidence_text,
                    "validation_status": row.validation_status,
                })

        # Record export in audit log
        audit = AuditLog(
            action="export_ml_dataset",
            source="mcp",
            target_id=paper_id,
            payload={
                "library_name": library_name,
                "normalized_library_name": normalized_library_name,
                "year_min": year_min,
                "year_max": year_max,
                "target_types": targets,
                "format": format,
                "dft_record_count": len(dft_data.get("records", [])) if dft_data else 0,
                "ec_record_count": len(ec_records),
            },
        )
        session.add(audit)
        session.flush()

        # Assemble result
        result: dict[str, Any] = {
            "target_types": targets,
            "filters": {
                "paper_id": paper_id,
                "library_name": library_name,
                "normalized_library_name": normalized_library_name,
                "year_min": year_min,
                "year_max": year_max,
            },
        }

        if dft_data is not None:
            result["dft_results"] = dft_data
        if ec_records:
            result["electrochemical_performance"] = {
                "record_count": len(ec_records),
                "records": ec_records,
            }

        return result


# ---------------------------------------------------------------------------
# scan_duplicate_dois — find papers with duplicate DOIs
# ---------------------------------------------------------------------------

@mcp_server.tool(
    name="scan_duplicate_dois",
    description="Scan the database for papers that share the same DOI. Returns groups of duplicate papers.",
)
def scan_duplicate_dois() -> dict[str, Any]:
    require_mcp_capability("read_papers")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        duplicate_rows = session.execute(
            select(Paper.library_name, Paper.doi, func.count(Paper.id).label("cnt"))
            .where(Paper.doi.is_not(None))
            .group_by(Paper.library_name, Paper.doi)
        ).all()
        duplicate_counts: Counter[tuple[str, str], int] = Counter()
        for library_name, doi, count in duplicate_rows:
            duplicate_counts[(normalize_library_name(library_name), doi)] += count

        duplicates = []
        for (library_name, doi), count in sorted(duplicate_counts.items()):
            if count <= 1:
                continue
            paper_ids = session.scalars(
                select(Paper.id).where(
                    build_library_name_clause(Paper.library_name, library_name),
                    Paper.doi == doi,
                )
                .order_by(Paper.created_at.asc(), Paper.id.asc())
            ).all()
            duplicates.append(
                {
                    "library_name": library_name,
                    "doi": doi,
                    "count": count,
                    "paper_ids": [str(pid) for pid in paper_ids],
                }
            )

        return {
            "duplicate_groups_count": len(duplicates),
            "duplicates": duplicates
        }


# ---------------------------------------------------------------------------
# create_share_token — generate a read-only share link
# ---------------------------------------------------------------------------

@mcp_server.tool(
    name="create_share_token",
    description=(
        "Create a read-only share token that lets others view data via /api/share/{token}/... "
        "without needing MCP access. Scope can be 'all', 'library:{name}', or 'paper:{uuid}'. "
        "Optionally set expires_at for time-limited access. Requires the independent 'create_share_links' capability."
    ),
)
def create_share_token(
    scope: str = "all",
    expires_hours: int | None = None,
) -> dict[str, Any]:
    auth = require_mcp_capability("create_share_links")
    if scope != "all" and not scope.startswith(("library:", "paper:")):
        raise ValueError("Scope must be 'all', 'library:{name}', or 'paper:{uuid}'")
    if scope.startswith("library:") and not scope.split(":", 1)[1].strip():
        raise ValueError("Library scope requires a non-empty library name")

    import secrets
    token_str = secrets.token_urlsafe(32)

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        from datetime import timedelta
        if scope.startswith("library:"):
            library_name = scope.split(":", 1)[1].strip()
            if session.scalar(select(Paper.id).where(Paper.library_name == library_name).limit(1)) is None:
                raise ValueError("Library scope does not match an existing library")
        expires_at = None
        if expires_hours:
            expires_at = utcnow() + timedelta(hours=expires_hours)

        share = ShareToken(
            token=token_str,
            scope=scope,
            expires_at=expires_at,
            created_by=auth.source_prefix,
        )
        session.add(share)
        session.flush()

        # Use the dedicated share gateway when configured; never derive this
        # from untrusted request Host headers.
        base_url = (settings.share_public_base_url or "http://localhost:8000").rstrip("/")

        return {
            "token": token_str,
            "scope": scope,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "share_url": f"{base_url}/pages/share/index.html?token={token_str}",
            "api_base": f"{base_url}/api/share/{token_str}",
            "created_by": auth.source_prefix,
        }
