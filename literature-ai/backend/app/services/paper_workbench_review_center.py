from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID
import re

from app.db.models import DFTResult, Paper
from app.services.dft_rescan_policy import (
    build_dft_dedupe_signature,
    normalize_numeric_value,
    normalize_source_document_type,
    normalize_unit,
)
from app.services.supplementary_dft_lifecycle_service import (
    CLOSED_SUPPORT_DFT_LIFECYCLE_STATUSES,
    OPEN_SUPPORT_DFT_LIFECYCLE_STATUSES,
)


FINALIZED_DFT_CANDIDATE_STATUSES = {
    "ML_Ready",
    "Rejected",
    "human_reviewed_needs_evidence",
    "Gemini_Verified",
    "Human_Confirmed",
    "Citation_Ready",
    "verified",
    "human_verified",
}


class PaperWorkbenchReviewCenterMixin:
    """Helper methods used by the paper review-center summary."""

    @staticmethod
    def _is_active_dft_candidate(status: Any) -> bool:
        normalized = str(status or "system_candidate").strip()
        return normalized not in FINALIZED_DFT_CANDIDATE_STATUSES

    @classmethod
    def _count_active_dft_candidates(cls, rows: list[DFTResult]) -> int:
        if not rows:
            return 0
        finalized_signatures = {
            cls._dft_dedupe_signature(row)
            for row in rows
            if not cls._is_active_dft_candidate(row.candidate_status)
        }
        finalized_shadow_keys = {
            cls._dft_shadow_key(row)
            for row in rows
            if not cls._is_active_dft_candidate(row.candidate_status)
        }
        count = 0
        for row in rows:
            if not cls._is_active_dft_candidate(row.candidate_status):
                continue
            if cls._dft_dedupe_signature(row) in finalized_signatures:
                continue
            shadow_key = cls._dft_shadow_key(row)
            if shadow_key is not None and shadow_key in finalized_shadow_keys:
                continue
            count += 1
        return count

    @classmethod
    def _active_count_from_status_counts(cls, candidate_status_counts: dict[str, int]) -> int:
        return sum(
            int(count or 0)
            for candidate_status, count in candidate_status_counts.items()
            if cls._is_active_dft_candidate(candidate_status)
        )

    @staticmethod
    def _exportable_count_from_status_counts(candidate_status_counts: dict[str, int]) -> int:
        exportable_statuses = {
            "ml_ready",
            "human_confirmed",
            "citation_ready",
            "verified",
            "human_verified",
        }
        return sum(
            int(count or 0)
            for candidate_status, count in candidate_status_counts.items()
            if str(candidate_status or "").strip().lower() in exportable_statuses
        )

    @staticmethod
    def _is_dft_object_review_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        target_type = str(payload.get("target_type") or "").strip().lower()
        return target_type in {"dft_result", "dft_results"}

    @classmethod
    def _supplementary_group_payload(
        cls,
        paper_id: UUID,
        *,
        group_main_by_paper: dict[UUID, UUID],
        support_ids_by_main: dict[UUID, set[UUID]],
        related_paper_meta: dict[UUID, dict[str, Any]],
        dft_status_counts_by_paper: dict[UUID, dict[str, int]],
        dft_lifecycle_counts_by_paper: dict[UUID, dict[str, int]],
    ) -> dict[str, Any] | None:
        main_id = group_main_by_paper.get(paper_id)
        if main_id is None:
            return None
        support_ids = sorted(support_ids_by_main.get(main_id, set()), key=lambda item: str(item))
        member_ids = [main_id] + support_ids
        group_status_counts: Counter[str] = Counter()
        support_status_counts: Counter[str] = Counter()
        support_lifecycle_counts: Counter[str] = Counter()
        for member_id in member_ids:
            member_counts = dft_status_counts_by_paper.get(member_id, {})
            group_status_counts.update(member_counts)
            if member_id != main_id:
                support_status_counts.update(member_counts)
                support_lifecycle_counts.update(dft_lifecycle_counts_by_paper.get(member_id, {}))
        main_counts = dft_status_counts_by_paper.get(main_id, {})
        support_active_count = cls._active_count_from_status_counts(dict(support_status_counts))
        support_lifecycle_open_count = sum(
            int(support_lifecycle_counts.get(status, 0) or 0)
            for status in OPEN_SUPPORT_DFT_LIFECYCLE_STATUSES
        )
        support_lifecycle_closed_count = sum(
            int(support_lifecycle_counts.get(status, 0) or 0)
            for status in CLOSED_SUPPORT_DFT_LIFECYCLE_STATUSES
        )
        support_lifecycle_state = (
            "evidence_only_pending_writeback" if support_lifecycle_open_count else "clear"
        )
        support_lifecycle_label = (
            "SI 证据待闭环" if support_lifecycle_open_count else "SI 已闭环"
        )
        return {
            "role": "main" if paper_id == main_id else "supplementary",
            "main_paper_id": str(main_id),
            "main_paper_code": related_paper_meta.get(main_id, {}).get("paper_code"),
            "support_papers": [
                cls._support_paper_lifecycle_payload(
                    support_id=support_id,
                    main_id=main_id,
                    related_paper_meta=related_paper_meta,
                    status_counts=dft_status_counts_by_paper.get(support_id, {}),
                    lifecycle_counts=dft_lifecycle_counts_by_paper.get(support_id, {}),
                )
                for support_id in support_ids
            ],
            "member_paper_ids": [str(member_id) for member_id in member_ids],
            "dft_candidate_count": sum(int(count or 0) for count in group_status_counts.values()),
            "active_dft_candidate_count": cls._active_count_from_status_counts(dict(group_status_counts)),
            "exportable_dft_count": cls._exportable_count_from_status_counts(dict(group_status_counts)),
            "main_dft_candidate_count": sum(int(count or 0) for count in main_counts.values()),
            "main_active_dft_candidate_count": cls._active_count_from_status_counts(main_counts),
            "main_exportable_dft_count": cls._exportable_count_from_status_counts(main_counts),
            "support_dft_candidate_count": sum(int(count or 0) for count in support_status_counts.values()),
            "support_active_dft_candidate_count": support_active_count,
            "support_dft_lifecycle_state": support_lifecycle_state,
            "support_dft_lifecycle_label": support_lifecycle_label,
            "support_dft_lifecycle_counts": dict(sorted(support_lifecycle_counts.items())),
            "support_dft_lifecycle_open_count": support_lifecycle_open_count,
            "support_dft_lifecycle_closed_count": support_lifecycle_closed_count,
            "support_canonical_writeback_paper_id": str(main_id),
            "candidate_status_counts": dict(sorted(group_status_counts.items())),
        }

    @classmethod
    def _support_paper_lifecycle_payload(
        cls,
        *,
        support_id: UUID,
        main_id: UUID,
        related_paper_meta: dict[UUID, dict[str, Any]],
        status_counts: dict[str, int],
        lifecycle_counts: dict[str, int],
    ) -> dict[str, Any]:
        open_count = sum(
            int(lifecycle_counts.get(status, 0) or 0)
            for status in OPEN_SUPPORT_DFT_LIFECYCLE_STATUSES
        )
        closed_count = sum(
            int(lifecycle_counts.get(status, 0) or 0)
            for status in CLOSED_SUPPORT_DFT_LIFECYCLE_STATUSES
        )
        return {
            "paper_id": str(support_id),
            "paper_code": related_paper_meta.get(support_id, {}).get("paper_code"),
            "title": related_paper_meta.get(support_id, {}).get("title"),
            "active_dft_candidate_count": cls._active_count_from_status_counts(status_counts),
            "dft_candidate_count": sum(int(count or 0) for count in status_counts.values()),
            "canonical_writeback_paper_id": str(main_id),
            "dft_lifecycle_state": "evidence_only_pending_writeback" if open_count else "clear",
            "dft_lifecycle_counts": dict(sorted(lifecycle_counts.items())),
            "dft_lifecycle_open_count": open_count,
            "dft_lifecycle_closed_count": closed_count,
        }

    @staticmethod
    def _dft_dedupe_signature(row: DFTResult) -> str:
        payload = dict(row.evidence_payload) if isinstance(row.evidence_payload, dict) else {}
        payload.update(
            {
                "paper_id": row.paper_id,
                "adsorbate": row.adsorbate,
                "property_type": row.property_type,
                "value": row.value,
                "unit": row.unit,
                "reaction_step": row.reaction_step,
            }
        )
        return str(payload.get("dedupe_signature") or build_dft_dedupe_signature(payload))

    @staticmethod
    def _dft_shadow_key(row: DFTResult) -> tuple[str, str, str, str, int] | None:
        payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        anchor = payload.get("material_binding", {}).get("evidence_anchor")
        if not isinstance(anchor, dict):
            anchor = {}
        page = payload.get("page")
        if page in (None, ""):
            page = anchor.get("page")
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            return None
        source_type = normalize_source_document_type(
            payload.get("source_document_type") or anchor.get("source_document_type")
        )
        source_bucket = "supporting_reference" if source_type == "supporting_reference" else "paper_owned"
        return (
            source_bucket,
            " ".join(str(row.property_type or "").strip().lower().split()),
            normalize_numeric_value(row.value),
            normalize_unit(row.unit),
            page_number,
        )

    @staticmethod
    def _sort_review_center_rows(rows: list[dict[str, Any]], *, sort_by: str) -> list[dict[str, Any]]:
        normalized_sort = str(sort_by or "recent").strip().lower()

        def serial_value(row: dict[str, Any]) -> int:
            raw_code = str(row.get("paper_code") or "").strip().upper()
            match = re.match(r"^[A-Z](\d+)$", raw_code)
            if match:
                return int(match.group(1))
            raw_serial = row.get("serial_number")
            if raw_serial is not None:
                try:
                    return int(raw_serial)
                except (TypeError, ValueError):
                    pass
            return 10**12

        if normalized_sort == "paper_code_asc":
            return sorted(
                rows,
                key=lambda row: (
                    serial_value(row),
                    str(row.get("paper_code") or ""),
                    str(row.get("paper_id") or ""),
                ),
            )
        if normalized_sort == "year_desc":
            return sorted(
                rows,
                key=lambda row: (
                    -(int(row["year"]) if row.get("year") is not None else -1),
                    str(row.get("paper_id") or ""),
                ),
            )
        if normalized_sort == "conflicts_desc":
            return sorted(
                rows,
                key=lambda row: (
                    -int(row.get("review_conflict_total_count") or 0),
                    -int(row.get("review_conflict_count") or 0),
                    -int(row.get("locator_issue_count") or 0),
                    -int(row.get("figure_issue_count") or 0),
                    str(row.get("paper_id") or ""),
                ),
            )
        if normalized_sort == "suspected_missing_desc":
            return sorted(
                rows,
                key=lambda row: (
                    -int(row.get("suspected_missing_dft_count") or 0),
                    0
                    if str(row.get("workflow_status") or "") == "Suspected_Missing"
                    else (1 if str(row.get("workflow_status") or "") == "Unparsed" else 2),
                    str(row.get("paper_id") or ""),
                ),
            )
        return sorted(
            rows,
            key=lambda row: (
                str(row.get("created_at") or ""),
                str(row.get("paper_id") or ""),
            ),
            reverse=True,
        )

    @staticmethod
    def _manual_review_progress(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        source = data if isinstance(data, dict) else {}
        progress = source.get("manual_review_progress") if isinstance(source.get("manual_review_progress"), dict) else {}

        def normalize_entry(module: str) -> dict[str, Any]:
            raw = progress.get(module)
            if isinstance(raw, dict):
                return {
                    "completed": bool(raw.get("completed")),
                    "updated_at": raw.get("updated_at"),
                    "updated_by": raw.get("updated_by"),
                }
            return {
                "completed": bool(raw),
                "updated_at": None,
                "updated_by": None,
            }

        return {
            "content": normalize_entry("content"),
            "figures": normalize_entry("figures"),
            "dft": normalize_entry("dft"),
        }

    @staticmethod
    def _lightweight_dft_audit(
        paper: Paper,
        *,
        parsed_count: int,
        exportable_count: int,
        blocked_count: int,
        candidate_status_counts: dict[str, int],
    ) -> dict[str, Any]:
        rejected_count = sum(
            int(count or 0)
            for candidate_status, count in candidate_status_counts.items()
            if str(candidate_status or "").strip().lower() == "rejected"
        )
        exportable_statuses = {
            "ml_ready",
            "human_confirmed",
            "citation_ready",
            "verified",
            "human_verified",
        }
        finalized_statuses = {status.lower() for status in FINALIZED_DFT_CANDIDATE_STATUSES}
        derived_exportable_count = sum(
            int(count or 0)
            for candidate_status, count in candidate_status_counts.items()
            if str(candidate_status or "").strip().lower() in exportable_statuses
        )
        derived_active_count = sum(
            int(count or 0)
            for candidate_status, count in candidate_status_counts.items()
            if str(candidate_status or "").strip().lower() not in finalized_statuses
        )
        effective_exportable_count = int(exportable_count or 0) or derived_exportable_count
        effective_blocked_count = int(blocked_count or 0) or derived_active_count
        all_candidates_rejected = parsed_count > 0 and rejected_count == parsed_count
        status = "Unparsed"
        if all_candidates_rejected:
            status = "Human_Complete"
        elif parsed_count > 0:
            status = "DB_Ready" if effective_exportable_count > 0 and effective_blocked_count == 0 else "Initial_Parsed"
        if str(paper.workflow_status or "") == "Suspected_Missing":
            status = "Suspected_Missing"
        return {
            "schema_version": "dft_completeness_audit_v1_light",
            "coverage_status": status,
            "status_label": {
                "Unparsed": "未解析",
                "Initial_Parsed": "初步解析",
                "Suspected_Missing": "疑似漏提",
                "Human_Complete": "人工确认完整",
                "DB_Ready": "可入库",
            }.get(status, status),
            "detected_signal_count": parsed_count,
            "detected_sections": 0,
            "detected_tables": 0,
            "detected_figures": 0,
            "parsed_dft_count": parsed_count,
            "exportable_dft_count": effective_exportable_count,
            "blocked_dft_count": effective_blocked_count,
            "suspected_missing_count": 1 if status == "Suspected_Missing" else 0,
            "coverage_ratio": 1.0 if parsed_count and effective_blocked_count == 0 else 0.0,
            "unique_candidate_count": parsed_count,
            "duplicate_evidence_count": 0,
            "rescan_recommended": status == "Suspected_Missing",
            "rescan_stop_reason": "all_candidates_rejected" if all_candidates_rejected else None,
            "rescan_next_status": "Needs_IDE_Rescan" if status == "Suspected_Missing" else None,
            "low_recall_warning": status == "Suspected_Missing",
            "low_recall_reasons": [],
            "ide_ai_review_recommended": status in {"Initial_Parsed", "Suspected_Missing"},
            "signal_examples": [],
        }
