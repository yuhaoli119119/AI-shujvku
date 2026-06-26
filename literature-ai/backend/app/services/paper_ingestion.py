from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AuditLog, Paper, PaperChunk, PaperFigure, PaperRelationship, PaperSection, PaperTable, FigureDataPoint, EvidenceSpan
from app.parsers.body_boundary_cleaner import BodyBoundaryCleaner, BoundaryCleanupPlan
from app.parsers.docling_parser import DoclingParser
from app.parsers.grobid_parser import GrobidParseResult, GrobidParser
from app.schemas.documents import UnifiedFigure, UnifiedPaperDocument, UnifiedSection, UnifiedTable
from app.services.artifact_store import ArtifactStore
from app.security.files import validate_local_ingest_pdf
from app.services.pdf_image_extractor import PdfImageExtractor
from app.services.embedding import EmbeddingUnavailableError, get_embedding_service
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.extraction_pipeline import ExtractionPipelineService
from app.services.paper_identity import PaperIdentityService
from app.services.paper_codes import ensure_paper_codes, next_supplementary_paper_code
from app.services.paper_serials import renumber_library_papers_by_year
from app.services.parse_quality_auditor import ParseQualityAuditor
from app.services.paper_workbench_service import PaperWorkbenchService
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference, resolve_persisted_artifact_path
from app.utils.library_names import DEFAULT_LIBRARY_NAME
from app.utils.text_cleaning import normalize_text_tree

logger = logging.getLogger(__name__)

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


class PaperConflictError(RuntimeError):
    def __init__(self, paper: Paper, message: str = "Paper already exists") -> None:
        super().__init__(message)
        self.paper = paper


