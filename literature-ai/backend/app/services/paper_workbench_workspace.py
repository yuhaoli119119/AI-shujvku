from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from app.db.models import (
    AuditLog,
    DFTResult,
    EvidenceLocator,
    Paper,
    PaperFigure,
    PaperRelationship,
    PaperSection,
    PaperTable,
)
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.workbench_status import WORKBENCH_SCHEMA_VERSION


class PaperWorkbenchWorkspaceMixin:
    """Workspace filesystem and source-document helpers for PaperWorkbenchService."""

    def _paper_pdf_path(self, paper: Paper) -> Path | None:
        raw_path = Path(paper.pdf_path) if paper.pdf_path else None
        if raw_path is not None and raw_path.is_absolute() and not raw_path.exists():
            return None
        return resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=self.settings,
            trusted_persisted_reference=True,
        )

    def _workspace_root(self, paper_id: UUID) -> Path:
        return self.settings.storage_root / "by_id" / str(paper_id)

    def _workspace_ref(self, workspace_root: Path) -> str:
        try:
            return workspace_root.resolve().relative_to(self.settings.storage_root.resolve()).as_posix()
        except ValueError:
            return str(workspace_root.resolve())

    @staticmethod
    def _ensure_workspace_dirs(workspace_root: Path) -> dict[str, Path]:
        dirs = {
            "root": workspace_root,
            "pages": workspace_root / "pages",
            "figures": workspace_root / "figures",
            "tables": workspace_root / "tables",
            "markdown": workspace_root / "markdown",
            "ocr": workspace_root / "ocr",
            "evidence": workspace_root / "evidence",
            "extraction": workspace_root / "extraction",
            "audit": workspace_root / "audit",
        }
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        return dirs

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _copy_source_pdf(self, pdf_path: Path | None, workspace_root: Path) -> None:
        if pdf_path is None or not pdf_path.exists():
            return
        destination = workspace_root / "original.pdf"
        if destination.exists() and destination.stat().st_size == pdf_path.stat().st_size:
            return
        if pdf_path.resolve() != destination.resolve():
            shutil.copy2(pdf_path, destination)

    def _write_markdown_copy(self, paper: Paper, markdown_dir: Path) -> None:
        markdown_path = resolve_persisted_artifact_path(
            paper.markdown_path,
            category="markdown",
            settings=self.settings,
            trusted_persisted_reference=True,
        )
        if markdown_path is not None and markdown_path.exists():
            target = markdown_dir / "source.md"
            if markdown_path.resolve() != target.resolve():
                shutil.copy2(markdown_path, target)
        self._write_json(
            markdown_dir / "trust.json",
            {
                "markdown_trust": (paper.pdf_quality_report or {}).get("markdown_trust"),
                "pdf_quality_status": paper.pdf_quality_status,
                "policy": "Markdown is reading aid only; use evidence locators/PDF pages as source of truth.",
            },
        )

    def _write_docling_copy(self, paper: Paper, extraction_dir: Path) -> None:
        docling_path = resolve_persisted_artifact_path(
            paper.docling_json_path,
            category="docling_json",
            settings=self.settings,
            trusted_persisted_reference=True,
        )
        if docling_path is not None and docling_path.exists():
            target = extraction_dir / "docling.json"
            if docling_path.resolve() != target.resolve():
                shutil.copy2(docling_path, target)

    def _write_evidence_files(self, paper: Paper, dirs: dict[str, Path]) -> None:
        sections = self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper.id)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id == paper.id)).all()
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == paper.id)).all()
        locators = self.session.scalars(select(EvidenceLocator).where(EvidenceLocator.paper_id == paper.id)).all()
        self._write_json(
            dirs["evidence"] / "sections.json",
            [
                {
                    "id": str(row.id),
                    "title": row.section_title,
                    "section_type": row.section_type,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "section_level": row.section_level,
                    "section_number": row.section_number,
                    "parent_heading": row.parent_heading,
                    "heading_path": row.heading_path or [],
                    "text": row.text,
                    "evidence_state": "parsed_source_text",
                }
                for row in sections
            ],
        )
        self._write_json(
            dirs["evidence"] / "tables.json",
            [
                {
                    "id": str(row.id),
                    "caption": row.caption,
                    "page": row.page,
                    "markdown_content": row.markdown_content,
                    "prov": row.prov,
                    "evidence_state": "table_candidate_unverified",
                }
                for row in tables
            ],
        )
        self._sync_figure_workspace_files(figures, dirs["figures"])
        self._write_json(
            dirs["evidence"] / "figures.json",
            [
                {
                    "id": str(row.id),
                    "caption": row.caption,
                    "page": row.page,
                    "image_path": row.image_path,
                    "figure_label": row.figure_label,
                    "crop_status": row.crop_status,
                    "crop_confidence": row.crop_confidence,
                    "crop_source": row.crop_source,
                    "prov": row.prov,
                }
                for row in figures
            ],
        )
        self._write_json(
            dirs["evidence"] / "locators.json",
            [
                {
                    "id": str(row.id),
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "field_name": row.field_name,
                    "source_type": row.source_type,
                    "page": row.page,
                    "bbox": row.bbox,
                    "section": row.section,
                    "figure_id": str(row.figure_id) if row.figure_id else None,
                    "table_id": str(row.table_id) if row.table_id else None,
                    "evidence_text": row.evidence_text,
                    "locator_status": row.locator_status,
                    "locator_confidence": row.locator_confidence,
                    "parser_source": row.parser_source,
                    "warning_reason": row.warning_reason,
                }
                for row in locators
            ],
        )

    def _write_extraction_files(self, paper: Paper, dirs: dict[str, Path]) -> None:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        self._write_json(
            dirs["extraction"] / "dft_candidates.json",
            [
                {
                    "record_id": str(row.id),
                    "candidate_status": row.candidate_status,
                    "adsorbate": row.adsorbate,
                    "property_type": row.property_type,
                    "value": row.value,
                    "unit": row.unit,
                    "reaction_step": row.reaction_step,
                    "source_section": row.source_section,
                    "source_figure": row.source_figure,
                    "evidence_text": row.evidence_text,
                    "confidence": row.confidence,
                    "extraction_protocol_version": row.extraction_protocol_version,
                    "evidence_payload": row.evidence_payload,
                }
                for row in rows
            ],
        )

    def _source_documents_for_ai(self, paper: Paper) -> list[dict[str, Any]]:
        pdf_path = self._paper_pdf_path(paper)
        workspace_root = self._workspace_root(paper.id)
        source_documents = [
            {
                "source_document_type": "main_text",
                "label": "Main PDF",
                "paper_id": str(paper.id),
                "path": str(pdf_path) if pdf_path is not None else str(workspace_root / "original.pdf"),
                "available": bool(pdf_path is not None and pdf_path.exists()),
            },
            {
                "source_document_type": "supplementary_information",
                "label": "SI",
                "paper_id": str(paper.id),
                "path": None,
                "available": False,
                "note": "SI is treated as a source document for this main paper, not as a separate library paper.",
            },
        ]
        source_documents.extend(self._supplementary_documents_for_ai(paper))
        return source_documents

    def _supplementary_documents_for_ai(self, paper: Paper) -> list[dict[str, Any]]:
        relationship_types = {"supplementary", "supplementary_information", "si"}
        relationships = self.session.scalars(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == paper.id,
                PaperRelationship.relationship_type.in_(relationship_types),
            )
        ).all()
        documents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for relationship in relationships:
            target = self.session.get(Paper, relationship.target_paper_id)
            if target is None:
                continue
            target_path = self._paper_pdf_path(target)
            path_text = str(target_path) if target_path is not None else None
            dedupe_key = path_text or str(target.id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            documents.append(
                {
                    "source_document_type": "supplementary_information",
                    "label": target.title or "SI",
                    "paper_id": str(paper.id),
                    "related_paper_id": str(target.id),
                    "relationship_id": str(relationship.id),
                    "relationship_type": relationship.relationship_type,
                    "path": path_text,
                    "available": bool(target_path is not None and target_path.exists()),
                    "note": "Linked supplementary PDF is treated as source material for the main paper.",
                }
            )
        return documents

    def _write_audit_files(self, paper: Paper, dirs: dict[str, Path]) -> None:
        rows = self.session.scalars(
            select(AuditLog).where(AuditLog.paper_id == paper.id).order_by(AuditLog.created_at.asc())
        ).all()
        dft_count = self.session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)) or 0
        self._write_json(
            dirs["audit"] / "dft_completeness.json",
            DFTCompletenessAuditor(self.session).audit_paper(paper.id, parsed_count=int(dft_count)),
        )
        self._write_json(
            dirs["audit"] / "audit_log.json",
            [
                {
                    "id": str(row.id),
                    "action": row.action,
                    "source": row.source,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        )

    def _sync_figure_workspace_files(self, figures: list[PaperFigure], figure_dir: Path) -> None:
        for index, figure in enumerate(figures, start=1):
            label = self._figure_label(figure.caption, index)
            figure.figure_label = label
            crop_payload = self._figure_crop_payload(figure)
            figure.crop_status = crop_payload["crop_status"]
            figure.crop_confidence = crop_payload["crop_confidence"]
            figure.crop_source = crop_payload["crop_source"]
            src = resolve_persisted_artifact_path(
                figure.image_path,
                category="figures",
                settings=self.settings,
                trusted_persisted_reference=True,
            )
            if src is not None and src.exists():
                safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("._") or f"figure_{index}"
                target = figure_dir / f"{safe_label}{src.suffix.lower() or '.png'}"
                if src.resolve() != target.resolve():
                    shutil.copy2(src, target)
            self.session.add(figure)

    @staticmethod
    def _figure_label(caption: str | None, index: int) -> str:
        match = re.search(r"(?:figure|fig\.?|scheme)\s*([0-9]+[A-Za-z]?)", caption or "", re.IGNORECASE)
        return f"fig_{match.group(1)}" if match else f"fig_candidate_{index}"

    @staticmethod
    def _figure_crop_payload(figure: PaperFigure) -> dict[str, Any]:
        prov = figure.prov or []
        extraction = next(
            (item for item in reversed(prov) if isinstance(item, dict) and item.get("image_extraction")),
            None,
        )
        if figure.image_path and extraction:
            return {
                "crop_status": "candidate_crop",
                "crop_confidence": extraction.get("confidence"),
                "crop_source": extraction.get("source") or extraction.get("image_extraction"),
            }
        if figure.image_path:
            return {"crop_status": "needs_recrop", "crop_confidence": None, "crop_source": "legacy_image"}
        return {"crop_status": "caption_only", "crop_confidence": None, "crop_source": "caption"}

    def _render_page_previews(self, pdf_path: Path, pages_dir: Path) -> None:
        try:
            import fitz

            doc = fitz.open(str(pdf_path))
            try:
                for index, page in enumerate(doc, start=1):
                    out_path = pages_dir / f"page_{index:03d}.png"
                    if out_path.exists():
                        continue
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
                    pix.save(str(out_path))
            finally:
                doc.close()
        except Exception:
            return

    @staticmethod
    def _paper_metadata(paper: Paper) -> dict[str, Any]:
        return {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "paper_id": str(paper.id),
            "paper_code": getattr(paper, "paper_code", None),
            "library_name": paper.library_name,
            "serial_number": paper.serial_number,
            "title": paper.title,
            "doi": paper.doi,
            "year": paper.year,
            "journal": paper.journal,
            "authors": paper.authors,
            "pdf_path": paper.pdf_path,
            "markdown_path": paper.markdown_path,
            "docling_json_path": paper.docling_json_path,
            "workflow_status": paper.workflow_status,
            "pdf_quality_status": paper.pdf_quality_status,
        }
