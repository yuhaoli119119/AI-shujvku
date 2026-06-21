from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    EvidenceLocator,
    MechanismClaim,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.services.module_write_lock_service import ModuleWriteLockService
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.evidence_anchors import first_evidence_anchor, has_evidence_anchor, has_material_correction_anchor
from app.utils.figure_delete_policy import direct_delete_eligibility, normalized_figure_identity
from app.utils.figure_summary import normalize_figure_content_summary, normalize_figure_key_elements
from app.services.catalyst_sample_identity import clean_sample_payload, resolve_sample_identity


@dataclass(frozen=True)
class StructuredTargetSpec:
    model: type
    allowed_fields: frozenset[str]


class ReviewService:
    TRUSTED_LOCK_BYPASS_REVIEWERS = {"admin", "human", "curator", "system"}
    DIRECT_AI_LOCK_REVIEWERS = {"ide_ai", "ai_writer", "codex", "gemini", "claude", "glm", "openai", "chatgpt"}
    DIRECT_AI_LOCK_PREFIXES = ("ai_", "ide_ai", "codex_", "gemini_", "claude_", "glm_", "openai_")
    ALLOWED_PAPER_FIELDS = {
        "doi",
        "title",
        "year",
        "journal",
        "authors",
        "abstract",
        "oa_status",
        "license",
        "paper_type",
        "type_confidence",
        "classification_source",
    }
    ALLOWED_DFT_RESULT_FIELDS = {
        "catalyst_sample_id",
        "adsorbate",
        "property_type",
        "value",
        "unit",
        "reaction_step",
        "source_section",
        "source_figure",
        "evidence_text",
        "confidence",
    }
    STRUCTURED_TARGETS = {
        "dft_results": StructuredTargetSpec(
            model=DFTResult,
            allowed_fields=frozenset(ALLOWED_DFT_RESULT_FIELDS),
        ),
        "mechanism_claims": StructuredTargetSpec(
            model=MechanismClaim,
            allowed_fields=frozenset({"claim_type", "claim_text", "evidence_types", "confidence", "evidence_text"}),
        ),
        "electrochemical_performance": StructuredTargetSpec(
            model=ElectrochemicalPerformance,
            allowed_fields=frozenset(
                {
                    "sulfur_loading_mg_cm2",
                    "sulfur_content_wt_percent",
                    "electrolyte_sulfur_ratio",
                    "capacity_value",
                    "cycle_number",
                    "rate",
                    "decay_per_cycle",
                    "evidence_text",
                }
            ),
        ),
        "catalyst_samples": StructuredTargetSpec(
            model=CatalystSample,
            allowed_fields=frozenset(
                {
                    "name",
                    "catalyst_type",
                    "metal_centers",
                    "coordination",
                    "support",
                    "synthesis_method",
                    "evidence_strength",
                }
            ),
        ),
        "dft_settings": StructuredTargetSpec(
            model=DFTSetting,
            allowed_fields=frozenset(
                {
                    "software",
                    "functional",
                    "dispersion_correction",
                    "pseudopotential",
                    "cutoff_energy_ev",
                    "k_points",
                    "convergence_settings",
                    "vacuum_thickness_a",
                    "raw_json",
                }
            ),
        ),
        "writing_cards": StructuredTargetSpec(
            model=WritingCard,
            allowed_fields=frozenset(
                {
                    "paper_type",
                    "research_gap",
                    "proposed_solution",
                    "core_hypothesis",
                    "evidence_chain",
                    "section_strategy",
                    "figure_logic",
                    "abstract_logic",
                    "introduction_logic",
                    "discussion_logic",
                }
            ),
        ),
        "figures": StructuredTargetSpec(
            model=PaperFigure,
            allowed_fields=frozenset(
                {
                    "caption",
                    "image_path",
                    "page",
                    "figure_role",
                    "role_confidence",
                    "content_summary",
                    "key_elements",
                    "prov",
                    "figure_label",
                    "crop_status",
                    "crop_confidence",
                    "crop_source",
                }
            ),
        ),
        "tables": StructuredTargetSpec(
            model=PaperTable,
            allowed_fields=frozenset({"caption", "markdown_content", "page", "extraction_source", "prov"}),
        ),
        "sections": StructuredTargetSpec(
            model=PaperSection,
            allowed_fields=frozenset({"section_title", "section_type", "text", "page_start", "page_end"}),
        ),
    }
    STRUCTURED_DELETE_TARGETS = frozenset({"figures"})
    STRUCTURED_CREATE_TARGETS = frozenset(
        {
            "figures",
            "tables",
            "sections",
            "writing_cards",
            "mechanism_claims",
            "electrochemical_performance",
            "catalyst_samples",
        }
    )

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_corrections(self, status: str | None = "pending") -> list[PaperCorrection]:
        stmt = select(PaperCorrection).order_by(PaperCorrection.created_at.desc())
        if status:
            stmt = stmt.where(PaperCorrection.status == status)
        return self.session.scalars(stmt).all()

    def approve_correction(
        self,
        correction_id: UUID,
        reviewer: str,
        write_lock_tokens: list[str] | None = None,
        write_lock_owner: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> PaperCorrection:
        correction = self._get_correction(correction_id)
        if correction.status != "pending":
            raise ValueError("write_conflict:correction_not_pending")
        self._require_module_lock_for_direct_ai_write(
            correction,
            reviewer=reviewer,
            write_lock_tokens=write_lock_tokens,
            write_lock_owner=write_lock_owner,
        )
        if self._is_figure_recrop_correction(correction):
            return self._approve_figure_recrop_correction(correction_id, reviewer)
        claimed = self.session.execute(
            update(PaperCorrection)
            .where(PaperCorrection.id == correction_id, PaperCorrection.status == "pending")
            .values(status="applying")
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ValueError("write_conflict:correction_version_stale")

        correction.status = "approved"
        correction.reviewed_by = reviewer
        correction.reviewed_at = datetime.utcnow()
        self._apply_correction(correction)
        self.session.add(correction)
        self.session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action="approve_correction",
                source=reviewer,
                target_type="paper_correction",
                target_id=str(correction.id),
                payload={
                    "field_name": correction.field_name,
                    "target_path": correction.target_path,
                    "operation": correction.operation,
                },
            )
        )
        self.session.flush()
        self.session.refresh(correction)
        return correction

    def get_correction_detail(self, correction_id: UUID) -> dict[str, Any]:
        correction = self._get_correction(correction_id)
        try:
            current_value = self._resolve_current_value(correction)
            target_exists = True
        except ValueError:
            current_value = None
            target_exists = False

        return {
            "correction": correction,
            "current_value": current_value,
            "target_exists": target_exists,
        }

    def propose_figure_deletion(
        self,
        *,
        paper_id: UUID,
        figure_id: UUID,
        reason: str,
        reviewer: str,
        evidence_payload: dict[str, Any] | list[Any] | None = None,
    ) -> PaperCorrection:
        if not str(reason or "").strip():
            raise ValueError("A deletion reason is required.")
        figure = self.session.get(PaperFigure, figure_id)
        if figure is None or figure.paper_id != paper_id:
            raise ValueError("Figure not found for this paper.")
        payload = evidence_payload
        if not payload:
            payload = {
                "page": figure.page,
                "figure_label": figure.figure_label,
                "quoted_text": figure.caption or figure.content_summary or f"Figure object {figure.id}",
            }
        if not has_evidence_anchor(payload):
            raise ValueError("Figure deletion proposals require a PDF evidence anchor.")
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name="figures",
            target_path=f"figures:{figure_id}:delete",
            operation="delete",
            proposed_value=None,
            reason=str(reason).strip(),
            evidence_payload=payload,
            status="pending",
        )
        self.session.add(correction)
        self.session.flush()
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="propose_figure_deletion",
                source=reviewer,
                target_type="paper_correction",
                target_id=str(correction.id),
                payload={
                    "figure_id": str(figure_id),
                    "target_path": correction.target_path,
                    "operation": "delete",
                },
            )
        )
        self.session.flush()
        self.session.refresh(correction)
        return correction

    def direct_delete_figure(
        self,
        *,
        paper_id: UUID,
        figure_id: UUID,
        reason: str,
        reviewer: str,
        evidence_payload: dict[str, Any] | list[Any] | None = None,
    ) -> tuple[PaperCorrection, str | None, list[str]]:
        if not str(reason or "").strip():
            raise ValueError("A deletion reason is required.")
        figure = self.session.get(PaperFigure, figure_id)
        if figure is None or figure.paper_id != paper_id:
            raise ValueError("Figure not found for this paper.")
        duplicate_group_size = self._figure_duplicate_group_size(figure)
        allowed, policy_reason = direct_delete_eligibility(figure, duplicate_group_size=duplicate_group_size)
        if not allowed:
            raise ValueError("direct_delete_not_allowed:figure_not_duplicate_or_noise")
        payload = evidence_payload
        if not payload:
            payload = {
                "page": figure.page,
                "figure_label": figure.figure_label,
                "quoted_text": figure.caption or figure.content_summary or f"Figure object {figure.id}",
            }
        if not has_evidence_anchor(payload):
            raise ValueError("Figure deletion requires a PDF evidence anchor.")
        image_path = figure.image_path
        correction = PaperCorrection(
            paper_id=paper_id,
            source=reviewer,
            field_name="figures",
            target_path=f"figures:{figure_id}:delete",
            operation="delete",
            proposed_value=None,
            reason=str(reason).strip(),
            evidence_payload=payload,
            status="approved",
            reviewed_at=datetime.utcnow(),
            reviewed_by=reviewer,
        )
        self.session.add(correction)
        self.session.flush()
        self._apply_structured_delete(correction)
        retired_ids = self._retire_direct_delete_pending_figure_corrections(
            paper_id=paper_id,
            figure_id=figure_id,
            reviewer=reviewer,
            keep_correction_id=correction.id,
        )
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="direct_delete_figure",
                source=reviewer,
                target_type="figures",
                target_id=str(figure_id),
                payload={
                    "reason": correction.reason,
                    "policy_reason": policy_reason,
                    "source_correction_id": str(correction.id),
                    "retired_correction_ids": retired_ids,
                },
            )
        )
        self.session.flush()
        self.session.refresh(correction)
        return correction, image_path, retired_ids

    def reject_correction(self, correction_id: UUID, reviewer: str, reason: str | None = None) -> PaperCorrection:
        correction = self._get_correction(correction_id)
        if correction.status != "pending":
            raise ValueError("Correction is not pending")

        correction.status = "rejected"
        correction.reviewed_by = reviewer
        correction.reviewed_at = datetime.utcnow()
        self.session.add(correction)
        self.session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action="reject_correction",
                source=reviewer,
                target_type="paper_correction",
                target_id=str(correction.id),
                payload={"reason": reason} if reason else None,
            )
        )
        self.session.flush()
        self.session.refresh(correction)
        return correction

    def approve_corrections_batch(
        self,
        correction_ids: list[UUID],
        reviewer: str,
        write_lock_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        approved: list[PaperCorrection] = []
        skipped: list[dict[str, Any]] = []
        for cid in correction_ids:
            try:
                correction = self.approve_correction(cid, reviewer, write_lock_tokens=write_lock_tokens)
                approved.append(correction)
            except Exception as exc:
                skipped.append({"correction_id": str(cid), "reason": str(exc)})
        return {
            "total_requested": len(correction_ids),
            "approved": len(approved),
            "skipped": len(skipped),
            "approved_ids": [str(c.id) for c in approved],
            "skipped_items": skipped,
        }

    def reject_corrections_batch(self, correction_ids: list[UUID], reviewer: str, reason: str | None = None) -> dict[str, Any]:
        rejected: list[PaperCorrection] = []
        skipped: list[dict[str, Any]] = []
        for cid in correction_ids:
            try:
                correction = self.reject_correction(cid, reviewer, reason)
                rejected.append(correction)
            except Exception as exc:
                skipped.append({"correction_id": str(cid), "reason": str(exc)})
        return {
            "total_requested": len(correction_ids),
            "rejected": len(rejected),
            "skipped": len(skipped),
            "rejected_ids": [str(c.id) for c in rejected],
            "skipped_items": skipped,
        }

    def _apply_correction(self, correction: PaperCorrection) -> None:
        if correction.operation == "recrop_figure" and correction.field_name == "figures":
            self._apply_figure_recrop_correction(correction)
            return
        if correction.operation == "delete" and correction.field_name == "figures":
            self._apply_structured_delete(correction)
            return
        if correction.operation == "create" and correction.field_name == "catalyst_samples":
            self._apply_catalyst_sample_create(correction)
            return
        if correction.operation == "create" and correction.field_name in self.STRUCTURED_CREATE_TARGETS:
            self._apply_structured_create(correction)
            return
        if correction.operation != "replace":
            raise ValueError("Only replace corrections and approved structured creation are supported in the current review flow")

        if self._is_top_level_paper_correction(correction):
            paper = self.session.get(Paper, correction.paper_id)
            if not paper:
                raise ValueError("Paper not found")

            setattr(paper, correction.field_name, correction.proposed_value)
            self.session.add(paper)
            return

        if correction.field_name in self.STRUCTURED_TARGETS:
            self._apply_structured_correction(correction)
            return

        raise ValueError(f"Correction field is not review-applicable yet: {correction.field_name}")

    @staticmethod
    def _is_figure_recrop_correction(correction: PaperCorrection) -> bool:
        return correction.operation == "recrop_figure" and correction.field_name == "figures"

    def _approve_figure_recrop_correction(
        self,
        correction_id: UUID,
        reviewer: str,
    ) -> PaperCorrection:
        correction = self._get_correction(correction_id)
        recrop_plan = self._prepare_figure_recrop_plan(correction)
        # Close the short read transaction before PDF rendering/file I/O.
        self.session.rollback()

        rendered: dict[str, Any] | None = None
        try:
            rendered = self._render_figure_recrop_plan(recrop_plan)
            claimed = self.session.execute(
                update(PaperCorrection)
                .where(PaperCorrection.id == correction_id, PaperCorrection.status == "pending")
                .values(status="applying")
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise ValueError("write_conflict:correction_version_stale")

            correction = self._get_correction(correction_id)
            prov_entry = {
                "action": "recrop_figure",
                "strategy": recrop_plan["rect_kind"],
                "bbox": {
                    "l": rendered["bbox_used"][0],
                    "t": rendered["bbox_used"][1],
                    "r": rendered["bbox_used"][2],
                    "b": rendered["bbox_used"][3],
                    "coord_origin": "TOPLEFT",
                },
                "pixel_size": rendered["pixel_size"],
                "previous_path": recrop_plan["old_image_path"],
                "recropped_by": reviewer,
                "source_correction_id": str(correction.id),
            }
            next_prov = list(recrop_plan["prov"])
            next_prov.append(prov_entry)
            updated = self.session.execute(
                update(PaperFigure)
                .where(
                    PaperFigure.id == recrop_plan["figure_id"],
                    PaperFigure.write_version == recrop_plan["write_version"],
                )
                .values(
                    image_path=rendered["rel_path"],
                    crop_status="recropped",
                    crop_source=f"recrop:{recrop_plan['rect_kind']}:review_service",
                    crop_confidence=0.9 if recrop_plan["rect_kind"] == "ai_bbox" else 0.8,
                    prov=next_prov,
                    write_version=recrop_plan["write_version"] + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if updated.rowcount != 1:
                if self.session.get(PaperFigure, recrop_plan["figure_id"]) is None:
                    raise ValueError(
                        f"Figure {recrop_plan['figure_id']} not found during write-back (race condition)"
                    )
                raise ValueError("write_conflict:figure_version_stale")

            correction.status = "approved"
            correction.reviewed_by = reviewer
            correction.reviewed_at = datetime.utcnow()
            correction.evidence_payload = {
                **dict(correction.evidence_payload or {}),
                "recrop_result": {
                    "figure_id": str(recrop_plan["figure_id"]),
                    "image_path": rendered["rel_path"],
                    "strategy": recrop_plan["rect_kind"],
                    "bbox_used": rendered["bbox_used"],
                    "pixel_size": rendered["pixel_size"],
                },
            }
            self.session.add(correction)
            self.session.add(
                AuditLog(
                    paper_id=correction.paper_id,
                    action="recrop_figure",
                    source=correction.reviewed_by or correction.source,
                    target_type="paper_figure",
                    target_id=str(recrop_plan["figure_id"]),
                    payload={
                        "strategy": recrop_plan["rect_kind"],
                        "new_bbox": rendered["bbox_used"],
                        "new_image_path": rendered["rel_path"],
                        "old_image_path": recrop_plan["old_image_path"],
                        "source_correction_id": str(correction.id),
                    },
                )
            )
            self.session.flush()
            return correction
        except Exception:
            self.session.rollback()
            if rendered is not None:
                rendered["abs_path"].unlink(missing_ok=True)
            raise

    def _apply_figure_recrop_correction(self, correction: PaperCorrection) -> None:
        reviewer = correction.reviewed_by or correction.source
        approved = self._approve_figure_recrop_correction(correction.id, reviewer)
        correction.status = approved.status
        correction.reviewed_by = approved.reviewed_by
        correction.reviewed_at = approved.reviewed_at
        correction.evidence_payload = approved.evidence_payload
        flag_modified(correction, "evidence_payload")

    def _resolve_figure_for_recrop(self, correction: PaperCorrection) -> PaperFigure:
        try:
            collection, row_id_text, _ = self._parse_structured_target_path(correction.target_path)
            if collection == "figures" and row_id_text not in {"", "new"}:
                figure = self.session.get(PaperFigure, UUID(row_id_text))
                if figure and figure.paper_id == correction.paper_id:
                    return figure
        except Exception:
            pass
        proposed = correction.proposed_value if isinstance(correction.proposed_value, dict) else {}
        evidence = correction.evidence_payload if isinstance(correction.evidence_payload, dict) else {}
        label = self._normalized_text(proposed.get("figure_label") or evidence.get("figure_label") or evidence.get("figure"))
        page = proposed.get("page") or evidence.get("page")
        rows = self.session.scalars(select(PaperFigure).where(PaperFigure.paper_id == correction.paper_id)).all()
        if label:
            for row in rows:
                if self._normalized_text(row.figure_label) == label:
                    return row
        if page not in (None, ""):
            try:
                page_num = int(page)
            except (TypeError, ValueError):
                page_num = None
            if page_num is not None:
                page_rows = [row for row in rows if row.page == page_num]
                if len(page_rows) == 1:
                    return page_rows[0]
        raise ValueError("Figure target for recrop could not be resolved")

    @staticmethod
    def _parse_recrop_payload(value: Any) -> tuple[str, list[float]]:
        payload = value if isinstance(value, dict) else {}
        strategy = str(payload.get("strategy") or "").strip().lower()
        raw_bbox = payload.get("bbox", payload.get("new_bbox"))
        if isinstance(raw_bbox, str) and raw_bbox.strip().lower() == "full_page":
            return "full_page", []
        if strategy == "full_page":
            return "full_page", []
        if isinstance(raw_bbox, dict):
            raw_bbox = [
                raw_bbox.get("l", raw_bbox.get("x0")),
                raw_bbox.get("t", raw_bbox.get("y0")),
                raw_bbox.get("r", raw_bbox.get("x1")),
                raw_bbox.get("b", raw_bbox.get("y1")),
            ]
        elif raw_bbox is None and all(key in payload for key in ("l", "t", "r", "b")):
            raw_bbox = [payload.get("l"), payload.get("t"), payload.get("r"), payload.get("b")]
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            try:
                bbox = [float(item) for item in raw_bbox]
            except (TypeError, ValueError) as exc:
                raise ValueError("recrop_figure bbox values must be numeric") from exc
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                raise ValueError("recrop_figure bbox must be [left, top, right, bottom]")
            return "ai_bbox", bbox
        raise ValueError("recrop_figure requires strategy='full_page' or numeric bbox/new_bbox")

    def _prepare_figure_recrop_plan(self, correction: PaperCorrection) -> dict[str, Any]:
        figure = self._resolve_figure_for_recrop(correction)
        paper = self.session.get(Paper, correction.paper_id)
        if not paper or not paper.pdf_path:
            raise ValueError("Paper PDF is missing; cannot recrop figure")
        settings = get_settings()
        pdf_path = resolve_persisted_artifact_path(
            paper.pdf_path,
            category="pdf",
            settings=settings,
            trusted_persisted_reference=True,
        )
        if pdf_path is None or not pdf_path.exists():
            raise ValueError("Paper PDF file is missing on disk; cannot recrop figure")
        if figure.page is None or int(figure.page) < 1:
            raise ValueError("Figure page is missing; cannot recrop figure")
        rect_kind, bbox = self._parse_recrop_payload(correction.proposed_value)
        prov = figure.prov or []
        if isinstance(prov, dict):
            prov = [prov]
        return {
            "paper_id": correction.paper_id,
            "figure_id": figure.id,
            "page": int(figure.page),
            "rect_kind": rect_kind,
            "bbox": bbox,
            "pdf_path": pdf_path,
            "old_image_path": figure.image_path,
            "prov": list(prov),
            "write_version": int(figure.write_version or 1),
        }

    def _render_figure_recrop_plan(self, recrop_plan: dict[str, Any]) -> dict[str, Any]:
        settings = get_settings()
        import fitz
        import uuid as _uuid

        doc = fitz.open(str(recrop_plan["pdf_path"]))
        try:
            page_index = recrop_plan["page"] - 1
            if page_index < 0 or page_index >= len(doc):
                raise ValueError("Figure page is outside the PDF page range")
            page = doc[page_index]
            target_rect = page.rect if recrop_plan["rect_kind"] == "full_page" else fitz.Rect(*recrop_plan["bbox"])
            target_rect = target_rect.intersect(page.rect)
            if target_rect.is_empty:
                raise ValueError("Requested crop bbox is empty or outside the page")
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=target_rect, alpha=False)
            if pix.width < 16 or pix.height < 16:
                raise ValueError("Requested crop bbox produced an image that is too small")
            filename = f"{recrop_plan['paper_id']}_fig_{_uuid.uuid4().hex[:8]}.png"
            rel_path = f"{recrop_plan['paper_id']}/{filename}"
            abs_path = settings.storage_paths["figures"] / str(recrop_plan["paper_id"]) / filename
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            pix.save(str(abs_path))
            return {
                "rel_path": rel_path,
                "abs_path": abs_path,
                "pixel_size": {"width": pix.width, "height": pix.height},
                "bbox_used": [target_rect.x0, target_rect.y0, target_rect.x1, target_rect.y1],
            }
        finally:
            doc.close()

    def _apply_catalyst_sample_create(self, correction: PaperCorrection) -> None:
        if not has_material_correction_anchor(correction.evidence_payload):
            raise ValueError(
                "Catalyst sample creation requires at least one PDF evidence anchor: "
                "page, section, quoted_text, table, or figure."
            )
        collection, row_id_text, attribute = self._parse_structured_target_path(correction.target_path)
        if collection != "catalyst_samples" or row_id_text != "new" or attribute != "create":
            raise ValueError("Catalyst sample creation target must be catalyst_samples:new:create")
        proposed = dict(correction.proposed_value or {})
        cleaned = clean_sample_payload(proposed)
        resolution = resolve_sample_identity(
            self.session,
            paper_id=correction.paper_id,
            proposed_value=proposed,
        )
        if resolution.status == "ambiguous":
            correction.status = "requires_resolution"
            correction.evidence_payload = {
                **dict(correction.evidence_payload or {}),
                "sample_resolution": {
                    "status": "ambiguous",
                    "candidate_ids": list(resolution.candidate_ids),
                },
            }
            raise ValueError("Catalyst sample identity is ambiguous; manual resolution is required.")
        sample = resolution.sample
        if sample is None:
            sample = CatalystSample(paper_id=correction.paper_id, **cleaned)
            self.session.add(sample)
            self.session.flush()
        correction.evidence_payload = {
            **dict(correction.evidence_payload or {}),
            "sample_resolution": {
                "status": resolution.status,
                "catalyst_sample_id": str(sample.id),
            },
        }
        self.session.add(correction)
        self.session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action="create_or_reuse_catalyst_sample",
                source=correction.reviewed_by or correction.source,
                target_type="catalyst_sample",
                target_id=str(sample.id),
                payload={
                    "resolution": resolution.status,
                    "source_correction_id": str(correction.id),
                    "proposed_identity": proposed,
                    "evidence_anchor": correction.evidence_payload,
                },
            )
        )

    def _require_module_lock_for_direct_ai_write(
        self,
        correction: PaperCorrection,
        *,
        reviewer: str,
        write_lock_tokens: list[str] | None,
        write_lock_owner: str | list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> None:
        if not self._requires_non_dft_module_lock(correction):
            return
        if not self._reviewer_requires_module_lock(reviewer):
            return
        target_path = correction.field_name if self._is_top_level_paper_correction(correction) else correction.target_path
        module = ModuleWriteLockService.module_from_field(correction.field_name, target_path)
        ModuleWriteLockService(self.session).require_write(
            paper_id=correction.paper_id,
            module_names=[module],
            lock_tokens=write_lock_tokens,
            locked_by=write_lock_owner or reviewer,
        )

    @classmethod
    def _reviewer_requires_module_lock(cls, reviewer: str) -> bool:
        normalized = str(reviewer or "").strip().lower()
        if not normalized or normalized in cls.TRUSTED_LOCK_BYPASS_REVIEWERS:
            return False
        if normalized in cls.DIRECT_AI_LOCK_REVIEWERS:
            return True
        return normalized.startswith(cls.DIRECT_AI_LOCK_PREFIXES)

    def _requires_non_dft_module_lock(self, correction: PaperCorrection) -> bool:
        if self._is_top_level_paper_correction(correction):
            return True
        return correction.field_name in {
            "figures",
            "tables",
            "sections",
            "writing_cards",
            "mechanism_claims",
            "electrochemical_performance",
            "catalyst_samples",
        }

    def _apply_structured_correction(self, correction: PaperCorrection) -> None:
        record, spec, attribute = self._resolve_structured_target(correction)
        proposed_value = correction.proposed_value
        if spec.model is PaperFigure and attribute == "image_path":
            proposed_value = self._validated_relative_artifact_path(proposed_value)
        if spec.model is PaperFigure and attribute == "key_elements":
            proposed_value, _detail = normalize_figure_key_elements(proposed_value)
        if spec.model is CatalystSample and not has_material_correction_anchor(correction.evidence_payload):
            raise ValueError(
                "Catalyst sample corrections require at least one PDF evidence anchor: "
                "page, section, quoted_text, table, or figure."
            )
        if isinstance(record, DFTResult) and attribute == "catalyst_sample_id":
            if not has_evidence_anchor(correction.evidence_payload):
                raise ValueError("DFT catalyst/material binding corrections require a page, section, table, figure, or quoted-text anchor.")
            if proposed_value in ("", None):
                proposed_value = None
            else:
                try:
                    proposed_uuid = UUID(str(proposed_value))
                except (TypeError, ValueError) as exc:
                    raise ValueError("DFT catalyst/material binding corrections require a valid catalyst_sample_id UUID.") from exc
                catalyst = self.session.get(CatalystSample, proposed_uuid)
                if catalyst is None:
                    raise ValueError("Target catalyst sample was not found.")
                if catalyst.paper_id != correction.paper_id:
                    raise ValueError("Target catalyst sample does not belong to the same paper.")
                proposed_value = proposed_uuid
            setattr(record, attribute, proposed_value)
            if isinstance(correction.evidence_payload, dict):
                merged_payload = dict(record.evidence_payload or {})
                merged_payload["material_binding"] = {
                    "catalyst_sample_id": str(proposed_value) if proposed_value else None,
                    "approved_correction_id": str(correction.id),
                    "approved_by": correction.reviewed_by,
                    "approved_at": correction.reviewed_at.isoformat() if correction.reviewed_at else None,
                    "evidence_anchor": correction.evidence_payload,
                }
                record.evidence_payload = merged_payload
                self._upsert_binding_locator(record, correction.evidence_payload)
            self.session.add(record)
            return
        setattr(record, attribute, proposed_value)
        if isinstance(record, PaperFigure) and attribute in {"caption", "content_summary"}:
            record.content_summary = normalize_figure_content_summary(record.content_summary, record.caption)
        self.session.add(record)

    def _apply_structured_delete(self, correction: PaperCorrection) -> None:
        collection, row_id_text, attribute = self._parse_structured_target_path(correction.target_path)
        if correction.field_name != collection:
            raise ValueError("Correction field_name must match structured target collection")
        if attribute != "delete":
            raise ValueError("Structured deletion target must use format <collection>:<row_id>:delete")
        if collection not in self.STRUCTURED_DELETE_TARGETS:
            raise ValueError(f"Structured deletion is not enabled for {collection}")
        if not has_evidence_anchor(correction.evidence_payload):
            raise ValueError("Structured deletion requires at least one PDF evidence anchor.")
        spec = self.STRUCTURED_TARGETS.get(collection)
        if spec is None:
            raise ValueError("Unsupported structured deletion target")
        record = self.session.get(spec.model, UUID(row_id_text))
        if not record:
            raise ValueError(f"{collection} row not found")
        if getattr(record, "paper_id", None) != correction.paper_id:
            raise ValueError(f"{collection} row does not belong to the target paper")
        snapshot = self._structured_record_snapshot(record, collection)
        self.session.delete(record)
        correction.evidence_payload = {
            **dict(correction.evidence_payload or {}),
            "structured_delete": {
                "collection": collection,
                "target_id": row_id_text,
                "snapshot": snapshot,
            },
        }
        self.session.add(correction)
        self.session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action="delete_structured_object",
                source=correction.reviewed_by or correction.source,
                target_type=collection,
                target_id=row_id_text,
                payload={
                    "source_correction_id": str(correction.id),
                    "target_path": correction.target_path,
                    "snapshot": snapshot,
                    "evidence_anchor": correction.evidence_payload,
                },
            )
        )

    def _retire_direct_delete_pending_figure_corrections(
        self,
        *,
        paper_id: UUID,
        figure_id: UUID,
        reviewer: str,
        keep_correction_id: UUID,
    ) -> list[str]:
        rows = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.id != keep_correction_id)
            .where(PaperCorrection.status.in_(["pending", "requires_resolution"]))
            .where(PaperCorrection.target_path.like(f"figures:{figure_id}:%"))
        ).all()
        retired_ids: list[str] = []
        for row in rows:
            row.status = "rejected"
            row.reviewed_by = reviewer
            row.reviewed_at = datetime.utcnow()
            payload = dict(row.evidence_payload or {}) if isinstance(row.evidence_payload, dict) else {}
            payload["superseded_by_direct_delete"] = {
                "figure_id": str(figure_id),
                "reviewer": reviewer,
            }
            row.evidence_payload = payload
            flag_modified(row, "evidence_payload")
            retired_ids.append(str(row.id))
            self.session.add(
                AuditLog(
                    paper_id=paper_id,
                    action="retire_figure_correction_after_direct_delete",
                    source=reviewer,
                    target_type="paper_correction",
                    target_id=str(row.id),
                    payload={
                        "figure_id": str(figure_id),
                        "target_path": row.target_path,
                    },
                )
            )
        return retired_ids

    def _figure_duplicate_group_size(self, figure: PaperFigure) -> int:
        identity = normalized_figure_identity(figure)
        if not identity:
            return 1
        rows = self.session.scalars(
            select(PaperFigure).where(PaperFigure.paper_id == figure.paper_id)
        ).all()
        count = 0
        for row in rows:
            if normalized_figure_identity(row) == identity:
                count += 1
        return max(1, count)

    def _apply_structured_create(self, correction: PaperCorrection) -> None:
        collection, row_id_text, attribute = self._parse_structured_target_path(correction.target_path)
        if collection != correction.field_name or row_id_text != "new" or attribute != "create":
            raise ValueError("Structured creation target must use <collection>:new:create")
        if collection not in self.STRUCTURED_CREATE_TARGETS:
            raise ValueError(f"Structured creation is not enabled for {collection}")
        if not has_evidence_anchor(correction.evidence_payload):
            raise ValueError(
                "Non-DFT structured creation requires at least one PDF evidence anchor: "
                "page, section, quoted_text, table, or figure."
            )
        if collection == "sections" and not self._has_strong_section_anchor(correction.evidence_payload):
            raise ValueError(
                "Section creation requires a checkable evidence anchor beyond a bare page number: "
                "section, section_title, quoted_text, evidence_text, figure, table, or bbox."
            )
        if not isinstance(correction.proposed_value, dict):
            raise ValueError("Structured creation proposed_value must be an object")

        spec = self.STRUCTURED_TARGETS[collection]
        proposed = dict(correction.proposed_value)
        if spec.model is PaperFigure and proposed.get("image_path"):
            proposed["image_path"] = self._validated_relative_artifact_path(proposed["image_path"])
        cleaned = {
            field: proposed.get(field)
            for field in spec.allowed_fields
            if field in proposed and proposed.get(field) not in (None, "")
        }
        if spec.model is PaperFigure:
            cleaned["content_summary"] = normalize_figure_content_summary(
                cleaned.get("content_summary"),
                cleaned.get("caption"),
            )
            if "key_elements" in cleaned:
                cleaned["key_elements"], _detail = normalize_figure_key_elements(cleaned.get("key_elements"))
            if cleaned.get("content_summary") is None:
                cleaned.pop("content_summary", None)
            if cleaned.get("key_elements") is None:
                cleaned.pop("key_elements", None)
        if not cleaned:
            raise ValueError("Structured creation proposed_value did not include any supported fields")
        self._validate_structured_create_payload(collection, cleaned)

        record = self._find_existing_structured_record(correction.paper_id, collection, proposed)
        action = "update_existing_structured_object"
        if record is None:
            record = spec.model(paper_id=correction.paper_id, **cleaned)
            action = "create_structured_object"
        else:
            for key, value in cleaned.items():
                setattr(record, key, value)

        self.session.add(record)
        self.session.flush()
        correction.evidence_payload = {
            **dict(correction.evidence_payload or {}),
            "structured_create": {
                "collection": collection,
                "target_id": str(record.id),
                "action": action,
            },
        }
        self.session.add(correction)
        self.session.add(
            AuditLog(
                paper_id=correction.paper_id,
                action=action,
                source=correction.reviewed_by or correction.source,
                target_type=collection,
                target_id=str(record.id),
                payload={
                    "source_correction_id": str(correction.id),
                    "target_path": correction.target_path,
                    "fields": sorted(cleaned),
                    "evidence_anchor": correction.evidence_payload,
                },
            )
        )

    @staticmethod
    def _has_strong_section_anchor(payload: Any) -> bool:
        from app.utils.evidence_anchors import iter_anchor_payloads

        strong_keys = {
            "section",
            "section_title",
            "quoted_text",
            "evidence_text",
            "figure",
            "figure_id",
            "table",
            "table_id",
            "bbox",
        }
        for candidate in iter_anchor_payloads(payload):
            for key in strong_keys:
                value = candidate.get(key)
                if value is not None and str(value).strip():
                    return True
        return False

    def _validate_structured_create_payload(self, collection: str, cleaned: dict[str, Any]) -> None:
        if collection == "sections" and not str(cleaned.get("text") or "").strip():
            raise ValueError("Section creation requires non-empty text")
        if collection == "figures" and not (
            str(cleaned.get("caption") or "").strip()
            or str(cleaned.get("figure_label") or "").strip()
            or str(cleaned.get("image_path") or "").strip()
        ):
            raise ValueError("Figure creation requires caption, figure_label, or image_path")
        if collection == "tables" and not (
            str(cleaned.get("caption") or "").strip()
            or str(cleaned.get("markdown_content") or "").strip()
        ):
            raise ValueError("Table creation requires caption or markdown_content")
        if collection == "writing_cards" and not any(str(value).strip() for value in cleaned.values()):
            raise ValueError("Writing card creation requires at least one non-empty field")
        if collection == "mechanism_claims" and not str(cleaned.get("claim_text") or "").strip():
            raise ValueError("Mechanism claim creation requires non-empty claim_text")
        if collection == "electrochemical_performance" and not any(
            str(cleaned.get(field) or "").strip()
            for field in (
                "sulfur_loading_mg_cm2",
                "sulfur_content_wt_percent",
                "electrolyte_sulfur_ratio",
                "capacity_value",
                "cycle_number",
                "rate",
                "decay_per_cycle",
                "evidence_text",
            )
        ):
            raise ValueError("Electrochemical performance creation requires at least one performance metric or evidence_text")
        if collection == "catalyst_samples" and not any(
            str(cleaned.get(field) or "").strip()
            for field in (
                "name",
                "catalyst_type",
                "metal_centers",
                "coordination",
                "support",
                "synthesis_method",
                "evidence_strength",
            )
        ):
            raise ValueError("Catalyst sample creation requires at least one material identity field")

    def _find_existing_structured_record(self, paper_id: UUID, collection: str, proposed: dict[str, Any]) -> Any | None:
        spec = self.STRUCTURED_TARGETS[collection]
        rows = self.session.scalars(select(spec.model).where(spec.model.paper_id == paper_id)).all()
        normalized_label = self._normalized_text(proposed.get("figure_label"))
        normalized_caption = self._normalized_text(proposed.get("caption"))
        normalized_section_title = self._normalized_text(proposed.get("section_title"))
        normalized_claim_text = self._normalized_text(proposed.get("claim_text"))
        normalized_evidence_text = self._normalized_text(proposed.get("evidence_text"))
        page = proposed.get("page")
        page_start = proposed.get("page_start")
        if collection == "figures":
            for row in rows:
                if normalized_label and self._normalized_text(getattr(row, "figure_label", None)) == normalized_label:
                    return row
            for row in rows:
                if normalized_caption and self._normalized_text(getattr(row, "caption", None)) == normalized_caption:
                    return row
        if collection == "tables":
            for row in rows:
                if normalized_caption and self._normalized_text(getattr(row, "caption", None)) == normalized_caption:
                    return row
        if collection == "sections":
            for row in rows:
                if (
                    normalized_section_title
                    and self._normalized_text(getattr(row, "section_title", None)) == normalized_section_title
                    and (page_start in (None, "", getattr(row, "page_start", None)) or getattr(row, "page_start", None) in (None, ""))
                ):
                    return row
        if collection == "writing_cards" and rows:
            return rows[0]
        if collection == "mechanism_claims":
            for row in rows:
                if normalized_claim_text and self._normalized_text(getattr(row, "claim_text", None)) == normalized_claim_text:
                    return row
        if collection == "electrochemical_performance":
            proposed_signature = (
                proposed.get("capacity_value"),
                proposed.get("cycle_number"),
                self._normalized_text(proposed.get("rate")),
                normalized_evidence_text,
            )
            for row in rows:
                row_signature = (
                    getattr(row, "capacity_value", None),
                    getattr(row, "cycle_number", None),
                    self._normalized_text(getattr(row, "rate", None)),
                    self._normalized_text(getattr(row, "evidence_text", None)),
                )
                if proposed_signature == row_signature and any(item not in (None, "", 0, "0") for item in proposed_signature):
                    return row
        return None

    @staticmethod
    def _normalized_text(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _validated_relative_artifact_path(value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).replace("\\", "/").strip()
        if not text:
            return None
        if text.startswith("/") or ":" in text or ".." in text.split("/"):
            raise ValueError("Artifact paths must be relative storage paths")
        return text

    def _upsert_binding_locator(self, row: DFTResult, evidence_payload: dict[str, Any]) -> None:
        anchor = first_evidence_anchor(evidence_payload)
        if not anchor or anchor.get("page") in (None, ""):
            return
        try:
            page = int(anchor["page"])
        except (TypeError, ValueError):
            return
        locator = self.session.scalar(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == row.paper_id,
                EvidenceLocator.target_type == "dft_results",
                EvidenceLocator.target_id == str(row.id),
                EvidenceLocator.field_name.in_([None, "catalyst_sample_id"]),
            )
        )
        if locator is None:
            locator = EvidenceLocator(
                paper_id=row.paper_id,
                source_type="pdf",
                target_type="dft_results",
                target_id=str(row.id),
                field_name="catalyst_sample_id",
                evidence_text=str(anchor.get("quoted_text") or row.evidence_text or "PDF evidence"),
                locator_status="exact_page",
                locator_confidence=1.0,
                parser_source="external_ai_review",
            )
        locator.page = page
        locator.section = anchor.get("section") or anchor.get("section_title") or row.source_section
        locator.evidence_text = str(anchor.get("quoted_text") or locator.evidence_text or row.evidence_text or "PDF evidence")
        locator.locator_status = "exact_bbox" if anchor.get("bbox") else "exact_page"
        locator.locator_confidence = 1.0
        locator.parser_source = "external_ai_review"
        locator.warning_reason = None
        self.session.add(locator)

    def _resolve_current_value(self, correction: PaperCorrection) -> Any:
        if correction.operation == "delete" and correction.field_name in self.STRUCTURED_DELETE_TARGETS:
            record, collection = self._resolve_structured_delete_target(correction)
            return self._structured_record_snapshot(record, collection)
        if correction.operation == "create" and correction.field_name == "catalyst_samples":
            return None
        if correction.operation == "create" and correction.field_name in self.STRUCTURED_CREATE_TARGETS:
            return None
        if self._is_top_level_paper_correction(correction):
            paper = self.session.get(Paper, correction.paper_id)
            if not paper:
                raise ValueError("Paper not found")
            return getattr(paper, correction.field_name)

        if correction.field_name in self.STRUCTURED_TARGETS:
            record, _, attribute = self._resolve_structured_target(correction)
            return getattr(record, attribute)

        raise ValueError("Correction target cannot be resolved")

    def _resolve_structured_target(self, correction: PaperCorrection) -> tuple[Any, StructuredTargetSpec, str]:
        collection, row_id_text, attribute = self._parse_structured_target_path(correction.target_path)
        spec = self.STRUCTURED_TARGETS.get(collection)
        if spec is None:
            raise ValueError("Unsupported structured correction target")
        if correction.field_name != collection:
            raise ValueError("Correction field_name must match structured target collection")
        if attribute not in spec.allowed_fields:
            raise ValueError(f"Structured correction field is not review-applicable yet: {attribute}")

        record = self.session.get(spec.model, UUID(row_id_text))
        if not record:
            raise ValueError(f"{collection} row not found")
        if getattr(record, "paper_id", None) != correction.paper_id:
            raise ValueError(f"{collection} row does not belong to the target paper")
        return record, spec, attribute

    def _resolve_structured_delete_target(self, correction: PaperCorrection) -> tuple[Any, str]:
        collection, row_id_text, attribute = self._parse_structured_target_path(correction.target_path)
        if correction.field_name != collection:
            raise ValueError("Correction field_name must match structured target collection")
        if attribute != "delete":
            raise ValueError("Structured deletion target must use format <collection>:<row_id>:delete")
        if collection not in self.STRUCTURED_DELETE_TARGETS:
            raise ValueError(f"Structured deletion is not enabled for {collection}")
        spec = self.STRUCTURED_TARGETS.get(collection)
        if spec is None:
            raise ValueError("Unsupported structured deletion target")
        record = self.session.get(spec.model, UUID(row_id_text))
        if not record:
            raise ValueError(f"{collection} row not found")
        if getattr(record, "paper_id", None) != correction.paper_id:
            raise ValueError(f"{collection} row does not belong to the target paper")
        return record, collection

    @staticmethod
    def _parse_structured_target_path(target_path: str) -> tuple[str, str, str]:
        parts = [part.strip() for part in target_path.split(":")]
        if len(parts) != 3 or not all(parts):
            raise ValueError("Structured correction target path must use format <collection>:<row_id>:<field>")
        return parts[0], parts[1], parts[2]

    @classmethod
    def _is_top_level_paper_correction(cls, correction: PaperCorrection) -> bool:
        if correction.field_name not in cls.ALLOWED_PAPER_FIELDS:
            return False
        target_path = str(correction.target_path or "").strip()
        if not target_path:
            return True
        allowed_paths = {
            correction.field_name,
            f"paper.{correction.field_name}",
            f"paper:{correction.field_name}",
            f"paper:{correction.paper_id}:{correction.field_name}",
        }
        return target_path in allowed_paths

    @staticmethod
    def _structured_record_snapshot(record: Any, collection: str) -> dict[str, Any]:
        if collection == "figures":
            return {
                "id": str(record.id),
                "figure_label": getattr(record, "figure_label", None),
                "page": getattr(record, "page", None),
                "caption": getattr(record, "caption", None),
                "figure_role": getattr(record, "figure_role", None),
                "crop_status": getattr(record, "crop_status", None),
                "image_path": getattr(record, "image_path", None),
            }
        return {"id": str(getattr(record, "id", ""))}

    def _get_correction(self, correction_id: UUID) -> PaperCorrection:
        correction = self.session.get(PaperCorrection, correction_id)
        if not correction:
            raise ValueError("Correction not found")
        return correction
