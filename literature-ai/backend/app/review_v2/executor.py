from __future__ import annotations
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import os
from typing import Any
from uuid import UUID, uuid4
from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from app.config import Settings
from app.db.models import AuditLog, Paper, PaperFigure, PaperTable, utcnow
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService
from .figure_types import REGISTRY, REGISTRY_VERSION, search_terms, validate_keys
from .models import FigureReadingInput, FigureReviewAction, FigureTyping, PaperReviewBatchRequest, TableReviewAction
from .receipts import RECEIPT_ACTION, find_receipt, read_receipt
from .snapshot import canonical_hash, table_version
from .state_machine import assert_contract, final_state
from .task_builder import ReviewTaskBuilder

class PaperReviewV2Service:
    def __init__(self, session: Session, settings: Settings):
        self.session, self.settings = session, settings
        self.builder = ReviewTaskBuilder(session, settings)
    def get_task(self, paper_id: UUID) -> dict: return self.builder.build(paper_id)
    def get_receipt(self, paper_id: UUID, request_id: str) -> dict: return read_receipt(self.session, paper_id, request_id)
    def _allowed_sources(self, task: dict) -> set[str]: return {x["paper_id"] for x in task["source_documents"]}
    def _prevalidate(self, request: PaperReviewBatchRequest, task: dict) -> None:
        if request.paper_id != task["paper_id"]: raise ValueError("paper_id_mismatch")
        if request.task_fingerprint != task["task_fingerprint"]: raise ValueError(f"stale_task_fingerprint:expected={task['task_fingerprint']}")
        figures = {x["figure_id"]: x for x in task["figures"]}; tables = {x["table_id"]: x for x in task["tables"]}; allowed = self._allowed_sources(task)
        for item in request.figure_actions:
            if item.action == "CREATE":
                if item.source_paper_id not in allowed: raise ValueError("figure_create_outside_main_or_linked_si")
            else:
                current = figures.get(str(item.figure_id))
                if not current: raise ValueError(f"figure_not_in_task:{item.figure_id}")
                if str(item.expected_object_version) != str(current["object_version"]): raise ValueError(f"figure_version_conflict:{item.figure_id}")
        for item in request.figure_readings:
            current = figures.get(str(item.figure_id))
            if not current: raise ValueError(f"figure_not_in_task:{item.figure_id}")
            if item.source_paper_id != current["source_paper_id"]: raise ValueError(f"figure_source_paper_mismatch:{item.figure_id}")
            if str(item.expected_object_version) != str(current["object_version"]): raise ValueError(f"figure_version_conflict:{item.figure_id}")
        for item in request.table_actions:
            if item.action == "CREATE":
                if item.source_paper_id not in allowed: raise ValueError("table_create_outside_main_or_linked_si")
            else:
                current = tables.get(str(item.table_id))
                if not current: raise ValueError(f"table_not_in_task:{item.table_id}")
                if str(item.expected_object_version) != str(current["object_version"]): raise ValueError(f"table_version_conflict:{item.table_id}")
        if request.final_state == "completed":
            covered_figures = {str(x.figure_id) for x in request.figure_actions if x.figure_id} | {str(x.figure_id) for x in request.figure_readings}
            covered_tables = {str(x.table_id) for x in request.table_actions if x.table_id}
            missing_figures = sorted(set(figures) - covered_figures)
            missing_tables = sorted(set(tables) - covered_tables)
            if missing_figures or missing_tables:
                raise ValueError(f"incomplete_scope_coverage:figures={missing_figures};tables={missing_tables}")
    def _validated_typing(self, typing: FigureTyping) -> dict[str, Any]:
        keys = validate_keys(typing.figure_types)
        if typing.figure_type_primary not in keys: raise ValueError("figure_type_primary_not_in_figure_types")
        panels = []
        for panel in typing.panel_types:
            panel_keys = validate_keys(panel.figure_types)
            if "other" in panel_keys and not (panel.raw_type_name or "").strip(): raise ValueError(f"panel_other_requires_raw_type_name:{panel.label}")
            panels.append({"label": panel.label, "figure_types": panel_keys, "type_names_zh": [REGISTRY[x].name_zh for x in panel_keys], "raw_type_name": panel.raw_type_name})
        evidence = [x.model_dump(mode="json", exclude_none=True) for x in typing.type_evidence]
        return {"figure_type_primary": typing.figure_type_primary, "figure_type_display_zh": REGISTRY[typing.figure_type_primary].name_zh, "figure_types": keys, "figure_type_names_zh": [REGISTRY[x].name_zh for x in keys], "panel_types": panels, "figure_role": typing.figure_role, "type_confidence": typing.type_confidence, "type_evidence": evidence, "raw_type_name": typing.raw_type_name, "figure_type_registry_version": REGISTRY_VERSION, "type_search_terms": search_terms(keys, typing.raw_type_name), "type_updated_at": datetime.now(timezone.utc).isoformat()}
    def _apply_typing(self, figure: PaperFigure, typing: FigureTyping) -> dict:
        payload = self._validated_typing(typing); reading = dict(figure.reading_explanation or {}); reading.update(payload); figure.reading_explanation = reading
        if typing.figure_role is not None: figure.figure_role = typing.figure_role
        figure.role_confidence = typing.type_confidence; flag_modified(figure, "reading_explanation"); return payload
    def _audit(self, *, main_paper_id: UUID, source: str, target_type: str, target_id: str, action: str, payload: dict) -> str:
        row = AuditLog(paper_id=main_paper_id, action=action, source=source[:64], target_type=target_type, target_id=target_id, payload=payload, created_at=utcnow())
        self.session.add(row); self.session.flush(); return str(row.id)
    def _render_crops(self, source_paper_id: UUID, crops: list[dict], *, figure_id: UUID | None = None) -> tuple[str, list[dict], dict]:
        paper = self.session.get(Paper, source_paper_id)
        if not paper: raise ValueError("source_paper_not_found")
        pdf = EvidenceReviewBundleService(self.session, self.settings)._resolve_pdf(paper.pdf_path)
        if pdf is None: raise ValueError("source_pdf_not_found")
        import fitz
        rendered: list[Image.Image] = []; provenance: list[dict] = []; doc = fitz.open(str(pdf))
        try:
            for crop in crops:
                page_no = int(crop["page"])
                if page_no < 1 or page_no > len(doc): raise ValueError(f"page_out_of_bounds:{page_no}")
                box = [float(x) for x in crop["bbox_norm"]]; page = doc[page_no - 1]; rect = page.rect
                clip = fitz.Rect(rect.x0 + box[0]*rect.width, rect.y0 + box[1]*rect.height, rect.x0 + box[2]*rect.width, rect.y0 + box[3]*rect.height).intersect(rect)
                if clip.is_empty: raise ValueError(f"empty_crop:{page_no}")
                pix = page.get_pixmap(matrix=fitz.Matrix(2,2), clip=clip, alpha=False); image = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB"); rendered.append(image)
                provenance.append({"page": page_no, "bbox_norm": box, "bbox_pdf": [clip.x0, clip.y0, clip.x1, clip.y1], "pixel_size": {"width": image.width, "height": image.height}})
        finally: doc.close()
        width = max(x.width for x in rendered); gap = 12 if len(rendered) > 1 else 0; height = sum(x.height for x in rendered) + gap*(len(rendered)-1)
        canvas = Image.new("RGB", (width, height), "white"); y = 0
        for image in rendered:
            x = (width-image.width)//2; canvas.paste(image, (x,y)); y += image.height+gap
        token = uuid4().hex[:12]; filename = f"{source_paper_id}_fig_{figure_id or 'new'}_v2_{token}.png"; rel = f"{source_paper_id}/{filename}"
        out = self.settings.storage_paths["figures"] / str(source_paper_id) / filename; out.parent.mkdir(parents=True, exist_ok=True); temp = out.with_suffix(".tmp.png")
        canvas.save(temp, format="PNG", optimize=True); os.replace(temp, out)
        return rel, provenance, {"width": width, "height": height, "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
    def _apply_figure_action(self, main_id: UUID, item: FigureReviewAction, source: str, task_fp: str) -> dict:
        if item.action == "HOLD": return {"category": "figure", "action": item.action, "target_id": item.figure_id, "outcome": "held", "reason": item.reason}
        before = None
        if item.action == "CREATE":
            figure = PaperFigure(paper_id=UUID(str(item.source_paper_id)), caption=item.caption, page=item.page, figure_label=item.figure_label, figure_role=item.figure_role, content_summary=item.content_summary, key_elements=item.key_elements or [], crop_status="ai_created_crop", crop_confidence=item.confidence, crop_source="paper_review_v2", write_version=1)
            self.session.add(figure); self.session.flush()
        else:
            figure = self.session.get(PaperFigure, UUID(str(item.figure_id)))
            if not figure: raise ValueError("figure_not_found")
            before = {"id": str(figure.id), "paper_id": str(figure.paper_id), "figure_label": figure.figure_label, "caption": figure.caption, "page": figure.page, "image_path": figure.image_path, "figure_role": figure.figure_role, "content_summary": figure.content_summary, "key_elements": figure.key_elements, "prov": figure.prov, "reading_explanation": figure.reading_explanation, "write_version": figure.write_version}
        if item.action == "DELETE_FALSE_POSITIVE":
            audit_id = self._audit(main_paper_id=main_id, source=source, target_type="paper_figure", target_id=str(figure.id), action="paper_review_v2_delete_false_positive", payload={"task_fingerprint": task_fp, "reason": item.reason, "evidence": [x.model_dump(mode="json", exclude_none=True) for x in item.evidence], "before": before, "recoverable_from_database_backup": True})
            self.session.delete(figure); self.session.flush(); return {"category": "figure", "action": item.action, "target_id": str(item.figure_id), "outcome": "applied", "audit_log_id": audit_id, "deleted": True}
        changed = item.action == "CREATE"
        for field in ("figure_label", "caption", "figure_role", "content_summary", "key_elements"):
            value = getattr(item, field)
            if value is not None and getattr(figure, field) != value: setattr(figure, field, value); changed = True
        crop_info = None
        if item.action in {"RECROP", "CREATE", "COMPOSE_PAGES"}:
            crops = [{"page": item.page, "bbox_norm": item.bbox_norm}] if item.action != "COMPOSE_PAGES" else [x.model_dump() for x in item.pages]
            rel, pages, size = self._render_crops(figure.paper_id, crops, figure_id=figure.id); figure.image_path = rel; figure.page = crops[0]["page"]
            figure.crop_status = "composed_pages" if item.action == "COMPOSE_PAGES" else ("ai_created_crop" if item.action == "CREATE" else "recropped"); figure.crop_source = "paper_review_v2"; figure.crop_confidence = item.confidence
            prov = list(figure.prov or []); prov.append({"action": "paper_review_v2_compose_pages" if item.action == "COMPOSE_PAGES" else "paper_review_v2_crop", "source_pages": pages, "page_range": [x["page"] for x in pages], "output": {"image_path": rel, **size}, "reason": item.reason, "task_fingerprint": task_fp})
            figure.prov = prov; flag_modified(figure, "prov"); crop_info = prov[-1]; changed = True
        typing_result = None
        if item.typing is not None: typing_result = self._apply_typing(figure, item.typing); changed = True
        if changed and item.action != "CREATE": figure.write_version = int(figure.write_version or 1) + 1
        self.session.add(figure); self.session.flush()
        audit_id = self._audit(main_paper_id=main_id, source=source, target_type="paper_figure", target_id=str(figure.id), action="paper_review_v2_figure_action", payload={"task_fingerprint": task_fp, "action": item.action, "reason": item.reason, "evidence": [x.model_dump(mode="json", exclude_none=True) for x in item.evidence], "before": before, "after_version": str(figure.write_version), "typing": typing_result, "crop": crop_info})
        return {"category": "figure", "action": item.action, "target_id": str(figure.id), "outcome": "applied" if changed else "unchanged", "audit_log_id": audit_id, "object_version": str(figure.write_version), "image_path": figure.image_path, "typing": typing_result, "crop": crop_info}
    def _apply_table_action(self, main_id: UUID, item: TableReviewAction, source: str, task_fp: str) -> dict:
        if item.action == "HOLD": return {"category": "table", "action": item.action, "target_id": item.table_id, "outcome": "held", "reason": item.reason}
        if item.action == "CREATE":
            table = PaperTable(paper_id=UUID(str(item.source_paper_id)), caption=item.caption, markdown_content=item.markdown_content, page=item.page, extraction_source="paper_review_v2", prov=[]); self.session.add(table); self.session.flush(); before = None
        else:
            table = self.session.get(PaperTable, UUID(str(item.table_id)))
            if not table: raise ValueError("table_not_found")
            before = {"id": str(table.id), "paper_id": str(table.paper_id), "caption": table.caption, "markdown_content": table.markdown_content, "page": table.page, "prov": table.prov}
        if item.action == "DELETE_FALSE_POSITIVE":
            audit_id = self._audit(main_paper_id=main_id, source=source, target_type="paper_table", target_id=str(table.id), action="paper_review_v2_delete_false_positive", payload={"task_fingerprint": task_fp, "reason": item.reason, "evidence": [x.model_dump(mode="json", exclude_none=True) for x in item.evidence], "before": before, "recoverable_from_database_backup": True})
            self.session.delete(table); self.session.flush(); return {"category": "table", "action": item.action, "target_id": str(item.table_id), "outcome": "applied", "audit_log_id": audit_id, "deleted": True}
        changed = item.action == "CREATE"
        for field in ("caption", "markdown_content", "page"):
            value = getattr(item, field)
            if value is not None and getattr(table, field) != value: setattr(table, field, value); changed = True
        if changed:
            table.extraction_source = "paper_review_v2"; table.prov = list(table.prov or []) + [{"action": item.action, "reason": item.reason, "task_fingerprint": task_fp}]; flag_modified(table, "prov")
        self.session.add(table); self.session.flush(); audit_id = self._audit(main_paper_id=main_id, source=source, target_type="paper_table", target_id=str(table.id), action="paper_review_v2_table_action", payload={"task_fingerprint": task_fp, "action": item.action, "reason": item.reason, "before": before, "after_version": table_version(table)})
        return {"category": "table", "action": item.action, "target_id": str(table.id), "outcome": "applied" if changed else "unchanged", "audit_log_id": audit_id, "object_version": table_version(table)}
    def _apply_reading(self, main_id: UUID, item: FigureReadingInput, source: str, task_fp: str) -> dict:
        figure = self.session.get(PaperFigure, UUID(item.figure_id))
        if not figure: raise ValueError("figure_not_found")
        old = dict(figure.reading_explanation or {}); typing = self._apply_typing(figure, item.typing); source_fp = self.builder._figure_source_fingerprint(figure); reading_version = int(old.get("reading_version") or old.get("version") or 0) + 1
        merged = dict(figure.reading_explanation or {}); merged.update({"reading_version": reading_version, "summary_zh": item.summary_zh, "detailed_explanation_zh": item.detailed_explanation_zh, "evidence_locators": [x.model_dump(mode="json", exclude_none=True) for x in item.evidence_locators], "uncertainties_zh": item.uncertainties_zh, "subfigures": [x.model_dump(mode="json", exclude_none=True) for x in item.subfigures], "source_fingerprint": source_fp, "is_stale": False, "model": "paper_review_v2", "updated_at": datetime.now(timezone.utc).isoformat()})
        figure.reading_explanation = merged; figure.write_version = int(figure.write_version or 1) + 1; flag_modified(figure, "reading_explanation"); self.session.add(figure); self.session.flush()
        audit_id = self._audit(main_paper_id=main_id, source=source, target_type="paper_figure", target_id=str(figure.id), action="paper_review_v2_figure_reading", payload={"task_fingerprint": task_fp, "source_fingerprint": source_fp, "reading_version": reading_version, "typing": typing, "previous_source_fingerprint": old.get("source_fingerprint")})
        return {"category": "figure_reading", "action": "UPDATE", "target_id": str(figure.id), "outcome": "applied", "audit_log_id": audit_id, "object_version": str(figure.write_version), "source_fingerprint": source_fp, "reading_version": reading_version}
    def apply(self, request: PaperReviewBatchRequest) -> dict:
        paper_id = UUID(request.paper_id); self.session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": f"paper_review_v2:{paper_id}"})
        payload_hash = canonical_hash(request.model_dump(mode="json")); existing = find_receipt(self.session, paper_id, request.request_id)
        if existing:
            stored = existing.payload if isinstance(existing.payload, dict) else {}
            if stored.get("payload_hash") != payload_hash: raise ValueError("request_id_payload_conflict")
            return {**stored.get("response", {}), "idempotent_replay": True, "receipt_audit_id": str(existing.id)}
        task = self.builder.build(paper_id); self._prevalidate(request, task); applied, unchanged, held, rejected = [], [], [], []
        work = [("figure", x) for x in request.figure_actions] + [("table", x) for x in request.table_actions] + [("reading", x) for x in request.figure_readings]
        for category, item in work:
            try:
                with self.session.begin_nested():
                    if category == "figure": result = self._apply_figure_action(paper_id, item, request.reviewer_label, task["task_fingerprint"])
                    elif category == "table": result = self._apply_table_action(paper_id, item, request.reviewer_label, task["task_fingerprint"])
                    else: result = self._apply_reading(paper_id, item, request.reviewer_label, task["task_fingerprint"])
                {"applied": applied, "unchanged": unchanged, "held": held}.get(result["outcome"], rejected).append(result)
            except Exception as exc:
                rejected.append({"category": category, "target_id": getattr(item, "figure_id", None) or getattr(item, "table_id", None), "outcome": "rejected", "error": str(exc)})
        self.session.flush(); readback_task = self.builder.build(paper_id); stage = final_state(item_count=len(work), held=len(held), rejected=len(rejected), requested=request.final_state); unresolved = len(held) + len(rejected)
        integrity_unresolved = int(readback_task["status"].get("missing_image_count") or 0) + int(readback_task["status"].get("invalid_image_count") or 0) + int(readback_task["status"].get("subfigure_empty_description_count") or 0) + int(readback_task["status"].get("stale_figure_reading_count") or 0)
        if integrity_unresolved:
            stage = "stale"
            unresolved += integrity_unresolved
        assert_contract(stage, unresolved)
        touched_ids = {str(x.get("target_id")) for x in [*applied, *unchanged, *held, *rejected] if x.get("target_id")}
        authoritative = {"paper_id": str(paper_id), "task_fingerprint": readback_task["task_fingerprint"], "stage_status": stage, "unresolved_count": unresolved, "chart_review_status": stage, "asset_status": readback_task["status"]["asset_status"], "missing_image_count": readback_task["status"]["missing_image_count"], "missing_image_labels": readback_task["status"]["missing_image_labels"], "invalid_image_count": readback_task["status"]["invalid_image_count"], "invalid_image_labels": readback_task["status"]["invalid_image_labels"], "subfigure_empty_description_count": readback_task["status"]["subfigure_empty_description_count"], "subfigure_description_issues": readback_task["status"]["subfigure_description_issues"], "stale_figure_reading_count": readback_task["status"]["stale_figure_reading_count"], "stale_figure_reading_labels": readback_task["status"]["stale_figure_reading_labels"], "dft_gate_allowed": stage in {"completed", "completed_with_issues"} and unresolved == 0, "figure_reading_coverage": readback_task["status"]["figure_reading_coverage"], "legacy_issue_count": unresolved, "figures": [x for x in readback_task["figures"] if x["figure_id"] in touched_ids], "tables": [x for x in readback_task["tables"] if x["table_id"] in touched_ids], "active_figure_order": [x["figure_id"] for x in readback_task["figures"]], "active_table_order": [x["table_id"] for x in readback_task["tables"]]}
        response = {"schema_version": "paper_review_batch_receipt_v2", "paper_id": str(paper_id), "request_id": request.request_id, "request_payload_hash": payload_hash, "idempotent_replay": False, "applied": applied, "unchanged": unchanged, "held": held, "rejected": rejected, "authoritative_readback": authoritative}
        receipt = AuditLog(paper_id=paper_id, action=RECEIPT_ACTION, source=request.reviewer_label[:64], target_type="paper_review_v2", target_id=request.request_id, payload={"payload_hash": payload_hash, "task_fingerprint": request.task_fingerprint, "post_apply_task_fingerprint": readback_task["task_fingerprint"], "response": response}, created_at=utcnow())
        self.session.add(receipt); self.session.flush(); response["receipt_audit_id"] = str(receipt.id); receipt.payload = {**receipt.payload, "response": response}; flag_modified(receipt, "payload"); return response
