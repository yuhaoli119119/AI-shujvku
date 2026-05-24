from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import func, select
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
from app.services.extraction_pipeline import ExtractionPipelineService
from app.utils.text_cleaning import normalize_text_tree

logger = logging.getLogger(__name__)

DEFAULT_LIBRARY_NAME = "\u9ed8\u8ba4\u6587\u732e\u5e93"

class PaperIngestionService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.artifacts = ArtifactStore(settings)
        self.grobid_parser = GrobidParser(settings.grobid_url)
        self.docling_parser = DoclingParser(settings)
        self.embedding = DeterministicEmbeddingService(settings.embedding_dimension)
        self.extraction_pipeline = ExtractionPipelineService(
            session=session,
            settings=settings,
        )

    async def ingest_upload(
        self,
        file: UploadFile,
        external_metadata: dict[str, Any] | None = None,
        library_name: str | None = None,
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
        )

    async def ingest_pdf(
        self,
        source_path: Path,
        original_filename: str,
        copy_pdf: bool = True,
        external_metadata: dict[str, Any] | None = None,
        source_reference: str | None = None,
        library_name: str | None = None,
    ) -> Paper:
        if copy_pdf:
            stored_pdf = self.artifacts.save_pdf_copy(source_path, f"{uuid.uuid4()}_{original_filename}")
        else:
            stored_pdf = source_path
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
        docling_result = await self.docling_parser.parse_pdf(stored_pdf)
        unified = await self._build_unified_document(stored_pdf, grobid_result, docling_result)

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

        return self._persist(unified, external_metadata, source_reference=source_reference, library_name=library_name)

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
        ext = external_metadata or {}
        title = ext.get("title") or identifier or "Untitled paper"
        paper = Paper(
            library_name=(library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME,
            doi=ext.get("doi"),
            title=title,
            year=ext.get("year"),
            journal=ext.get("journal"),
            authors=ext.get("authors") or [],
            abstract=ext.get("abstract") or None,
            pdf_path="",
            source_path=source_reference or ext.get("url") or ext.get("identifier") or identifier,
            oa_status="metadata_only",
            license=ext.get("license"),
        )
        
        # 针对 metadata_only 论文执行自适应启发式快速分类
        res = self.extraction_pipeline._rule_based_classify(title, ext.get("journal"))
        paper.paper_type = res["paper_type"]
        paper.type_confidence = res["type_confidence"]
        paper.classification_source = res["classification_source"]

        max_sn = self.session.scalar(
            select(func.max(Paper.serial_number)).where(Paper.library_name == paper.library_name)
        )
        paper.serial_number = (max_sn or 0) + 1
        self.session.add(paper)
        self.session.commit()
        self.session.refresh(paper)
        return paper

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
    ) -> Paper:
        ext = external_metadata or {}
        paper = Paper(
            library_name=(library_name or DEFAULT_LIBRARY_NAME).strip() or DEFAULT_LIBRARY_NAME,
            doi=ext.get("doi") or document.metadata.get("doi"),
            title=ext.get("title") or document.metadata.get("title"),
            year=ext.get("year") or document.metadata.get("year"),
            journal=ext.get("journal") or document.metadata.get("journal"),
            authors=ext.get("authors") or document.metadata.get("authors", []),
            abstract=ext.get("abstract") or document.abstract or None,
            pdf_path=str(document.source_pdf_path),
            source_path=source_reference,
            oa_status=document.metadata.get("oa_status"),
            license=document.metadata.get("license"),
            tei_path=str(document.tei_path) if document.tei_path else None,
            docling_json_path=str(document.docling_json_path) if document.docling_json_path else None,
            markdown_path=str(document.markdown_path) if document.markdown_path else None,
        )
        max_sn = self.session.scalar(
            select(func.max(Paper.serial_number)).where(Paper.library_name == paper.library_name)
        )
        paper.serial_number = (max_sn or 0) + 1
        self.session.add(paper)
        self.session.flush()

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

            # VLM Level 3 Numerical Extraction persistence
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
                        raw_text=str(dp)
                    )
                    self.session.add(db_dp)
                    self.session.flush()

                    # Automatically generate an EvidenceSpan for downstream RAG
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
                        confidence=db_dp.confidence
                    )
                    self.session.add(evidence)

        self.extraction_pipeline.run_stage2(paper, document)
        self.session.commit()
        self.session.refresh(paper)
        return paper
