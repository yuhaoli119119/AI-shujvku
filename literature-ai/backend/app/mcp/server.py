from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from fastapi.concurrency import run_in_threadpool
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AuditLog, Paper, PaperCorrection, PaperNote, ParseJob
from app.db.session import session_scope
from app.mcp.auth import require_mcp_capability
from app.rag.retriever import Retriever
from app.schemas.mcp import MCPCorrectionDetailResponse, MCPCorrectionResponse, MCPNoteResponse, MCPParseJobResponse
from app.services.discovery_service import DiscoveryService
from app.services.external_analysis_service import (
    ExternalAnalysisNormalizedModel,
    ExternalAnalysisService,
    build_internal_ai_review_blob,
    sanitize_internal_corrections,
)
from app.services.local_pdf_service import LocalPdfService
from app.services.paper_ingestion import PaperIngestionService
from app.services.paper_query import PaperQueryService
from app.services.review_service import ReviewService

mcp_server = FastMCP(
    get_settings().mcp_server_name,
    json_response=True,
    streamable_http_path="/",
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


@mcp_server.tool(name="query_papers", description="Query parsed papers in the local library.")
def query_papers(
    q: str | None = None,
    year: int | None = None,
    journal: str | None = None,
    has_dft_results: bool | None = None,
    has_writing_cards: bool | None = None,
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


@mcp_server.tool(name="propose_correction", description="Submit a correction proposal for curator review.")
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
def approve_correction(correction_id: str) -> dict[str, Any]:
    auth = require_mcp_capability("review_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        item = ReviewService(session).approve_correction(UUID(correction_id), reviewer=auth.source_prefix)
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
                existing = session.scalar(select(Paper).where(Paper.doi == doi))
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
                            payload={"identifier": identifier, "doi": doi},
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
# External AI integration tools (retrieve_evidence, review_paper,
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
        retriever = Retriever(session, embedding_dimension=settings.embedding_dimension)
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


_REVIEW_SYSTEM_PROMPT = (
    "You are an internal scientific curation agent for a literature database. "
    "Review the provided parsed-paper bundle and return only high-confidence structured output. "
    "Use review_notes for useful summaries or caveats, correction_proposals for concrete field fixes, "
    "and supporting_papers only when an existing linked paper can be inferred from DOI/title clues already present. "
    "Do not invent evidence, identifiers, values, or target paths. Prefer leaving arrays empty over guessing. "
    "For top-level paper fields, only use these correction field_name values: doi, title, year, journal, authors, abstract, oa_status, license. "
    "For those top-level fields, set target_path exactly equal to field_name. "
    "For structured corrections, only use field_name values from dft_results, mechanism_claims, electrochemical_performance, catalyst_samples, dft_settings, writing_cards, "
    "and set target_path strictly as <collection>:<row_id>:<field> using row ids that already exist in the provided bundle."
)


@mcp_server.tool(
    name="review_paper",
    description="Trigger an AI-powered deep review of a paper using the configured LLM (e.g. DeepSeek). The AI will analyze the paper's extracted data and produce structured review notes, correction proposals, and relationship suggestions. Results are stored as candidates and can be materialized later.",
)
async def review_paper(
    paper_id: str,
    auto_apply: bool = False,
    source_label: str = "mcp_review",
) -> dict[str, Any]:
    require_mcp_capability("propose_corrections")
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        detail = PaperQueryService(session).get_paper_detail(UUID(paper_id))
        if not detail:
            raise ValueError("Paper not found")

        service = ExternalAnalysisService(session=session, settings=settings)
        if not service.llm.is_configured():
            raise ValueError("Internal AI is not configured. Set LITAI_WRITER_API_KEY and LITAI_WRITER_API_BASE.")

        review_blob = build_internal_ai_review_blob(detail)
        user_prompt = (
            "Analyze this parsed literature record and extraction output. "
            "Identify any clear normalization notes, corrections, and supporting-paper relationships.\n\n"
            f"{review_blob}"
        )

        normalized = await run_in_threadpool(
            service.llm.structured_extract, _REVIEW_SYSTEM_PROMPT, user_prompt, ExternalAnalysisNormalizedModel
        )
        if normalized is None:
            raise ValueError("AI review failed to produce structured output")

        normalized = sanitize_internal_corrections(normalized)

        run = service.import_run(
            paper_id=UUID(paper_id),
            source="mcp_review",
            source_label=source_label,
            raw_text=None,
            raw_payload=normalized.model_dump(mode="json"),
        )

        created_notes = 0
        created_corrections = 0
        created_relationships = 0
        auto_applied_corrections = 0
        skipped_candidates = 0

        if auto_apply:
            materialized = service.materialize_candidates(
                run_id=run.id,
                candidate_ids=None,
                explicit_all=True,
                created_by="mcp_review",
            )
            created_notes = materialized.created_notes
            created_corrections = materialized.created_corrections
            created_relationships = materialized.created_relationships
            skipped_candidates = materialized.skipped_candidates
            if materialized.created_corrections:
                reviewer = ReviewService(session)
                correction_candidate_ids = [
                    item.materialized_target_id
                    for item in service.list_candidates(run.id)
                    if item.materialized_target_type == "paper_correction" and item.materialized_target_id
                ]
                for correction_id in correction_candidate_ids:
                    try:
                        reviewer.approve_correction(UUID(str(correction_id)), reviewer="mcp_review")
                        auto_applied_corrections += 1
                    except ValueError:
                        continue

        session.commit()
        return {
            "run_id": str(run.id),
            "mapping_status": run.mapping_status,
            "created_notes": created_notes,
            "created_corrections": created_corrections,
            "created_relationships": created_relationships,
            "auto_applied_corrections": auto_applied_corrections,
            "skipped_candidates": skipped_candidates,
        }


@mcp_server.tool(
    name="import_analysis",
    description="Import analysis results from an external AI agent (e.g. Cursor, DeepSeek chat, Claude) into the library. Supports free-text or structured JSON. The system will auto-normalize the input into structured notes, corrections, and relationships.",
)
def import_analysis(
    paper_id: str,
    source: str,
    source_label: str = "",
    raw_text: str | None = None,
    raw_payload: dict | None = None,
) -> dict[str, Any]:
    require_mcp_capability("propose_corrections")
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
        candidates = service.list_candidates(run.id)
        session.commit()
        return {
            "run_id": str(run.id),
            "mapping_status": run.mapping_status,
            "mapping_error": run.mapping_error,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "id": str(c.id),
                    "type": c.candidate_type,
                    "confidence": c.confidence,
                    "status": c.status,
                    "summary": (c.normalized_payload or {}).get("content") or (c.normalized_payload or {}).get("reason", ""),
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
            entry: dict[str, Any] = {"id": p["id"], "title": p["title"], "year": p["year"], "paper_type": p["paper_type"]}
            for f in active_fields:
                entry[f] = p.get(f, [])
            comparison.append(entry)

        return {
            "paper_count": len(comparison),
            "compared_fields": sorted(active_fields),
            "papers": comparison,
        }
