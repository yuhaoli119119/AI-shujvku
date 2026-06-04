from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample, DFTResult, DFTSetting, EvidenceLocator, Paper
from app.utils.review_safety import is_export_eligible_extraction, summarize_gate_results


DFT_TARGET_TYPES = ("dft_results", "dft_result", "DFTResult")
ENERGY_UNITS = {
    "ev",
    "mev",
    "kj/mol",
    "kj mol-1",
    "kjmol-1",
    "kcal/mol",
    "kcal mol-1",
    "kcalmol-1",
    "j/mol",
    "hartree",
    "ha",
}
POTENTIAL_UNITS = {"v", "mv", "ev"}


class DFTReviewQueueService:
    """Build a Codex-ready queue for reviewing DFT result candidates."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_queue(
        self,
        *,
        property_type: str | None = None,
        adsorbate: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        paper_id: UUID | None = None,
        reason: str | None = None,
        status: str = "needs_review",
        limit: int = 100,
        schema_version: str = "dft_review_queue_v1",
    ) -> dict[str, Any]:
        rows = self.session.execute(
            self._statement(
                property_type=property_type,
                adsorbate=adsorbate,
                year_min=year_min,
                year_max=year_max,
                paper_id=paper_id,
            )
        ).all()
        gate_results = []
        queue_rows = []
        paper_ids = set()
        paper_meta_by_id: dict[str, dict[str, Any]] = {}
        exportable_by_paper: dict[str, int] = defaultdict(int)
        blocked_by_paper: dict[str, int] = defaultdict(int)
        review_status_counts: Counter[str] = Counter()

        for row, paper in rows:
            gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
            gate_results.append(gate)
            for review_status in self._review_statuses(gate.review_status):
                review_status_counts[review_status] += 1
            pid = str(paper.id)
            paper_ids.add(paper.id)
            paper_meta_by_id[pid] = {
                "title": paper.title,
                "doi": paper.doi,
                "library_detail_url": f"../literature_library/index.html?paper_id={pid}&tab=dft",
                "review_workbench_url": f"../external_analysis_workbench/index.html?paper_id={pid}",
            }
            if gate.eligible:
                exportable_by_paper[pid] += 1
            else:
                blocked_by_paper[pid] += 1
            if reason and reason not in gate.reasons:
                continue
            if not self._status_matches(status, gate):
                continue
            queue_rows.append(self._row_payload(row, paper, gate))

        queue_rows.sort(
            key=lambda item: (
                bool(item.get("sanity_flags")),
                not bool(item.get("can_mark_verified")),
                -(item.get("year") or 0),
                str(item.get("title") or ""),
            )
        )

        catalyst_counts: Counter[str] = Counter()
        setting_counts: Counter[str] = Counter()
        if paper_ids:
            for pid in self.session.scalars(
                select(CatalystSample.paper_id).where(CatalystSample.paper_id.in_(paper_ids))
            ).all():
                catalyst_counts[str(pid)] += 1
            for pid in self.session.scalars(select(DFTSetting.paper_id).where(DFTSetting.paper_id.in_(paper_ids))).all():
                setting_counts[str(pid)] += 1

        paper_completeness = []
        for pid in sorted({str(item) for item in paper_ids}):
            meta = paper_meta_by_id.get(pid, {})
            paper_completeness.append(
                {
                    "paper_id": pid,
                    "title": meta.get("title"),
                    "doi": meta.get("doi"),
                    "library_detail_url": meta.get("library_detail_url"),
                    "review_workbench_url": meta.get("review_workbench_url"),
                    "exportable_dft_results": exportable_by_paper.get(pid, 0),
                    "blocked_dft_results": blocked_by_paper.get(pid, 0),
                    "catalyst_samples": catalyst_counts.get(pid, 0),
                    "dft_settings": setting_counts.get(pid, 0),
                    "hints": [
                        hint
                        for hint, present in (
                            ("missing_catalyst_sample", catalyst_counts.get(pid, 0) == 0),
                            ("missing_dft_setting", setting_counts.get(pid, 0) == 0),
                            ("has_blocked_dft_results", blocked_by_paper.get(pid, 0) > 0),
                        )
                        if present
                    ],
                }
            )

        gate_summary = summarize_gate_results(gate_results)
        return {
            "metadata": {
                "schema_version": schema_version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "filters": {
                    "property_type": property_type,
                    "adsorbate": adsorbate,
                    "year_min": year_min,
                    "year_max": year_max,
                    "paper_id": str(paper_id) if paper_id else None,
                    "reason": reason,
                    "status": status,
                },
                "safety_gate": "safe_verified_with_required_evidence",
                "eligible_count": gate_summary["eligible"],
                "blocked_count": gate_summary["blocked"],
                "blocked_reasons": gate_summary["blocked_reasons"],
                "review_status_counts": dict(sorted(review_status_counts.items())),
                "total_candidates": gate_summary["total_candidates"],
                "returned": min(len(queue_rows), limit),
            },
            "rows": queue_rows[:limit],
            "paper_completeness": paper_completeness[:limit],
        }

    def _statement(
        self,
        *,
        property_type: str | None,
        adsorbate: str | None,
        year_min: int | None,
        year_max: int | None,
        paper_id: UUID | None,
    ):
        stmt = select(DFTResult, Paper).join(Paper, DFTResult.paper_id == Paper.id).order_by(
            Paper.year.desc().nulls_last(),
            Paper.title,
            DFTResult.property_type,
        )
        if property_type:
            stmt = stmt.where(DFTResult.property_type.ilike(f"%{property_type}%"))
        if adsorbate:
            stmt = stmt.where(DFTResult.adsorbate.ilike(f"%{adsorbate}%"))
        if year_min:
            stmt = stmt.where(Paper.year >= year_min)
        if year_max:
            stmt = stmt.where(Paper.year <= year_max)
        if paper_id:
            stmt = stmt.where(Paper.id == paper_id)
        return stmt

    def _row_payload(self, row: DFTResult, paper: Paper, gate: Any) -> dict[str, Any]:
        paper_id = str(paper.id)
        result_id = str(row.id)
        reasons = list(gate.reasons)
        locators = self._locator_payloads(row)
        sanity_flags = self._sanity_flags(row)
        return {
            "record_id": result_id,
            "dft_result_id": result_id,
            "paper_id": paper_id,
            "title": paper.title,
            "doi": paper.doi,
            "journal": paper.journal,
            "year": paper.year,
            "property_type": row.property_type,
            "adsorbate": row.adsorbate,
            "value": row.value,
            "unit": row.unit,
            "reaction_step": row.reaction_step,
            "source_section": row.source_section,
            "source_figure": row.source_figure,
            "evidence_text": row.evidence_text,
            "evidence_preview": self._shorten(row.evidence_text),
            "confidence": row.confidence,
            "review_status": gate.review_status,
            "decision_status": self._decision_status(gate.review_status),
            "review_gate_status": gate.review_gate_status,
            "provenance_level": gate.provenance_level,
            "locator_status": gate.locator_status,
            "blocked_reasons": reasons,
            "is_exportable": gate.eligible,
            "sanity_flags": sanity_flags,
            "can_mark_verified": set(reasons) == {"missing_review"} and not sanity_flags,
            "recommended_action": self._recommended_action(reasons, gate, sanity_flags),
            "evidence_locators": locators,
            "evidence_check": {
                "has_evidence_text": bool((row.evidence_text or "").strip()),
                "locator_count": len(locators),
                "has_exact_page_locator": gate.locator_status == "exact_page",
            },
            "paper_detail_url": f"../paper_detail/index.html?paper_id={paper_id}",
            "library_detail_url": f"../literature_library/index.html?paper_id={paper_id}&tab=dft",
            "codex_item_url": f"/api/papers/{paper_id}/codex-item/dft_result/{result_id}",
            "verify_url": f"/api/papers/{paper_id}/dft-results/{result_id}/verify",
            "reject_url": f"/api/papers/{paper_id}/dft-results/{result_id}/reject",
            "correction_url": f"/api/papers/{paper_id}/dft-results/{result_id}/corrections",
            "review_workbench_url": (
                f"../external_analysis_workbench/index.html?paper_id={paper_id}"
                if {"missing_review", "unsafe_review"} & set(reasons)
                else f"../literature_library/index.html?paper_id={paper_id}&tab=review"
            ),
        }

    def _locator_payloads(self, row: DFTResult) -> list[dict[str, Any]]:
        locators = self.session.scalars(
            select(EvidenceLocator)
            .where(
                EvidenceLocator.paper_id == row.paper_id,
                EvidenceLocator.target_id == str(row.id),
                EvidenceLocator.target_type.in_(DFT_TARGET_TYPES),
            )
            .order_by(EvidenceLocator.page.asc().nulls_last(), EvidenceLocator.created_at.asc())
            .limit(5)
        ).all()
        return [
            {
                "id": str(locator.id),
                "page": locator.page,
                "section": locator.section,
                "field_name": locator.field_name,
                "locator_status": locator.locator_status,
                "locator_confidence": locator.locator_confidence,
                "parser_source": locator.parser_source,
                "evidence_text": locator.evidence_text,
                "evidence_preview": self._shorten(locator.evidence_text),
                "bbox": locator.bbox,
                "warning_reason": locator.warning_reason,
            }
            for locator in locators
        ]

    @staticmethod
    def _status_matches(status: str | None, gate: Any) -> bool:
        normalized = (status or "needs_review").strip().lower()
        review_statuses = DFTReviewQueueService._review_statuses(gate.review_status)
        if normalized in {"all", "any", ""}:
            return True
        if normalized in {"needs_review", "blocked"}:
            if normalized == "blocked":
                return not gate.eligible
            return not gate.eligible and "rejected" not in review_statuses
        if normalized in {"rejected", "reject"}:
            return "rejected" in review_statuses
        if normalized in {"exportable", "eligible", "verified"}:
            return gate.eligible
        return normalized in set(gate.reasons)

    @staticmethod
    def _recommended_action(reasons: list[str], gate: Any, sanity_flags: list[str] | None = None) -> str:
        if "rejected" in DFTReviewQueueService._review_statuses(gate.review_status):
            return "rejected_candidate"
        if sanity_flags:
            return "inspect_suspicious_candidate"
        if gate.eligible:
            return "ready_for_ml_export"
        reason_set = set(reasons)
        if "missing_evidence_text" in reason_set:
            return "add_evidence_text"
        if "missing_evidence" in reason_set:
            return "repair_evidence_reference"
        if "unsafe_locator" in reason_set:
            return "repair_pdf_locator"
        if reason_set == {"missing_review"}:
            return "verify_against_pdf"
        if "unsafe_review" in reason_set:
            return "resolve_review_status"
        return "review_candidate"

    @staticmethod
    def _shorten(value: str | None, limit: int = 360) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "..."

    @staticmethod
    def _review_statuses(value: str | None) -> set[str]:
        statuses = {
            item.strip().lower()
            for item in str(value or "").split(",")
            if item.strip()
        }
        return statuses or {"missing"}

    @staticmethod
    def _decision_status(value: str | None) -> str:
        statuses = DFTReviewQueueService._review_statuses(value)
        if "rejected" in statuses:
            return "rejected"
        if "verified" in statuses:
            return "verified"
        if statuses == {"missing"}:
            return "unreviewed"
        return "needs_check"

    @staticmethod
    def _sanity_flags(row: DFTResult) -> list[str]:
        flags: list[str] = []
        property_type = str(row.property_type or "").strip().lower()
        unit = str(row.unit or "").strip().lower().replace(" ", "")
        adsorbate = str(row.adsorbate or "").strip()
        value = row.value

        if value is None:
            flags.append("missing_numeric_value")
        if adsorbate and re.fullmatch(r"\[?\d+(?:[-,]\d+)*\]?", adsorbate):
            flags.append("adsorbate_looks_like_reference")
        if adsorbate and len(adsorbate) > 40:
            flags.append("adsorbate_too_long")

        if property_type:
            expects_potential = "potential" in property_type or property_type in {"ul", "u_l"}
            expects_energy = any(
                token in property_type
                for token in (
                    "energy",
                    "barrier",
                    "formation",
                    "adsorption",
                    "binding",
                    "migration",
                    "gibbs",
                )
            )
            if expects_potential and unit and unit not in POTENTIAL_UNITS:
                flags.append(f"unexpected_potential_unit:{unit}")
            if expects_energy and unit and unit not in ENERGY_UNITS:
                flags.append(f"unexpected_energy_unit:{unit}")
            if expects_energy and unit == "ev" and value is not None and abs(float(value)) > 50:
                flags.append("energy_value_outside_typical_ev_range")
            if expects_potential and unit in {"v", "ev"} and value is not None and abs(float(value)) > 20:
                flags.append("potential_value_outside_typical_range")
        elif unit and unit not in ENERGY_UNITS | POTENTIAL_UNITS:
            flags.append(f"unexpected_unit:{unit}")

        return flags
