from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Paper, PaperFigure, PaperSection, PaperTable, FigureDataPoint, EvidenceSpan
from app.parsers.docling_parser import DoclingParser
from app.parsers.grobid_parser import GrobidParseResult, GrobidParser
from app.schemas.documents import UnifiedFigure, UnifiedPaperDocument, UnifiedSection, UnifiedTable
from app.services.artifact_store import ArtifactStore
from app.services.pdf_image_extractor import PdfImageExtractor
from app.services.vlm_service import VLMService
from app.services.embedding import DeterministicEmbeddingService
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.extraction_pipeline import ExtractionPipelineService
from app.services.paper_identity import PaperIdentityService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.utils.artifact_paths import canonicalize_persisted_artifact_reference
from app.utils.text_cleaning import normalize_text_tree

logger = logging.getLogger(__name__)

DEFAULT_LIBRARY_NAME = "\u9ed8\u8ba4\u6587\u732e\u5e93"
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
        self.embedding = DeterministicEmbeddingService(settings.embedding_dimension)
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
        confirm_identity_mismatch: bool = False,
        ingest_source: str | None = None,
    ) -> Paper:
        if copy_pdf:
            stored_pdf = self.artifacts.save_pdf_copy(source_path, f"{uuid.uuid4()}_{original_filename}")
        else:
            stored_pdf = source_path
        quality_report = PaperWorkbenchService.assess_pdf_path(stored_pdf, self.settings)
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
            docling_result = await self.docling_parser.parse_pdf(stored_pdf)
            unified = await self._build_unified_document(stored_pdf, grobid_result, docling_result)
        except Exception as exc:
            logger.error("Docling parsing or unified building failed for %s: %s", original_filename, exc, exc_info=True)
            if quality_report.get("needs_human_confirmation"):
                unified = self._build_quality_blocked_document(
                    stored_pdf=stored_pdf,
                    original_filename=original_filename,
                    grobid_result=grobid_result,
                    external_metadata=external_metadata,
                )
            else:
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
                self.session.commit()
                self.session.refresh(paper)
                raise RuntimeError(f"docling_parse_failed:{paper.id} {exc}") from exc

        if quality_report.get("needs_human_confirmation"):
            unified = self._document_metadata_only(unified)

        if not external_metadata:
            doi = unified.metadata.get("doi")
            if doi:
                from app.services.discovery_service import DiscoveryService
                from fastapi.concurrency import run_in_threadpool

                try:
                    svc = DiscoveryService()
                    _, external_metadata = await run_in_threadpool(svc.fetch_metadata, doi)
                except Exception as exc:
                    logger.warning("Auto-enrichment via discovery failed for DOI %s: %s", doi, exc)

        library = (library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME
        identity_metadata = self._build_identity_metadata(unified, external_metadata, source_reference)
        doi = identity_metadata.get("doi")
        title = identity_metadata.get("title")
        year = identity_metadata.get("year")
        arxiv_id = identity_metadata.get("arxiv_id")
        oa_status = ingest_source or self._infer_oa_status(source_reference=source_reference, copy_pdf=copy_pdf)

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
        return self.identity.upsert_metadata_only(
            self.session,
            external_metadata=external_metadata or {},
            identifier=identifier,
            library_name=library,
            source_reference=source_reference,
            classify_callback=self.extraction_pipeline._rule_based_classify,
        )

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

        tables = [UnifiedTable(**table) for table in normalized_tables]
        figures = [UnifiedFigure(**figure) for figure in normalized_figures]
        # 过滤掉没有 caption 的图片（logo、CrossMark 等装饰图），只保留学术图片
        # 第二道防线：docling_parser 已做黑名单过滤，这里补充短 caption 和纯序号过滤
        _short_caption_re = re.compile(r'^fig\.?\s*\d+\.?\s*$', re.IGNORECASE)
        figures = [
            fig for fig in figures
            if fig.caption
            and fig.caption.strip()
            and not _short_caption_re.match(fig.caption.strip())
        ]

        PdfImageExtractor.extract_figures(
            pdf_path=stored_pdf,
            figures=figures,
            output_dir=self.artifacts.settings.storage_paths["figures"]
        )

        # Optional Level 2 VLM Classification & Level 3 Numerical Extraction
        if self.artifacts.settings.writer_api_key:
            vlm = VLMService(self.artifacts.settings)
            prompt = """你是一位材料科学论文图表分析与数值提取专家。分析这张论文图片，返回 JSON 格式的分析结果：

{
  "figure_role": "crystal_structure | electronic_structure | reaction_pathway | phase_diagram | morphology | spectroscopy | electrochemistry | performance | schematic | comparison | other",
  "role_confidence": 0.0-1.0,
  "content_summary": "一句话描述图片内容，如'Fe-N4单原子催化剂上CO2吸附的优化构型及吸附能'",
  "key_elements": ["Fe-N4", "CO2", "adsorption energy", "-1.23 eV"],
  "numerical_data_points": [
    {
      "metric_name": "指标名称，例如 onset_potential | overpotential | tafel_slope | capacity | energy_barrier | adsorption_energy | d_band_center | band_gap | other_metric",
      "metric_value": 150.0,
      "unit": "单位，例如 mV | V | mA/cm² | mAh/g | eV | etc",
      "sample_label": "对应的样品名或图例标签，例如 Fe-N4/C, Pt/C",
      "conditions": {"electrolyte": "0.1 M KOH", "scan_rate": "5 mV/s"},
      "confidence": 0.0-1.0
    }
  ]
}

分类标准：
- crystal_structure: 原子/分子构型、晶胞、吸附位、缺陷结构
- electronic_structure: DOS图、能带、电荷密度差、Bader电荷可视化
- reaction_pathway: NEB能垒图、反应坐标、过渡态
- phase_diagram: 稳定性相图、Pourbaix图、凸包图
- morphology: SEM/TEM/AFM/HRTEM 等形貌图
- spectroscopy: XRD/Raman/FTIR/XPS/EXAFS 等光谱
- electrochemistry: CV/LSV/Tafel/EIS/恒流充放电曲线
- performance: 循环寿命/倍率/容量/效率等性能图
- schematic: 机理示意图、装置图、流程图
- comparison: 与其他工作对比的柱状图/雷达图/表格

数值提取指南：
1. 仅当图片类型属于关键数据图（如 electrochemistry, performance, reaction_pathway, crystal_structure, electronic_structure, comparison）且能读出具体数值时，才填充 "numerical_data_points" 列表。否则，填充空列表 []。
2. 尽量提取对电催化（CO2RR/ORR/HER/OER）或锂硫电池有决定性意义的定量数据点。例如：onset_potential、overpotential (特别是过电位10mA/cm²处的数值)、tafel_slope、容量、循环性能、能垒、吸附能等。
3. 准确关联样品名（"sample_label"），这对于图例/多条曲线的对比图至关重要。不确定的样品可使用 logical label。
"""
            for figure in figures:
                if figure.image_path:
                    abs_path = self.artifacts.settings.storage_paths["figures"] / figure.image_path
                    if abs_path.exists():
                        res = await asyncio.to_thread(vlm.analyze_image, str(abs_path), prompt)
                        if res:
                            figure.figure_role = res.get("figure_role", figure.figure_role)
                            figure.role_confidence = res.get("role_confidence")
                            figure.content_summary = res.get("content_summary")
                            figure.key_elements = res.get("key_elements")
                            figure.numerical_data_points = res.get("numerical_data_points")

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
        max_sn = self.session.scalar(
            select(func.max(Paper.serial_number)).where(Paper.library_name == paper.library_name)
        )
        paper.serial_number = (max_sn or 0) + 1
        self.session.add(paper)
        self.session.flush()
        if quality_report:
            self.workbench.apply_quality_report(paper, quality_report)
        if quality_report and quality_report.get("needs_human_confirmation"):
            self.session.commit()
            self.session.refresh(paper)
            try:
                self.workbench.prepare_paper_workspace(paper.id)
            except Exception:
                logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
            return paper
        self._persist_document_entities(paper, document)
        summary = self.extraction_pipeline.run_stage2(paper, document)
        self.workbench.mark_parsed_ready(
            paper,
            candidate_count=self._stage2_candidate_count(summary),
        )
        self.session.commit()
        self.session.refresh(paper)
        try:
            self.workbench.prepare_paper_workspace(paper.id)
        except Exception:
            logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
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
            self.session.commit()
            self.session.refresh(paper)
            try:
                self.workbench.prepare_paper_workspace(paper.id)
            except Exception:
                logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
            return paper

        self._clear_document_entities(paper.id)
        self._persist_document_entities(paper, document)
        summary = self.extraction_pipeline.replace_stage2(paper, document)
        self.workbench.mark_parsed_ready(
            paper,
            candidate_count=self._stage2_candidate_count(summary),
        )
        self.session.commit()
        self.session.refresh(paper)
        try:
            self.workbench.prepare_paper_workspace(paper.id)
        except Exception:
            logger.exception("Failed to prepare Codex workspace for paper %s", paper.id)
        return paper

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
        max_sn = self.session.scalar(
            select(func.max(Paper.serial_number)).where(Paper.library_name == paper.library_name)
        )
        paper.serial_number = (max_sn or 0) + 1
        self.session.add(paper)
        self.session.flush()
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
            self.session.add(
                PaperSection(
                    paper_id=paper.id,
                    section_title=section.section_title,
                    section_type=section.section_type,
                    text=section.text,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    embedding=self.embedding.embed_text(section.text),
                )
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
                truncated_text = table_text[:2000] + ("..." if len(table_text) > 2000 else "")
                self.session.add(
                    PaperSection(
                        paper_id=paper.id,
                        section_title=table.caption or "Table",
                        section_type="table",
                        text=truncated_text,
                        page_start=table.page,
                        page_end=table.page,
                        embedding=self.embedding.embed_text(truncated_text),
                    )
                )

        for figure in document.figures:
            caption_text = figure.caption or "Figure"
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

            self.session.add(
                PaperSection(
                    paper_id=paper.id,
                    section_title=caption_text,
                    section_type="figure_caption",
                    text=caption_text,
                    page_start=figure.page,
                    page_end=figure.page,
                    embedding=self.embedding.embed_text(caption_text),
                )
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

                    unit_str = f" {db_dp.unit}" if db_dp.unit else ""
                    val_str = f": {db_dp.metric_value}" if db_dp.metric_value is not None else ""
                    sample_str = f" for {db_dp.sample_label}" if db_dp.sample_label else ""
                    fig_str = f" (from Figure {figure.caption or ''})"
                    text_evidence = f"{db_dp.metric_name}{val_str}{unit_str}{sample_str}{fig_str}"

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

    def _clear_document_entities(self, paper_id: UUID) -> None:
        self.session.execute(
            delete(EvidenceSpan).where(
                EvidenceSpan.paper_id == paper_id,
                EvidenceSpan.object_type == "figure_data",
            )
        )
        self.session.execute(delete(FigureDataPoint).where(FigureDataPoint.paper_id == paper_id))
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
        if candidate is None and (doi or arxiv_id):
            candidate = self.identity.find_existing_paper(
                self.session,
                doi=doi,
                title=None,
                year=None,
                arxiv_id=arxiv_id,
                library_name=None,
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

    @staticmethod
    def _infer_oa_status(source_reference: str | None, copy_pdf: bool) -> str:
        if not copy_pdf:
            return "uploaded"
        if source_reference:
            return "local_pdf"
        return "downloaded"
