from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Paper, PaperFigure, PaperSection, PaperTable
from app.schemas.documents import UnifiedFigure, UnifiedPaperDocument, UnifiedSection, UnifiedTable
from app.services.extraction_pipeline import ExtractionPipelineService
from app.utils.artifact_paths import resolve_persisted_artifact_path


class PaperReprocessingService:
    """Rebuilds a unified document from persisted paper artifacts and reruns Stage 2 extraction."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.pipeline = ExtractionPipelineService(session, settings)

    def rerun_stage2(self, paper_id: UUID) -> dict[str, int]:
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise ValueError("Paper not found")
        document = self._rebuild_document(paper)
        summary = self.pipeline.replace_stage2(paper, document)
        self.session.commit()
        return summary

    def classify_single_paper(self, paper_id: UUID, overwrite: bool = False) -> dict[str, Any]:
        """Classify a single paper using LLM or rule fallback, and commit to DB."""
        paper = self.session.get(Paper, paper_id)
        if not paper:
            raise ValueError("Paper not found")
        
        # If already classified and we don't want to overwrite, return existing
        if paper.paper_type and paper.paper_type != "Unknown" and not overwrite:
            return {
                "paper_type": paper.paper_type,
                "type_confidence": paper.type_confidence or 0.0,
                "classification_source": paper.classification_source or "existing",
            }
            
        # 1. Check if metadata_only paper
        is_metadata_only = not paper.pdf_path or paper.oa_status == "metadata_only"
        
        # 2. Metadata-only papers still get an AI-assisted attempt when title/abstract is available.
        if is_metadata_only:
            res = self._classify_with_ai_or_rules(paper)
            paper.paper_type = res["paper_type"]
            paper.type_confidence = res["type_confidence"]
            paper.classification_source = res["classification_source"]
            self.session.add(paper)
            self.session.commit()
            return res
            
        # 3. Rebuild document safely
        try:
            document = self._rebuild_document(paper)
        except Exception:
            # Rebuild document failed, fallback to AI-assisted metadata classification and then rules.
            res = self._classify_with_ai_or_rules(paper)
            paper.paper_type = res["paper_type"]
            paper.type_confidence = res["type_confidence"]
            paper.classification_source = res["classification_source"]
            self.session.add(paper)
            self.session.commit()
            return res
            
        # 4. Fallback if document has no valid text content
        if not document.sections and not document.abstract:
            res = self._classify_with_ai_or_rules(paper)
        else:
            try:
                quick_class = self.pipeline.comprehensive_extractor.extract_quick_classification(document)
                if quick_class and quick_class.get("paper_type") != "Unknown":
                    res = {
                        "paper_type": quick_class.get("paper_type"),
                        "type_confidence": quick_class.get("type_confidence", 0.0),
                        "classification_source": "quick"
                    }
                else:
                    sections_text = "\n".join(section.text or "" for section in (document.sections or [])[:12])
                    res = self.pipeline._rule_based_classify(paper.title, paper.journal, document.abstract, sections_text)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("LLM classification failed for paper %s, falling back to rules: %s", paper_id, e)
                sections_text = "\n".join(section.text or "" for section in (document.sections or [])[:12])
                res = self.pipeline._rule_based_classify(paper.title, paper.journal, document.abstract, sections_text)
                
        paper.paper_type = res["paper_type"]
        paper.type_confidence = res["type_confidence"]
        paper.classification_source = res["classification_source"]
        self.session.add(paper)
        self.session.commit()
        return res

    def _classify_with_ai_or_rules(self, paper: Paper) -> dict[str, Any]:
        synthetic_doc = self._build_classification_stub(paper)
        try:
            quick_class = self.pipeline.comprehensive_extractor.extract_quick_classification(synthetic_doc)
        except Exception:
            quick_class = None
        if quick_class and quick_class.get("paper_type") and quick_class.get("paper_type") != "Unknown":
            return {
                "paper_type": quick_class.get("paper_type"),
                "type_confidence": quick_class.get("type_confidence", 0.0),
                "classification_source": quick_class.get("classification_source", "quick"),
            }
        result = self.pipeline._rule_based_classify(paper.title, paper.journal, paper.abstract)
        if (not paper.pdf_path or paper.oa_status == "metadata_only") and result.get("classification_source") == "rule_heuristic":
            result["type_confidence"] = min(float(result.get("type_confidence") or 0.0), 0.5)
        return result

    def _build_classification_stub(self, paper: Paper) -> UnifiedPaperDocument:
        summary_lines = [
            f"Title: {paper.title or ''}",
            f"Journal: {paper.journal or ''}",
            f"Year: {paper.year or ''}",
        ]
        if paper.abstract:
            summary_lines.append(f"Abstract: {paper.abstract}")
        pseudo_summary = "\n".join(line for line in summary_lines if line.strip())
        sections = [
            UnifiedSection(
                section_title="Metadata Summary",
                section_type="metadata",
                text=pseudo_summary,
                page_start=None,
                page_end=None,
            )
        ] if pseudo_summary else []
        return UnifiedPaperDocument(
            metadata={
                "doi": paper.doi,
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "authors": paper.authors or [],
            },
            abstract=paper.abstract or pseudo_summary,
            sections=sections,
            tables=[],
            figures=[],
            references=[],
            markdown=pseudo_summary,
            tei_xml="",
            docling_json={},
            source_pdf_path=Path(paper.pdf_path or ""),
            tei_path=None,
            markdown_path=None,
            docling_json_path=None,
        )

    def _rebuild_document(self, paper: Paper) -> UnifiedPaperDocument:
        section_rows = self.session.scalars(
            select(PaperSection)
            .where(PaperSection.paper_id == paper.id)
            .where(PaperSection.section_type.not_in(["table", "figure_caption"]))
            .order_by(PaperSection.page_start.asc().nulls_last(), PaperSection.section_title.asc())
        ).all()
        table_rows = self.session.scalars(
            select(PaperTable)
            .where(PaperTable.paper_id == paper.id)
            .order_by(PaperTable.page.asc().nulls_last())
        ).all()
        figure_rows = self.session.scalars(
            select(PaperFigure)
            .where(PaperFigure.paper_id == paper.id)
            .order_by(PaperFigure.page.asc().nulls_last())
        ).all()

        docling_json = self._load_json(paper.docling_json_path, category="docling_json")
        markdown = self._load_text(paper.markdown_path, category="markdown")
        tei_xml = self._load_text(paper.tei_path, category="tei")
        source_pdf_path = resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=self.settings,
            must_exist=False,
        ) or Path(paper.pdf_path or "")
        tei_path = resolve_persisted_artifact_path(paper.tei_path, category="tei", settings=self.settings, must_exist=False)
        markdown_path = resolve_persisted_artifact_path(
            paper.markdown_path,
            category="markdown",
            settings=self.settings,
            must_exist=False,
        )
        docling_json_path = resolve_persisted_artifact_path(
            paper.docling_json_path,
            category="docling_json",
            settings=self.settings,
            must_exist=False,
        )

        return UnifiedPaperDocument(
            metadata={
                "doi": paper.doi,
                "title": paper.title,
                "year": paper.year,
                "journal": paper.journal,
                "authors": paper.authors or [],
                "oa_status": paper.oa_status,
                "license": paper.license,
            },
            abstract=paper.abstract or "",
            sections=[
                UnifiedSection(
                    section_title=row.section_title,
                    section_type=row.section_type,
                    text=row.text,
                    page_start=row.page_start,
                    page_end=row.page_end,
                )
                for row in section_rows
            ],
            tables=[
                UnifiedTable(
                    caption=row.caption,
                    markdown_content=row.markdown_content,
                    page=row.page,
                    extraction_source=row.extraction_source,
                )
                for row in table_rows
            ],
            figures=[
                UnifiedFigure(
                    caption=row.caption,
                    image_path=row.image_path,
                    page=row.page,
                    figure_role=row.figure_role,
                )
                for row in figure_rows
            ],
            references=[],
            markdown=markdown,
            tei_xml=tei_xml,
            docling_json=docling_json,
            source_pdf_path=source_pdf_path,
            tei_path=tei_path,
            markdown_path=markdown_path,
            docling_json_path=docling_json_path,
        )

    @staticmethod
    def _load_text(path_str: str | None, category: str | None = None) -> str:
        path = resolve_persisted_artifact_path(path_str, category=category)
        if path is None:
            return ""
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _load_json(path_str: str | None, category: str | None = None) -> dict:
        raw = PaperReprocessingService._load_text(path_str, category=category)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
