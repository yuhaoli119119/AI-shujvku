from __future__ import annotations
from pathlib import Path
import re
from uuid import UUID
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import Settings
from app.db.models import AuditLog, Paper, PaperFigure, PaperRelationship, PaperTable
from app.services.figure_reading_service import FigureReadingService, compute_content_fingerprint
from app.utils.artifact_paths import resolve_persisted_artifact_path
from .figure_types import public_registry
from .models import PaperReviewBatchRequest
from .prompt import PROMPT_VERSION, public_prompt
from .snapshot import table_version, task_fingerprint

class ReviewTaskBuilder:
    def __init__(self, session: Session, settings: Settings):
        self.session, self.settings = session, settings
    def _source_ids(self, paper_id: UUID) -> list[UUID]:
        rels = self.session.scalars(select(PaperRelationship).where(PaperRelationship.source_paper_id == paper_id, PaperRelationship.relationship_type == "supplementary")).all()
        return [paper_id, *[r.target_paper_id for r in rels]]
    def _figure_source_fingerprint(self, row: PaperFigure) -> str:
        raw = None
        if row.image_path:
            path = resolve_persisted_artifact_path(row.image_path, category="figures", settings=self.settings, must_exist=True, trusted_persisted_reference=True)
            if path and Path(path).is_file():
                raw = Path(path).read_bytes()
        label_hint = row.figure_label if re.search(r"(?:Figure|Fig\.?)\s*[S]?\d+", row.figure_label or "", re.IGNORECASE) else (row.caption or row.figure_label)
        referenced = FigureReadingService(self.session, self.settings)._find_referencing_sections(row.paper_id, label_hint)
        section_payload = [
            (str(item.get("section_id") or ""), str(item.get("section_text") or ""))
            for item in referenced
        ]
        return compute_content_fingerprint(row.caption, raw, section_payload)
    def _figure_asset_integrity(self, row: PaperFigure) -> dict:
        if not row.image_path:
            return {"status": "not_configured", "exists": False, "decodable": False, "reason": "missing_image_path"}
        path = resolve_persisted_artifact_path(row.image_path, category="figures", settings=self.settings, must_exist=True, trusted_persisted_reference=True)
        if path is None or not Path(path).is_file():
            return {"status": "missing", "exists": False, "decodable": False, "reason": "missing_image_file"}
        try:
            with Image.open(path) as image:
                image.load()
                width, height, image_format = int(image.width), int(image.height), str(image.format or "")
        except Exception as exc:
            return {"status": "invalid", "exists": True, "decodable": False, "reason": f"image_decode_failed:{type(exc).__name__}"}
        return {"status": "ok", "exists": True, "decodable": True, "width": width, "height": height, "format": image_format}
    @staticmethod
    def _typing(reading):
        data = reading if isinstance(reading, dict) else {}
        return {"figure_type_primary": data.get("figure_type_primary"), "figure_type_display_zh": data.get("figure_type_display_zh"), "figure_types": data.get("figure_types") or [], "figure_type_names_zh": data.get("figure_type_names_zh") or [], "panel_types": data.get("panel_types") or [], "figure_role": data.get("figure_role"), "type_confidence": data.get("type_confidence"), "type_evidence": data.get("type_evidence") or [], "raw_type_name": data.get("raw_type_name"), "registry_version": data.get("figure_type_registry_version"), "legacy_type_mapping": {"status": "mapped" if data.get("figure_type_primary") else "unmapped", "raw_figure_role": data.get("figure_role")}}
    @staticmethod
    def _missing_subfigure_descriptions(reading) -> list[str]:
        data = reading if isinstance(reading, dict) else {}
        panel_labels = [str(item.get("label") or "").strip() for item in data.get("panel_types") or [] if isinstance(item, dict)]
        descriptions = {
            str(item.get("label") or "").strip(): str(item.get("description") or "").strip()
            for item in data.get("subfigures") or [] if isinstance(item, dict)
        }
        return [label for label in panel_labels if not descriptions.get(label) or not any("\u4e00" <= ch <= "\u9fff" for ch in descriptions[label])]
    def _figure_payload(self, figure: PaperFigure) -> dict:
        return {"figure_id": str(figure.id), "source_paper_id": str(figure.paper_id), "object_version": str(figure.write_version or 1), "figure_label": figure.figure_label, "page": figure.page, "caption": figure.caption, "image_path": figure.image_path, "asset_integrity": self._figure_asset_integrity(figure), "figure_role": figure.figure_role, "content_summary": figure.content_summary, "key_elements": figure.key_elements, "prov": figure.prov, "source_fingerprint": self._figure_source_fingerprint(figure), **self._typing(figure.reading_explanation), "reading": figure.reading_explanation}
    def build(self, paper_id: UUID) -> dict:
        main = self.session.get(Paper, paper_id)
        if not main: raise LookupError(f"Paper {paper_id} not found")
        source_ids = self._source_ids(paper_id)
        papers = [self.session.get(Paper, pid) for pid in source_ids]
        figures = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id.in_(source_ids)).order_by(PaperFigure.paper_id, PaperFigure.page.asc().nulls_last(), PaperFigure.figure_label)).all()
        tables = self.session.scalars(select(PaperTable).where(PaperTable.paper_id.in_(source_ids)).order_by(PaperTable.paper_id, PaperTable.page.asc().nulls_last(), PaperTable.id)).all()
        payload = {
            "schema_version": "paper_review_task_v2", "paper_id": str(main.id), "paper_code": main.paper_code, "title": main.title, "year": main.year, "journal": main.journal,
            "prompt_version": PROMPT_VERSION, "prompt": public_prompt(), "figure_type_registry": public_registry(),
            "source_documents": [{"paper_id": str(p.id), "paper_code": p.paper_code, "title": p.title, "pdf_path": p.pdf_path, "is_supplementary": p.id != paper_id} for p in papers if p],
            "figures": [self._figure_payload(f) for f in figures],
            "tables": [{"table_id": str(t.id), "source_paper_id": str(t.paper_id), "object_version": table_version(t), "page": t.page, "caption": t.caption, "markdown_content": t.markdown_content, "prov": t.prov} for t in tables],
            "review_schema": PaperReviewBatchRequest.model_json_schema(),
        }
        payload["task_fingerprint"] = task_fingerprint(payload)
        payload["object_counts"] = {"figures": len(figures), "tables": len(tables)}
        main_figures = [x for x in payload["figures"] if x["source_paper_id"] == str(paper_id)]
        linked_figures = [x for x in payload["figures"] if x["source_paper_id"] != str(paper_id)]
        missing_assets = [x for x in main_figures if x["asset_integrity"]["status"] == "missing"]
        invalid_assets = [x for x in main_figures if x["asset_integrity"]["status"] == "invalid"]
        subfigure_description_issues = {x["figure_label"] or x["figure_id"]: self._missing_subfigure_descriptions(x.get("reading")) for x in main_figures}
        subfigure_description_issues = {key: value for key, value in subfigure_description_issues.items() if value}
        stale_readings = [x for x in main_figures if isinstance(x.get("reading"), dict) and x["reading"].get("detailed_explanation_zh") and x["reading"].get("source_fingerprint") != x.get("source_fingerprint")]
        reading_done = sum(1 for x in main_figures if isinstance(x.get("reading"), dict) and x["reading"].get("detailed_explanation_zh") and x["reading"].get("source_fingerprint") == x.get("source_fingerprint") and x["asset_integrity"]["status"] not in {"missing", "invalid"} and not self._missing_subfigure_descriptions(x.get("reading")))
        stage = "in_progress" if figures or tables else "not_required"
        unresolved = 0
        latest = self.session.scalar(select(AuditLog).where(AuditLog.paper_id == paper_id, AuditLog.action == "paper_review_v2_receipt").order_by(AuditLog.created_at.desc(), AuditLog.id.desc()))
        if latest is not None and isinstance(latest.payload, dict):
            response = latest.payload.get("response") if isinstance(latest.payload.get("response"), dict) else {}
            auth = response.get("authoritative_readback") if isinstance(response.get("authoritative_readback"), dict) else {}
            if latest.payload.get("post_apply_task_fingerprint") == payload["task_fingerprint"]:
                stage = str(auth.get("stage_status") or stage)
                unresolved = int(auth.get("unresolved_count") or 0)
            else:
                stage = "stale"
                unresolved = 1
        delivery_issue_count = len(missing_assets) + len(invalid_assets) + len(stale_readings) + sum(len(labels) for labels in subfigure_description_issues.values())
        if delivery_issue_count:
            stage = "stale"
            unresolved = max(unresolved, delivery_issue_count)
        payload["status"] = {"chart_review_status": stage, "asset_status": "asset_missing" if missing_assets else ("asset_invalid" if invalid_assets else "ok"), "missing_image_count": len(missing_assets), "missing_image_labels": [x["figure_label"] or x["figure_id"] for x in missing_assets], "invalid_image_count": len(invalid_assets), "invalid_image_labels": [x["figure_label"] or x["figure_id"] for x in invalid_assets], "subfigure_empty_description_count": sum(len(labels) for labels in subfigure_description_issues.values()), "subfigure_description_issues": subfigure_description_issues, "stale_figure_reading_count": len(stale_readings), "stale_figure_reading_labels": [x["figure_label"] or x["figure_id"] for x in stale_readings], "linked_source_asset_issue_count": sum(1 for x in linked_figures if x["asset_integrity"]["status"] in {"missing", "invalid"}), "dft_gate_allowed": stage in {"completed", "completed_with_issues"} and unresolved == 0, "figure_reading_coverage": {"completed": reading_done, "total": len(main_figures)}, "legacy_issue_count": unresolved}
        return payload