class PaperIdentityMismatchError(RuntimeError):
    def __init__(
        self,
        *,
        status: str,
        target_paper: Paper,
        incoming: dict[str, Any],
        match_report: dict[str, Any],
        message: str = "Paper identity needs confirmation",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.target_paper = target_paper
        self.incoming = incoming
        self.match_report = match_report


class PaperIngestionService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.artifacts = ArtifactStore(settings)
        self.grobid_parser = GrobidParser(settings.grobid_url)
        self.docling_parser = DoclingParser(settings)
        self.embedding = get_embedding_service(
            provider=settings.embedding_provider,
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
        self.identity = PaperIdentityService()
        self.extraction_pipeline = ExtractionPipelineService(
            session=session,
            settings=settings,
        )
        self.locators = EvidenceLocatorService(session)
        self.workbench = PaperWorkbenchService(session=session, settings=settings)

    async def ingest_upload(
        self,
        file: UploadFile,
        external_metadata: dict[str, Any] | None = None,
        library_name: str | None = None,
        attach_to_paper_id: UUID | None = None,
        supplementary_for_paper_id: UUID | None = None,
        confirm_identity_mismatch: bool = False,
    ) -> Paper:
        target_name = f"{uuid.uuid4()}.pdf"
        stored_pdf = await self.artifacts.save_upload(file, target_name)
        return await self.ingest_pdf(
            source_path=stored_pdf,
            original_filename=file.filename or target_name,
            copy_pdf=False,
            external_metadata=external_metadata,
            source_reference=None,
            library_name=library_name,
            attach_to_paper_id=attach_to_paper_id,
            supplementary_for_paper_id=supplementary_for_paper_id,
            confirm_identity_mismatch=confirm_identity_mismatch,
            ingest_source="uploaded",
        )

    async def ingest_pdf(
        self,
        source_path: Path,
        original_filename: str,
        copy_pdf: bool = True,
        external_metadata: dict[str, Any] | None = None,
        source_reference: str | None = None,
        library_name: str | None = None,
        attach_to_paper_id: UUID | None = None,
        supplementary_for_paper_id: UUID | None = None,
        confirm_identity_mismatch: bool = False,
        ingest_source: str | None = None,
    ) -> Paper:
        if attach_to_paper_id is not None and supplementary_for_paper_id is not None:
            raise ValueError("supplementary_upload_cannot_attach_to_existing_placeholder")
        if ingest_source == "local_pdf":
            source_path = validate_local_ingest_pdf(source_path, self.settings)
        if copy_pdf:
            stored_pdf = self.artifacts.save_pdf_copy(source_path, f"{uuid.uuid4()}_{original_filename}")
        else:
            stored_pdf = source_path
        quality_report = PaperWorkbenchService.assess_pdf_path(stored_pdf, self.settings)
        parse_allowed = self._quality_allows_initial_parse(quality_report)
        if not parse_allowed:
            grobid_result = GrobidParseResult(
                metadata={"title": original_filename, **(external_metadata or {})},
                abstract=str((external_metadata or {}).get("abstract") or ""),
                sections=[],
                references=[],
                tei_xml="",
            )
            unified = self._build_quality_blocked_document(
                stored_pdf=stored_pdf,
                original_filename=original_filename,
                grobid_result=grobid_result,
                external_metadata=external_metadata,
            )
        else:
            try:
                grobid_result = await self.grobid_parser.parse_pdf(stored_pdf)
            except Exception as exc:
                logger.warning("Grobid parsing failed for %s: %s", original_filename, exc, exc_info=True)
                grobid_result = GrobidParseResult(
                    metadata={"title": original_filename},
                    abstract="",
                    sections=[],
                    references=[],
                    tei_xml="",
                )
            try:
                docling_result = await self.docling_parser.parse_pdf(
                    stored_pdf,
                    document_timeout=self._docling_document_timeout_for(
                        supplementary=bool(supplementary_for_paper_id),
                    ),
                )
                unified = await self._build_unified_document(stored_pdf, grobid_result, docling_result)
            except Exception as exc:
                logger.error("Docling parsing or unified building failed for %s: %s", original_filename, exc, exc_info=True)
                library = (library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME
                ext = external_metadata or {"title": original_filename}
                paper = Paper(
                    library_name=library,
                    doi=self.identity.normalize_doi(ext.get("doi")),
                    title=ext.get("title") or original_filename,
                    year=ext.get("year"),
                    pdf_path=self._artifact_ref(stored_pdf, category="pdf") or str(stored_pdf),
                    source_path=source_reference,
                    oa_status="parse_failed",
                    workflow_status="Needs_Human_Confirmation",
                    pdf_quality_status=quality_report.get("quality_status"),
                    pdf_quality_score=quality_report.get("quality_score"),
                    pdf_quality_report=quality_report,
                )
                self.session.add(paper)
                self.session.flush()
                ensure_paper_codes(self.session, [paper])
                self.session.commit()
                self.session.refresh(paper)
                raise RuntimeError(f"docling_parse_failed:{paper.id} {exc}") from exc

        if quality_report.get("needs_human_confirmation"):
            unified = self._document_metadata_only(unified)

        if (
            not external_metadata
            and self.settings.auto_enrich_ingested_metadata
            and self._needs_metadata_enrichment(unified.metadata)
        ):
            doi = unified.metadata.get("doi")
            if doi:
                from app.services.discovery_service import DiscoveryService
                from fastapi.concurrency import run_in_threadpool

                try:
                    svc = DiscoveryService()
                    _, external_metadata = await asyncio.wait_for(
                        run_in_threadpool(svc.fetch_metadata, doi),
                        timeout=max(0.1, float(self.settings.metadata_enrichment_timeout_seconds)),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Auto-enrichment via discovery timed out for DOI %s after %.1fs",
                        doi,
                        self.settings.metadata_enrichment_timeout_seconds,
                    )
                except Exception as exc:
                    logger.warning("Auto-enrichment via discovery failed for DOI %s: %s", doi, exc)

        library = (library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME
        identity_metadata = self._build_identity_metadata(unified, external_metadata, source_reference)
        doi = identity_metadata.get("doi")
        title = identity_metadata.get("title")
        year = identity_metadata.get("year")
        arxiv_id = identity_metadata.get("arxiv_id")
        oa_status = (
            "quality_blocked"
            if not parse_allowed
            else ingest_source or self._infer_oa_status(source_reference=source_reference, copy_pdf=copy_pdf)
        )

        if attach_to_paper_id is not None:
            target_paper = self.session.get(Paper, attach_to_paper_id)
            if target_paper is None:
                raise ValueError("Paper not found")
            if target_paper.pdf_path and target_paper.oa_status != "metadata_only":
                raise PaperConflictError(target_paper, "Paper already has an attached PDF")
            match_report = self.identity.identity_match_report(
                self.identity.metadata_for_paper(target_paper),
                identity_metadata,
            )
            if match_report["decision"] == "doi_conflict":
                raise PaperIdentityMismatchError(
                    status="identity_mismatch",
                    target_paper=target_paper,
                    incoming=identity_metadata,
                    match_report=match_report,
                    message="Incoming PDF DOI does not match target paper DOI",
                )
            if match_report["decision"] == "low_confidence" and not confirm_identity_mismatch:
                raise PaperIdentityMismatchError(
                    status="needs_confirmation",
                    target_paper=target_paper,
                    incoming=identity_metadata,
                    match_report=match_report,
                )
            conflict = self._find_conflicting_paper(
                doi=doi,
                arxiv_id=arxiv_id,
                library_name=target_paper.library_name,
                exclude_paper_id=target_paper.id,
            )
            if conflict is not None:
                raise PaperConflictError(conflict, "Another paper with the same identity already has a PDF")
            paper = self._merge_into_existing_paper(
                target_paper,
                unified,
                external_metadata,
                source_reference=source_reference,
                oa_status=oa_status,
                quality_report=quality_report,
            )
            ingest_status = "merged_confirmed" if match_report["decision"] == "low_confidence" else "merged"
            setattr(paper, "_ingest_status", ingest_status)
            return paper

        if supplementary_for_paper_id is not None:
            main_paper = self.session.get(Paper, supplementary_for_paper_id)
            if main_paper is None:
                raise ValueError("Paper not found")
            existing = self._find_existing_supplementary_by_hash(main_paper, stored_pdf)
            if existing is not None:
                setattr(existing, "_ingest_status", "already_linked")
                return existing
            paper = self._persist_supplementary(
                main_paper=main_paper,
                document=unified,
                external_metadata=external_metadata,
                source_reference=source_reference,
                oa_status=oa_status,
                quality_report=quality_report,
            )
            setattr(paper, "_ingest_status", "completed")
            return paper

        placeholder = self.identity.find_metadata_placeholder(
            self.session,
            doi=doi,
            title=title,
            year=year,
            arxiv_id=arxiv_id,
            library_name=library,
        )
        if placeholder is not None:
            paper = self._merge_into_existing_paper(
                placeholder,
                unified,
                external_metadata,
                source_reference=source_reference,
                oa_status=oa_status,
                quality_report=quality_report,
            )
            setattr(paper, "_ingest_status", "merged")
            return paper

        conflict = self._find_conflicting_paper(
            doi=doi,
            arxiv_id=arxiv_id,
            library_name=library,
        )
        if conflict is not None:
            raise PaperConflictError(conflict, "Paper already exists with the same DOI or arXiv identity")

        paper = self._persist(
            unified,
            external_metadata,
            source_reference=source_reference,
            library_name=library,
            oa_status=oa_status,
            quality_report=quality_report,
        )
        setattr(paper, "_ingest_status", "completed")
        return paper

    async def reparse_existing_paper(self, paper_id: UUID) -> Paper:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise ValueError("Paper not found")
        stored_pdf = resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=self.settings,
            must_exist=False,
            trusted_persisted_reference=True,
        ) or Path(paper.pdf_path or "")
        if not stored_pdf.exists():
            raise FileNotFoundError("Paper PDF is missing")

        quality_report = PaperWorkbenchService.assess_pdf_path(stored_pdf, self.settings)
        parse_allowed = self._quality_allows_initial_parse(quality_report)
        if not parse_allowed:
            grobid_result = GrobidParseResult(
                metadata={
                    "title": paper.title or stored_pdf.name,
                    "doi": paper.doi,
                    "year": paper.year,
                    "journal": paper.journal,
                    "authors": paper.authors or [],
                },
                abstract=paper.abstract or "",
                sections=[],
                references=[],
                tei_xml="",
            )
            unified = self._build_quality_blocked_document(
                stored_pdf=stored_pdf,
                original_filename=paper.title or stored_pdf.name,
                grobid_result=grobid_result,
                external_metadata=None,
            )
            self._clear_document_entities(paper.id)
            self.extraction_pipeline._delete_existing_stage2(paper.id)
            paper.comprehensive_analysis = None
            paper.oa_status = "quality_blocked"
        else:
            try:
                grobid_result = await self.grobid_parser.parse_pdf(stored_pdf)
            except Exception as exc:
                logger.warning("Grobid parsing failed during reparse for %s: %s", paper_id, exc, exc_info=True)
                grobid_result = GrobidParseResult(
                    metadata={"title": paper.title or stored_pdf.name},
                    abstract=paper.abstract or "",
                    sections=[],
                    references=[],
                    tei_xml="",
                )
            try:
                docling_result = await self.docling_parser.parse_pdf(
                    stored_pdf,
                    document_timeout=self._docling_document_timeout_for(
                        supplementary=self._is_supplementary_paper(paper),
                    ),
                )
                unified = await self._build_unified_document(stored_pdf, grobid_result, docling_result)
            except Exception as exc:
                logger.error("Docling reparse failed for %s: %s", paper_id, exc, exc_info=True)
                paper.oa_status = "parse_failed"
                paper.workflow_status = "Needs_Human_Confirmation"
                self.workbench.apply_quality_report(paper, quality_report)
                self.session.add(paper)
                self.session.commit()
                self.session.refresh(paper)
                try:
                    self.workbench.prepare_paper_workspace(paper.id)
                except Exception:
                    logger.exception("Failed to refresh Codex workspace after reparse failure for paper %s", paper.id)
                raise RuntimeError(f"docling_parse_failed:{paper.id} {exc}") from exc

        if quality_report.get("needs_human_confirmation"):
            unified = self._document_metadata_only(unified)
        else:
            if paper.workflow_status == "Needs_Human_Confirmation":
                paper.workflow_status = "Quality_Checked"
            if str(paper.oa_status or "").strip().lower() in {"parse_failed", "quality_blocked"}:
                paper.oa_status = "reparsed"

        reparsed = self._merge_into_existing_paper(
            paper,
            unified,
            external_metadata=None,
            source_reference=paper.source_path,
            oa_status=paper.oa_status,
            quality_report=quality_report,
        )
        setattr(reparsed, "_ingest_status", "reparsed")
        return reparsed

    @staticmethod
    def _quality_allows_initial_parse(quality_report: dict[str, Any]) -> bool:
        return bool(
            quality_report.get("parse_allowed")
            and quality_report.get("quality_status") in {"A_text_readable", "B_text_partial"}
        )

    def ingest_pdf_sync(self, source_path: Path, original_filename: str) -> Paper:
        import asyncio

        return asyncio.run(self.ingest_pdf(source_path=source_path, original_filename=original_filename))

    def ingest_metadata_only(
        self,
        external_metadata: dict[str, Any],
        identifier: str | None = None,
        library_name: str | None = None,
        source_reference: str | None = None,
    ) -> Paper:
        library = (library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME
        paper = self.identity.upsert_metadata_only(
            self.session,
            external_metadata=external_metadata or {},
            identifier=identifier,
            library_name=library,
            source_reference=source_reference,
            classify_callback=self.extraction_pipeline._rule_based_classify,
        )
        ensure_paper_codes(self.session, [paper])
        renumber_library_papers_by_year(self.session, paper.library_name)
        self.session.commit()
        self.session.refresh(paper)
        return paper

    def _build_quality_blocked_document(
        self,
        *,
        stored_pdf: Path,
        original_filename: str,
        grobid_result: GrobidParseResult,
        external_metadata: dict[str, Any] | None,
    ) -> UnifiedPaperDocument:
        metadata = dict(grobid_result.metadata or {})
        metadata.update({key: value for key, value in (external_metadata or {}).items() if value not in (None, "", [], {})})
        metadata.setdefault("title", original_filename)
        return UnifiedPaperDocument(
            metadata=metadata,
            abstract=str((external_metadata or {}).get("abstract") or grobid_result.abstract or ""),
            sections=[],
            tables=[],
            figures=[],
            references=list(grobid_result.references or []),
            markdown="",
            tei_xml=grobid_result.tei_xml or "",
            docling_json={},
            source_pdf_path=stored_pdf,
            tei_path=None,
            markdown_path=None,
            docling_json_path=None,
        )

    @staticmethod
    def _document_metadata_only(document: UnifiedPaperDocument) -> UnifiedPaperDocument:
        return document.model_copy(
            update={
                "sections": [],
                "tables": [],
                "figures": [],
                "references": [],
                "markdown": "",
                "docling_json": {},
            }
        )

    async def _build_unified_document(self, stored_pdf: Path, grobid_result, docling_result) -> UnifiedPaperDocument:
        normalized_metadata = normalize_text_tree(grobid_result.metadata) or {}
        normalized_abstract = normalize_text_tree(grobid_result.abstract) or ""
        normalized_sections = normalize_text_tree(grobid_result.sections) or []
        normalized_references = normalize_text_tree(grobid_result.references) or []
        normalized_markdown = normalize_text_tree(docling_result.markdown) or ""
        normalized_payload = normalize_text_tree(docling_result.json_payload) or {}
        normalized_tables = normalize_text_tree(docling_result.tables) or []
        normalized_figures = normalize_text_tree(docling_result.figures) or []
        normalized_page_blocks = normalize_text_tree(docling_result.page_blocks) or []

        parse_blocked = bool(normalized_payload.get("parse_blocked"))
        parse_quality = normalized_payload.get("parse_quality") if isinstance(normalized_payload.get("parse_quality"), dict) else {}
        cleanup_plan = BoundaryCleanupPlan.from_metadata(parse_quality.get("boundary_cleanup"))
        if not cleanup_plan.removable_signatures and normalized_page_blocks:
            cleanup_plan = BodyBoundaryCleaner.analyze(normalized_page_blocks)
            parse_quality["boundary_cleanup"] = cleanup_plan.to_metadata()
            normalized_payload["parse_quality"] = parse_quality

        # GROBID sections are independent of Docling parse availability. Apply
        # only exact boundary signatures confirmed from Docling page analysis.
        normalized_sections = BodyBoundaryCleaner.clean_sections(normalized_sections, cleanup_plan)

        if parse_blocked:
            normalized_markdown = ""
            normalized_page_blocks = []
            normalized_tables = []
            normalized_figures = []
            normalized_payload["pages"] = []
            normalized_payload["texts"] = []
        else:
            normalized_markdown = BodyBoundaryCleaner.clean_text(normalized_markdown, cleanup_plan)
            normalized_page_blocks = BodyBoundaryCleaner.clean_page_blocks(normalized_page_blocks, cleanup_plan)
            if isinstance(normalized_payload.get("pages"), list):
                normalized_payload["pages"] = normalized_page_blocks
            for item in normalized_payload.get("texts") or []:
                if isinstance(item, dict) and item.get("text"):
                    item["text"] = BodyBoundaryCleaner.clean_text(str(item["text"]), cleanup_plan)

        if self._is_placeholder_title(normalized_metadata.get("title"), stored_pdf):
            derived_title = self._derive_title_from_docling(normalized_markdown, normalized_page_blocks)
            if derived_title:
                normalized_metadata["title"] = derived_title
        if not normalized_metadata.get("doi"):
            derived_doi = self._derive_doi_from_text(normalized_markdown)
            if derived_doi:
                normalized_metadata["doi"] = derived_doi

        tei_name = f"{stored_pdf.stem}.tei.xml"
        json_name = f"{stored_pdf.stem}.docling.json"
        markdown_name = f"{stored_pdf.stem}.md"

        tei_path = self.artifacts.write_text("tei", tei_name, grobid_result.tei_xml)
        json_path = self.artifacts.write_json("docling_json", json_name, normalized_payload)
        markdown_path = self.artifacts.write_text("markdown", markdown_name, normalized_markdown)

        sections = [
            UnifiedSection(**section)
            for section in normalized_sections
            if section.get("text")
        ]

        docling_headers = self._extract_docling_section_headers(
            normalized_payload,
            document_title=normalized_metadata.get("title"),
        )
        sections = self._repair_section_metadata_from_docling(sections, docling_headers)

        if not sections and normalized_page_blocks:
            sections = [
                UnifiedSection(
                    section_title=f"Page {block.get('page', index)}",
                    section_type="body",
                    text=block.get("text", ""),
                    page_start=block.get("page", index),
                    page_end=block.get("page", index),
                )
                for index, block in enumerate(normalized_page_blocks, start=1)
                if block.get("text")
            ]

        tables = ParseQualityAuditor.clean_tables([UnifiedTable(**table) for table in normalized_tables])
        figures = [UnifiedFigure(**figure) for figure in normalized_figures]
        # 过滤掉没有 caption 的图片（logo、CrossMark 等装饰图），只保留学术图片
        # 第二道防线：docling_parser 已做黑名单过滤，这里补充短 caption 和纯序号过滤
        _short_caption_re = re.compile(r'^fig\.?\s*\d+\.?\s*$', re.IGNORECASE)
        filtered_figures = []
        for fig in figures:
            if not fig.caption or not fig.caption.strip():
                continue
            if _short_caption_re.match(fig.caption.strip()):
                fig.figure_role = "caption_incomplete"
            filtered_figures.append(fig)
        figures = filtered_figures
        figures = ParseQualityAuditor.clean_figures_before_extraction(figures)

        PdfImageExtractor.extract_figures(
            pdf_path=stored_pdf,
            figures=figures,
            output_dir=self.artifacts.settings.storage_paths["figures"]
        )
        figures = ParseQualityAuditor.clean_figures_after_extraction(
            figures,
            self.artifacts.settings.storage_paths["figures"],
        )

        # Web-side model figure analysis is disabled. Keep extracted crops and
        # captions for IDE/MCP AI or human review instead of calling a backend model.

        return UnifiedPaperDocument(
            metadata=normalized_metadata,
            abstract=normalized_abstract,
            sections=sections,
            tables=tables,
            figures=figures,
            references=normalized_references,
            markdown=normalized_markdown,
            tei_xml=grobid_result.tei_xml,
            docling_json=normalized_payload,
            source_pdf_path=stored_pdf,
            tei_path=tei_path,
            markdown_path=markdown_path,
            docling_json_path=json_path,
        )

    @staticmethod
    def _extract_docling_section_headers(
        payload: dict[str, Any],
        *,
        document_title: str | None,
    ) -> list[dict[str, Any]]:
        headers: list[dict[str, Any]] = []
        title_norm = PaperIngestionService._normalize_heading_text(document_title)
        seen: set[tuple[str, int | None]] = set()
        for item in payload.get("texts") or []:
            if not isinstance(item, dict) or item.get("label") != "section_header":
                continue
            text = PaperIngestionService._compact_heading_text(item.get("text"))
            if not text:
                continue
            if title_norm and PaperIngestionService._normalize_heading_text(text) == title_norm:
                continue
            prov = item.get("prov") or []
            page_no = None
            if prov and isinstance(prov[0], dict):
                page_no = prov[0].get("page_no")
            key = (PaperIngestionService._normalize_heading_text(text), int(page_no) if page_no is not None else None)
            if key in seen:
                continue
            seen.add(key)
            headers.append(
                {
                    "text": text,
                    "page_no": int(page_no) if page_no is not None else None,
                    "level": item.get("level"),
                }
            )
        return headers

    @classmethod
    def _repair_section_metadata_from_docling(
        cls,
        sections: list[UnifiedSection],
        docling_headers: list[dict[str, Any]],
    ) -> list[UnifiedSection]:
        if not sections or not docling_headers:
            return sections
        repaired: list[UnifiedSection] = []
        used_headers: set[int] = set()
        for section in sections:
            payload = section.model_dump()
            title = cls._compact_heading_text(payload.get("section_title"))
            replacement = cls._matching_docling_header(title, docling_headers, used_headers)
            if title and replacement and cls._should_replace_section_title(title, replacement["text"]):
                payload["section_title"] = replacement["text"]
                heading_path = list(payload.get("heading_path") or [])
                if heading_path:
                    heading_path[-1] = replacement["text"]
                else:
                    heading_path = [replacement["text"]]
                payload["heading_path"] = heading_path
                if payload.get("section_level") is None:
                    payload["section_level"] = replacement.get("level")
                if payload.get("page_start") is None:
                    payload["page_start"] = replacement.get("page_no")
                if payload.get("page_end") is None:
                    payload["page_end"] = replacement.get("page_no")
                used_headers.add(replacement["index"])
            elif cls._is_placeholder_section_title(title):
                payload["section_title"] = None
                payload["heading_path"] = []
            repaired.append(UnifiedSection(**payload))
        return repaired

    @classmethod
    def _matching_docling_header(
        cls,
        section_title: str | None,
        docling_headers: list[dict[str, Any]],
        used_headers: set[int],
    ) -> dict[str, Any] | None:
        if not section_title:
            return None
        section_norm = cls._normalize_heading_text(section_title)
        best: dict[str, Any] | None = None
        best_score = 0
        for index, header in enumerate(docling_headers):
            if index in used_headers:
                continue
            header_text = cls._compact_heading_text(header.get("text"))
            if not header_text:
                continue
            header_norm = cls._normalize_heading_text(header_text)
            score = 0
            if header_norm == section_norm:
                score = 80
            elif header_norm.startswith(section_norm) and len(header_norm) > len(section_norm) + 8:
                score = 100
            elif section_norm.startswith(header_norm) and len(section_norm) > len(header_norm) + 8:
                score = 40
            if score > best_score:
                best_score = score
                best = {**header, "index": index}
        return best if best_score >= 90 else None

    @classmethod
    def _should_replace_section_title(cls, current_title: str | None, candidate_title: str | None) -> bool:
        if not candidate_title:
            return False
        if cls._is_placeholder_section_title(current_title):
            return True
        current_norm = cls._normalize_heading_text(current_title)
        candidate_norm = cls._normalize_heading_text(candidate_title)
        return candidate_norm.startswith(current_norm) and len(candidate_norm) > len(current_norm) + 8

    @staticmethod
    def _is_placeholder_section_title(title: str | None) -> bool:
        text = PaperIngestionService._compact_heading_text(title)
        if not text:
            return True
        return bool(re.fullmatch(r"Section\s+\d+", text, flags=re.IGNORECASE))

    @staticmethod
    def _compact_heading_text(value: Any) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text or None

    @staticmethod
    def _normalize_heading_text(value: Any) -> str:
        text = PaperIngestionService._compact_heading_text(value) or ""
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    def _persist(
        self,
        document: UnifiedPaperDocument,
        external_metadata: dict[str, Any] | None = None,
        source_reference: str | None = None,
        library_name: str | None = None,
        oa_status: str | None = None,
        quality_report: dict[str, Any] | None = None,
    ) -> Paper:
        ext = external_metadata or {}
        paper = Paper(
            library_name=(library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME,
            doi=self.identity.normalize_doi(ext.get("doi") or document.metadata.get("doi")),
            title=ext.get("title") or document.metadata.get("title"),
            year=ext.get("year") or document.metadata.get("year"),
            journal=ext.get("journal") or document.metadata.get("journal"),
            authors=ext.get("authors") or document.metadata.get("authors", []),
            abstract=ext.get("abstract") or document.abstract or None,
            pdf_path=self._artifact_ref(document.source_pdf_path, category="pdf") or str(document.source_pdf_path),
            source_path=source_reference,
            oa_status=oa_status or ext.get("oa_status") or document.metadata.get("oa_status"),
            license=ext.get("license") or document.metadata.get("license"),
            tei_path=self._artifact_ref(document.tei_path, category="tei"),
            docling_json_path=self._artifact_ref(document.docling_json_path, category="docling_json"),
            markdown_path=self._artifact_ref(document.markdown_path, category="markdown"),
        )
        self.session.add(paper)
        self.session.flush()
        if quality_report:
            self.workbench.apply_quality_report(paper, quality_report)
        if quality_report and quality_report.get("needs_human_confirmation"):
            ensure_paper_codes(self.session, [paper])
            renumber_library_papers_by_year(self.session, paper.library_name)
            self.session.commit()
            self.session.refresh(paper)
            try:
                self.workbench.prepare_paper_workspace(paper.id)
            except Exception:
                logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
            return paper
        self._persist_document_entities(paper, document)
        if self.settings.auto_run_stage2_extraction:
            summary = self.extraction_pipeline.run_stage2(paper, document)
        else:
            summary = {}
            logger.info(
                "Stage-2 extraction skipped for paper %s (auto_run_stage2_extraction=False)",
                paper.id,
            )
            self.session.add(
                AuditLog(
                    paper_id=paper.id,
                    action="stage2_skipped",
                    source="paper_ingestion",
                    target_type="paper",
                    target_id=str(paper.id),
                    payload={"reason": "auto_run_stage2_extraction disabled"},
                )
            )
        self.workbench.mark_parsed_ready(
            paper,
            candidate_count=self._stage2_candidate_count(summary),
        )
        ensure_paper_codes(self.session, [paper])
        renumber_library_papers_by_year(self.session, paper.library_name)
        self.session.commit()
        self.session.refresh(paper)
        try:
            self.workbench.prepare_paper_workspace(paper.id)
        except Exception:
            logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
        return paper

    def _persist_supplementary(
        self,
        *,
        main_paper: Paper,
        document: UnifiedPaperDocument,
        external_metadata: dict[str, Any] | None = None,
        source_reference: str | None = None,
        oa_status: str | None = None,
        quality_report: dict[str, Any] | None = None,
    ) -> Paper:
        ext = external_metadata or {}
        paper = Paper(
            library_name=main_paper.library_name,
            doi=None,
            title=ext.get("title") or document.metadata.get("title") or document.source_pdf_path.name,
            year=ext.get("year") or document.metadata.get("year") or main_paper.year,
            journal=ext.get("journal") or document.metadata.get("journal") or main_paper.journal,
            authors=[],
            abstract=ext.get("abstract") or document.abstract or None,
            pdf_path=self._artifact_ref(document.source_pdf_path, category="pdf") or str(document.source_pdf_path),
            source_path=source_reference,
            oa_status=oa_status or ext.get("oa_status") or document.metadata.get("oa_status"),
            license=ext.get("license") or document.metadata.get("license"),
            tei_path=self._artifact_ref(document.tei_path, category="tei"),
            docling_json_path=self._artifact_ref(document.docling_json_path, category="docling_json"),
            markdown_path=self._artifact_ref(document.markdown_path, category="markdown"),
            paper_type="supplementary",
        )
        self.session.add(paper)
        self.session.flush()
        if quality_report:
            self.workbench.apply_quality_report(paper, quality_report)
        paper.paper_code = next_supplementary_paper_code(
            self.session,
            main_paper_code=main_paper.paper_code,
            serial_number=main_paper.serial_number,
            exclude_paper_id=paper.id,
        )

        if quality_report and quality_report.get("needs_human_confirmation"):
            self._ensure_supplementary_relationship(main_paper=main_paper, supplementary_paper=paper)
            self.session.commit()
            self.session.refresh(paper)
            try:
                self.workbench.prepare_paper_workspace(paper.id)
            except Exception:
                logger.exception("Failed to prepare Codex workspace for supplementary paper %s", paper.id)
            return paper

        self._persist_document_entities(paper, document)
        if self.settings.auto_run_stage2_extraction:
            summary = self.extraction_pipeline.run_stage2(paper, document)
        else:
            summary = {}
            logger.info(
                "Stage-2 extraction skipped for supplementary paper %s (auto_run_stage2_extraction=False)",
                paper.id,
            )
            self.session.add(
                AuditLog(
                    paper_id=paper.id,
                    action="stage2_skipped",
                    source="paper_ingestion",
                    target_type="paper",
                    target_id=str(paper.id),
                    payload={"reason": "auto_run_stage2_extraction disabled", "supplementary": True},
                )
            )
        self.workbench.mark_parsed_ready(
            paper,
            candidate_count=self._stage2_candidate_count(summary),
        )
        self._ensure_supplementary_relationship(main_paper=main_paper, supplementary_paper=paper)
        self.session.commit()
        self.session.refresh(paper)
        try:
            self.workbench.prepare_paper_workspace(paper.id)
        except Exception:
            logger.exception("Failed to prepare Codex workspace for supplementary paper %s", paper.id)
        return paper

    def _merge_into_existing_paper(
        self,
        paper: Paper,
        document: UnifiedPaperDocument,
        external_metadata: dict[str, Any] | None = None,
        source_reference: str | None = None,
        oa_status: str | None = None,
        quality_report: dict[str, Any] | None = None,
    ) -> Paper:
        ext = external_metadata or {}
        is_supplementary = self._is_supplementary_paper(paper)
        if is_supplementary:
            paper.doi = None
        else:
            paper.doi = self.identity.normalize_doi(ext.get("doi") or document.metadata.get("doi") or paper.doi)
        paper.title = ext.get("title") or document.metadata.get("title") or paper.title
        paper.year = ext.get("year") or document.metadata.get("year") or paper.year
        paper.journal = ext.get("journal") or document.metadata.get("journal") or paper.journal
        paper.authors = ext.get("authors") or document.metadata.get("authors", []) or paper.authors or []
        paper.abstract = ext.get("abstract") or document.abstract or paper.abstract
        paper.pdf_path = self._artifact_ref(document.source_pdf_path, category="pdf") or str(document.source_pdf_path)
        paper.source_path = source_reference or paper.source_path
        paper.oa_status = oa_status or ext.get("oa_status") or document.metadata.get("oa_status") or paper.oa_status
        paper.license = ext.get("license") or document.metadata.get("license") or paper.license
        paper.tei_path = self._artifact_ref(document.tei_path, category="tei")
        paper.docling_json_path = self._artifact_ref(document.docling_json_path, category="docling_json")
        paper.markdown_path = self._artifact_ref(document.markdown_path, category="markdown")
        if quality_report:
            self.workbench.apply_quality_report(paper, quality_report)
        self.session.add(paper)
        self.session.flush()

        if quality_report and quality_report.get("needs_human_confirmation"):
            ensure_paper_codes(self.session, [paper])
            self.session.commit()
            self.session.refresh(paper)
            try:
                self.workbench.prepare_paper_workspace(paper.id)
            except Exception:
                logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
            return paper

        self._clear_document_entities(paper.id)
        self._persist_document_entities(paper, document)
        if self.settings.auto_run_stage2_extraction:
            summary = self.extraction_pipeline.replace_stage2(paper, document)
        else:
            summary = {}
            self.extraction_pipeline._delete_existing_stage2(paper.id)
            paper.comprehensive_analysis = None
            logger.info(
                "Stage-2 extraction skipped for paper %s (auto_run_stage2_extraction=False); existing stage2 cleared",
                paper.id,
            )
            self.session.add(
                AuditLog(
                    paper_id=paper.id,
                    action="stage2_skipped",
                    source="paper_ingestion",
                    target_type="paper",
                    target_id=str(paper.id),
                    payload={"reason": "auto_run_stage2_extraction disabled", "merge": True},
                )
            )
        self.workbench.mark_parsed_ready(
            paper,
            candidate_count=self._stage2_candidate_count(summary),
        )
        ensure_paper_codes(self.session, [paper])
        renumber_library_papers_by_year(self.session, paper.library_name)
        self.session.commit()
        self.session.refresh(paper)
        try:
            self.workbench.prepare_paper_workspace(paper.id)
        except Exception:
            logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
        return paper

    @staticmethod
    def _is_supplementary_paper(paper: Paper) -> bool:
        return str(getattr(paper, "paper_type", "") or "").strip().lower() in {
            "supplementary",
            "supplementary_information",
            "supporting_information",
            "si",
        }

    def _docling_document_timeout_for(self, *, supplementary: bool = False) -> float | None:
        if supplementary:
            return self.settings.docling_supplementary_document_timeout
        return self.settings.docling_document_timeout

    def _persist_quality_blocked(
        self,
        *,
        stored_pdf: Path,
        original_filename: str,
        quality_report: dict[str, Any],
        external_metadata: dict[str, Any] | None,
        source_reference: str | None,
        library_name: str | None,
        ingest_source: str | None,
    ) -> Paper:
        ext = external_metadata or {}
        library = (library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME
        paper = Paper(
            library_name=library,
            doi=self.identity.normalize_doi(ext.get("doi")),
            title=ext.get("title") or original_filename,
            year=ext.get("year"),
            journal=ext.get("journal"),
            authors=ext.get("authors") or [],
            abstract=ext.get("abstract"),
            pdf_path=self._artifact_ref(stored_pdf, category="pdf") or str(stored_pdf),
            source_path=source_reference,
            oa_status=ingest_source or "quality_blocked",
            workflow_status="Needs_Human_Confirmation",
            pdf_quality_status=quality_report.get("quality_status"),
            pdf_quality_score=quality_report.get("quality_score"),
            pdf_quality_report=quality_report,
        )
        self.session.add(paper)
        self.session.flush()
        ensure_paper_codes(self.session, [paper])
        renumber_library_papers_by_year(self.session, paper.library_name)
        self.session.commit()
        self.session.refresh(paper)
        try:
            self.workbench.prepare_paper_workspace(paper.id)
        except Exception:
            logger.exception("Failed to prepare quality-blocked Codex workspace for paper %s", paper.id)
        return paper

    @staticmethod
    def _stage2_candidate_count(summary: Any) -> int:
        if not isinstance(summary, dict):
            return 0
        total = 0
        for key in ("dft_results", "dft_settings", "mechanism_claims"):
            try:
                total += int(summary.get(key) or 0)
            except (TypeError, ValueError):
                continue
        return total

    def _persist_document_entities(self, paper: Paper, document: UnifiedPaperDocument) -> None:
        for section in document.sections:
            self._add_section_with_chunks(
                paper_id=paper.id,
                section_title=section.section_title,
                section_type=section.section_type,
                text=section.text,
                page_start=section.page_start,
                page_end=section.page_end,
                section_level=section.section_level,
                section_number=section.section_number,
                parent_heading=section.parent_heading,
                heading_path=section.heading_path,
            )

        for table in document.tables:
            table_text = "\n".join(filter(None, [table.caption, table.markdown_content]))
            self.session.add(
                PaperTable(
                    paper_id=paper.id,
                    caption=table.caption,
                    markdown_content=table.markdown_content,
                    page=table.page,
                    extraction_source=table.extraction_source,
                    prov=table.prov,
                )
            )
            if table_text:
                self._add_section_with_chunks(
                    paper_id=paper.id,
                    section_title=table.caption or "Table",
                    section_type="table",
                    text=table_text,
                    page_start=table.page,
                    page_end=table.page,
                )

        for figure in document.figures:
            caption_text = figure.caption or "Figure"

            # Build enhanced text for vector indexing: caption + stored figure metadata + numerical data points
            enhanced_text = caption_text
            if figure.content_summary:
                enhanced_text += f"\n[AI Visual Summary]: {figure.content_summary}"
            if figure.numerical_data_points:
                enhanced_text += f"\n[Extracted Data]: {json.dumps(figure.numerical_data_points, ensure_ascii=False)}"

            db_figure = PaperFigure(
                paper_id=paper.id,
                caption=figure.caption,
                image_path=figure.image_path,
                page=figure.page,
                figure_role=figure.figure_role,
                role_confidence=figure.role_confidence,
                content_summary=figure.content_summary,
                key_elements=figure.key_elements,
                prov=figure.prov,
            )
            self.session.add(db_figure)
            self.session.flush()

            self._add_section_with_chunks(
                paper_id=paper.id,
                section_title=caption_text,
                section_type="figure_caption",
                text=enhanced_text,
                page_start=figure.page,
                page_end=figure.page,
                create_chunks=False,
            )

            if figure.numerical_data_points:
                for dp in figure.numerical_data_points:
                    metric_name = dp.get("metric_name")
                    if not metric_name:
                        continue

                    try:
                        metric_value = float(dp["metric_value"]) if dp.get("metric_value") is not None else None
                    except (ValueError, TypeError):
                        metric_value = None

                    confidence = 1.0
                    try:
                        if dp.get("confidence") is not None:
                            confidence = float(dp["confidence"])
                    except (ValueError, TypeError):
                        pass

                    db_dp = FigureDataPoint(
                        figure_id=db_figure.id,
                        paper_id=paper.id,
                        metric_name=metric_name,
                        metric_value=metric_value,
                        unit=dp.get("unit"),
                        conditions=dp.get("conditions"),
                        sample_label=dp.get("sample_label"),
                        confidence=confidence,
                        raw_text=str(dp),
                    )

                    self.session.add(db_dp)
                    self.session.flush()

                    text_evidence = self._figure_data_evidence_text(
                        metric_name=db_dp.metric_name,
                        metric_value=db_dp.metric_value,
                        unit=db_dp.unit,
                        sample_label=db_dp.sample_label,
                        conditions=db_dp.conditions,
                        figure_caption=figure.caption,
                    )

                    evidence = EvidenceSpan(
                        paper_id=paper.id,
                        object_type="figure_data",
                        object_id=str(db_dp.id),
                        text=text_evidence,
                        page=figure.page,
                        figure=figure.caption or "Figure",
                        confidence=db_dp.confidence,
                    )
                    self.session.add(evidence)
                    self.session.flush()
                    bbox = None
                    if figure.prov and isinstance(figure.prov, list):
                        first_prov = figure.prov[0]
                        if isinstance(first_prov, dict):
                            bbox = first_prov.get("bbox")
                    self.locators.create_locator_for_span(
                        paper_id=paper.id,
                        object_type="figure_data",
                        object_id=str(db_dp.id),
                        evidence_text=text_evidence,
                        page=figure.page,
                        section=None,
                        figure=figure.caption or "Figure",
                        table=None,
                        confidence=db_dp.confidence,
                        bbox=bbox,
                        parser_source="docling" if bbox else "fallback",
                    )

    @staticmethod
    def _figure_data_evidence_text(
        *,
        metric_name: str,
        metric_value: float | None,
        unit: str | None,
        sample_label: str | None,
        conditions: dict | None,
        figure_caption: str | None,
    ) -> str:
        unit_str = f" {unit}" if unit else ""
        val_str = f": {metric_value}" if metric_value is not None else ""
        sample_str = f" for {sample_label}" if sample_label else ""
        conditions_str = f" under {conditions}" if conditions else ""
        fig_str = f" (from Figure {figure_caption or ''})"
        return f"{metric_name}{val_str}{unit_str}{sample_str}{conditions_str}{fig_str}"

    def _add_section_with_chunks(
        self,
        *,
        paper_id: UUID,
        section_title: str | None,
        section_type: str | None,
        text: str,
        page_start: int | None,
        page_end: int | None,
        section_level: int | None = None,
        section_number: str | None = None,
        parent_heading: str | None = None,
        heading_path: list[str] | None = None,
        create_chunks: bool = True,
    ) -> PaperSection:
        section = PaperSection(
            paper_id=paper_id,
            section_title=section_title,
            section_type=section_type,
            text=text,
            page_start=page_start,
            page_end=page_end,
            section_level=section_level,
            section_number=section_number,
            parent_heading=parent_heading,
            heading_path=heading_path or [],
            embedding=self._embed_text(text),
        )
        self.session.add(section)
        self.session.flush()
        if not create_chunks:
            return section
        for index, chunk_text in enumerate(self._chunk_text(text), start=1):
            self.session.add(
                PaperChunk(
                    paper_id=paper_id,
                    section_id=section.id,
                    chunk_index=index,
                    text=chunk_text,
                    page_start=page_start,
                    page_end=page_end,
                    token_count=len(self._chunk_tokens(chunk_text)),
                    embedding=self._embed_text(chunk_text),
                    embedding_model=self.settings.embedding_model,
                    embedding_dimension=self.settings.embedding_dimension,
                    content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                )
            )
        return section

    def _embed_text(self, text: str) -> list[float]:
        try:
            vector = self.embedding.embed_text(text)
        except EmbeddingUnavailableError:
            logger.exception("Embedding generation failed; using deterministic fallback vector")
            vector = get_embedding_service(
                provider="deterministic",
                dimension=self.settings.embedding_dimension,
            ).embed_text(text)
        if len(vector) != self.settings.embedding_dimension:
            logger.warning(
                "Embedding dimension mismatch: expected %s, got %s; using deterministic fallback vector",
                self.settings.embedding_dimension,
                len(vector),
            )
            vector = get_embedding_service(
                provider="deterministic",
                dimension=self.settings.embedding_dimension,
            ).embed_text(text)
        return vector

    @staticmethod
    def _chunk_tokens(text: str) -> list[str]:
        return re.findall(r"\S+", text or "")

    @classmethod
    def _chunk_text(cls, text: str, *, max_tokens: int = 800, overlap: int = 120) -> list[str]:
        if max_tokens <= 0:
            return []
        units = cls._structural_chunk_units(text, max_tokens=max_tokens)
        if not units:
            return []

        chunks: list[str] = []
        current: list[str] = []
        current_count = 0
        for unit in units:
            unit_count = len(cls._chunk_tokens(unit))
            if current and current_count + unit_count > max_tokens:
                chunks.append("\n\n".join(current).strip())
                overlap_units: list[str] = []
                overlap_count = 0
                for previous in reversed(current):
                    previous_count = len(cls._chunk_tokens(previous))
                    if overlap_count + previous_count > overlap:
                        break
                    overlap_units.insert(0, previous)
                    overlap_count += previous_count
                if not overlap_units and overlap > 0:
                    tail = cls._chunk_tokens(current[-1])[-overlap:]
                    overlap_units = [" ".join(tail)] if tail else []
                    overlap_count = len(tail)
                while overlap_units and overlap_count + unit_count > max_tokens:
                    removed = overlap_units.pop(0)
                    overlap_count -= len(cls._chunk_tokens(removed))
                current = overlap_units
                current_count = overlap_count
            current.append(unit)
            current_count += unit_count
        if current:
            chunks.append("\n\n".join(current).strip())
        return chunks

    @classmethod
    def _structural_chunk_units(cls, text: str, *, max_tokens: int) -> list[str]:
        blocks = [block.strip() for block in re.split(r"\n\s*\n+", text or "") if block.strip()]
        units: list[str] = []
        for block in blocks:
            block_tokens = cls._chunk_tokens(block)
            if len(block_tokens) <= max_tokens:
                units.append(block)
                continue

            sentences = [
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?。！？])\s+", block)
                if sentence.strip()
            ]
            if len(sentences) <= 1:
                units.extend(
                    " ".join(block_tokens[start : start + max_tokens])
                    for start in range(0, len(block_tokens), max_tokens)
                )
                continue

            sentence_group: list[str] = []
            sentence_group_count = 0
            for sentence in sentences:
                sentence_tokens = cls._chunk_tokens(sentence)
                if len(sentence_tokens) > max_tokens:
                    if sentence_group:
                        units.append(" ".join(sentence_group))
                        sentence_group = []
                        sentence_group_count = 0
                    units.extend(
                        " ".join(sentence_tokens[start : start + max_tokens])
                        for start in range(0, len(sentence_tokens), max_tokens)
                    )
                    continue
                if sentence_group and sentence_group_count + len(sentence_tokens) > max_tokens:
                    units.append(" ".join(sentence_group))
                    sentence_group = []
                    sentence_group_count = 0
                sentence_group.append(sentence)
                sentence_group_count += len(sentence_tokens)
            if sentence_group:
                units.append(" ".join(sentence_group))
        return units

    def _clear_document_entities(self, paper_id: UUID) -> None:
        self.session.execute(
            delete(EvidenceSpan).where(
                EvidenceSpan.paper_id == paper_id,
                EvidenceSpan.object_type == "figure_data",
            )
        )
        self.session.execute(delete(FigureDataPoint).where(FigureDataPoint.paper_id == paper_id))
        self.session.execute(delete(PaperChunk).where(PaperChunk.paper_id == paper_id))
        self.session.execute(delete(PaperFigure).where(PaperFigure.paper_id == paper_id))
        self.session.execute(delete(PaperTable).where(PaperTable.paper_id == paper_id))
        self.session.execute(delete(PaperSection).where(PaperSection.paper_id == paper_id))

    def _build_identity_metadata(
        self,
        document: UnifiedPaperDocument,
        external_metadata: dict[str, Any] | None,
        source_reference: str | None,
    ) -> dict[str, Any]:
        ext = external_metadata or {}
        doi = self.identity.normalize_doi(ext.get("doi") or document.metadata.get("doi"))
        title = ext.get("title") or document.metadata.get("title")
        year = ext.get("year") or document.metadata.get("year")
        arxiv_source = (
            ext.get("arxiv_id")
            or ext.get("identifier")
            or ext.get("url")
            or source_reference
            or document.metadata.get("identifier")
            or document.metadata.get("source_path")
            or title
        )
        return {
            "doi": doi,
            "title": title,
            "year": year,
            "arxiv_id": self.identity.extract_arxiv_id(str(arxiv_source) if arxiv_source else None),
        }

    def _artifact_ref(self, path: Path | None, *, category: str) -> str | None:
        return canonicalize_persisted_artifact_reference(path, category=category, settings=self.settings)

    @staticmethod
    def _needs_metadata_enrichment(metadata: dict[str, Any] | None) -> bool:
        data = metadata or {}
        title = str(data.get("title") or "").strip()
        if not title:
            return True
        return not any(data.get(key) for key in ("year", "journal", "authors"))

    @staticmethod
    def _is_placeholder_title(title: Any, pdf_path: Path) -> bool:
        if not title:
            return True
        normalized = str(title).strip().lower()
        if not normalized:
            return True
        return normalized in {pdf_path.name.lower(), pdf_path.stem.lower()} or normalized.endswith(".pdf")

    @staticmethod
    def _derive_title_from_docling(markdown: str, page_blocks: list[dict[str, Any]]) -> str | None:
        lines: list[str] = []
        if markdown:
            lines.extend(markdown.splitlines()[:60])
        for block in page_blocks:
            if block.get("page") not in (None, 1):
                continue
            text = block.get("text") or ""
            if text:
                lines.extend(text.splitlines()[:60])
                break

        skip_prefixes = (
            "abstract",
            "keywords",
            "citation:",
            "received:",
            "revised:",
            "accepted:",
            "published:",
            "copyright",
            "academic editor",
            "e-mail",
            "email:",
            "department ",
            "school ",
            "state key",
            "* correspondence",
            "- * correspondence",
            "arxiv:",
        )
        skip_exact = {"article", "review", "communication", "contents", "references"}
        for raw in lines:
            line = re.sub(r"^#+\s*", "", raw or "").strip()
            line = re.sub(r"\s+", " ", line)
            if not line or line.startswith("<!--"):
                continue
            lowered = line.lower()
            if lowered in skip_exact or lowered.startswith(skip_prefixes):
                continue
            if "@" in line and len(line) < 180:
                continue
            if not (20 <= len(line) <= 280):
                continue
            if not re.search(r"[A-Za-z]", line):
                continue
            return line.rstrip(".")
        return None

    @staticmethod
    def _derive_doi_from_text(text: str) -> str | None:
        if not text:
            return None
        lines = [line.strip() for line in text.splitlines()[:120] if line.strip()]
        priority_lines = [
            line
            for line in lines
            if line.lower().startswith("citation:") or "doi.org/" in line.lower() or line.lower().startswith("doi:")
        ]
        for line in priority_lines + lines[:80]:
            candidate = PaperIngestionService._extract_doi_candidate(line)
            if candidate:
                return candidate
        return None

    @staticmethod
    def _extract_doi_candidate(text: str) -> str | None:
        match = re.search(r"10\.\d{4,9}/(.+)$", text or "", flags=re.IGNORECASE)
        if not match:
            return None
        candidate = re.sub(r"\s+", "", match.group(0)).rstrip(".,;:)")
        candidate = re.sub(r"(?i)(?:academiceditor|received|revised|accepted|published).*$", "", candidate)
        candidate = candidate.rstrip(".,;:)")
        if candidate.lower().endswith("/s1"):
            return None
        if not DOI_RE.fullmatch(candidate):
            return None
        suffix = candidate.split("/", 1)[1]
        if len(suffix) < 6 or not re.search(r"\d", suffix):
            return None
        return candidate.lower()

    def _find_conflicting_paper(
        self,
        doi: str | None,
        arxiv_id: str | None,
        library_name: str,
        exclude_paper_id: UUID | None = None,
    ) -> Paper | None:
        if not doi and not arxiv_id:
            return None
        candidate = self.identity.find_existing_paper(
            self.session,
            doi=doi,
            title=None,
            year=None,
            arxiv_id=arxiv_id,
            library_name=library_name,
        )
        if candidate is None:
            return None
        if exclude_paper_id is not None and candidate.id == exclude_paper_id:
            return None
        if candidate.oa_status == "metadata_only":
            return None
        if doi and self.identity.normalize_doi(candidate.doi) == doi and candidate.pdf_path:
            return candidate
        candidate_arxiv = self.identity.extract_arxiv_id(candidate.source_path or candidate.title or candidate.doi)
        if arxiv_id and candidate_arxiv == arxiv_id and candidate.pdf_path:
            return candidate
        return None

    def _find_existing_supplementary_by_hash(self, main_paper: Paper, source_path: Path) -> Paper | None:
        incoming_hash = self._file_sha256(source_path)
        relationships = self.session.scalars(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == main_paper.id,
                PaperRelationship.relationship_type == "supplementary",
            )
        ).all()
        if not relationships:
            return None
        target_ids = [row.target_paper_id for row in relationships]
        papers = self.session.scalars(select(Paper).where(Paper.id.in_(target_ids))).all()
        for paper in papers:
            resolved_pdf = resolve_persisted_artifact_path(
                paper.pdf_path,
                category="pdf",
                settings=self.settings,
                must_exist=False,
                trusted_persisted_reference=True,
            ) or Path(str(paper.pdf_path or ""))
            if not resolved_pdf.exists():
                continue
            if self._file_sha256(resolved_pdf) == incoming_hash:
                return paper
        return None

    def _ensure_supplementary_relationship(self, *, main_paper: Paper, supplementary_paper: Paper) -> None:
        existing = self.session.scalar(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == main_paper.id,
                PaperRelationship.target_paper_id == supplementary_paper.id,
                PaperRelationship.relationship_type == "supplementary",
            )
        )
        if existing is not None:
            return
        self.session.add(
            PaperRelationship(
                source_paper_id=main_paper.id,
                target_paper_id=supplementary_paper.id,
                relationship_type="supplementary",
                created_by="supplementary_upload",
                note="Created by supplementary upload workflow.",
            )
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()

    @staticmethod
    def _infer_oa_status(source_reference: str | None, copy_pdf: bool) -> str:
        if not copy_pdf:
            return "uploaded"
        if source_reference:
            return "local_pdf"
        return "downloaded"
