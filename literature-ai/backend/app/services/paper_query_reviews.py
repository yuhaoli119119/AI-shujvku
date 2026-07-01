from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db.models import (
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperFigure,
    PaperNote,
    PaperSection,
    WritingCard,
)
from app.utils.review_safety import writing_card_gate


class PaperQueryReviewMixin:
    def _paper_detail_review_status(
        self,
        *,
        paper_id: UUID,
        paper: Paper,
        sections: list[PaperSection],
        figures: list[PaperFigure],
        writing_cards: list[WritingCard],
        dft_results: list[DFTResult],
        full_translation: str | None,
        figure_audits: dict[str, list[dict[str, Any]]],
        figure_conflicts: dict[str, list[dict[str, Any]]],
        writing_card_audits: dict[str, list[dict[str, Any]]],
        writing_card_conflicts: dict[str, list[dict[str, Any]]],
        dft_result_audits: dict[str, list[dict[str, Any]]],
        dft_result_conflicts: dict[str, list[dict[str, Any]]],
    ) -> dict[str, str]:
        reviewed_fields = self._batch_ai_reviewed_fields(paper_id)

        def collection_status(name: str, has_content: bool) -> str:
            if not has_content:
                return "missing"
            aliases = {name, name.rstrip("s")}
            return "ai_verified" if aliases.intersection(reviewed_fields) else "raw_only"

        if not writing_cards:
            writing_status = "missing"
        elif any(self._audit_list_marks_ai_verified(audits_by_card) for audits_by_card in writing_card_audits.values()):
            writing_status = "ai_verified"
        elif any(writing_card_gate(card).can_use_for_writing for card in writing_cards):
            writing_status = "ai_verified"
        else:
            writing_status = collection_status("writing_cards", True)

        if not figures:
            figure_status = "missing"
        elif any(figure_conflicts.get(str(figure.id)) for figure in figures) or any(
            self._figure_has_risk(figure) for figure in figures
        ):
            figure_status = "risk"
        elif any(
            self._audit_list_marks_ai_verified(figure_audits.get(str(figure.id), []))
            for figure in figures
        ):
            figure_status = "ai_verified"
        else:
            figure_status = collection_status("figures", True)

        return {
            "abstract_review_status": collection_status("abstract", bool(paper.abstract)),
            "sections_review_status": collection_status("sections", bool(sections)),
            "writing_cards_review_status": writing_status,
            "translation_review_status": "final_trusted" if full_translation else "missing",
            "figures_review_status": figure_status,
            "dft_review_status": self._dft_review_status(dft_results, dft_result_audits, dft_result_conflicts),
        }

    def _batch_ai_reviewed_fields(self, paper_id: UUID) -> set[str]:
        reviewed: set[str] = set()
        expected_decisions = {"approve", "approved", "accept", "verified", "revise", "update"}
        for note in self.session.scalars(select(PaperNote).where(PaperNote.paper_id == paper_id)).all():
            source = str(note.source or "").lower()
            content = str(note.content or "").lower()
            if source == "ide_ai" or "[ai_reviewed]" in content:
                field_name = str(note.field_name or "").strip().lower()
                reviewed.add(field_name)
                reviewed.add(field_name.split(":", 1)[0])
        for correction in self.session.scalars(
            select(PaperCorrection).where(
                PaperCorrection.paper_id == paper_id,
                PaperCorrection.status == "approved",
            )
        ).all():
            source = str(correction.source or "").lower()
            reviewer = str(correction.reviewed_by or "").lower()
            if source == "ide_ai" or "ide" in reviewer:
                reviewed.add(str(correction.field_name or "").strip().lower())
                reviewed.add(str(correction.target_path or "").split(":", 1)[0].strip().lower())
        for candidate in self.session.scalars(
            select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper_id)
        ).all():
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            decision = str(payload.get("decision") or payload.get("verdict") or "").strip().lower()
            verification = str(payload.get("verification_status") or "").strip().lower()
            source = str(payload.get("source") or "").lower()
            is_reviewed = candidate.status in {"ai_applied", "ai_reviewed", "materialized"} or (
                ("ide" in source or "[ai_reviewed]" in str(payload).lower())
                and (decision in expected_decisions or verification in {"verified", "ai_verified", "reviewed"})
            )
            if not is_reviewed:
                continue
            field_name = str(payload.get("field_name") or "").strip().lower()
            reviewed.add(field_name)
            reviewed.add(field_name.split(":", 1)[0])
            reviewed.add(str(payload.get("target_type") or "").strip().lower())
            reviewed.add(str(payload.get("target_path") or "").split(":", 1)[0].strip().lower())
        reviewed.discard("")
        return reviewed


    def _scalar_content_review_status(self, paper_id: UUID, field_name: str, has_content: bool) -> str:
        if not has_content:
            return "missing"
        if self._has_ai_applied_candidate(paper_id, field_names={field_name}, target_prefixes={field_name}):
            return "ai_verified"
        if self._has_ai_approved_correction(paper_id, field_names={field_name}, target_prefixes={field_name}):
            return "ai_verified"
        if self._has_ai_review_note(paper_id, field_names={field_name}):
            return "ai_verified"
        return "raw_only"


    def _collection_review_status(self, paper_id: UUID, collection: str, has_content: bool) -> str:
        if not has_content:
            return "missing"
        if self._has_ai_applied_candidate(paper_id, field_names={collection}, target_prefixes={collection}):
            return "ai_verified"
        if self._has_ai_approved_correction(paper_id, field_names={collection}, target_prefixes={collection}):
            return "ai_verified"
        if self._has_ai_review_note(paper_id, field_names={collection, collection.rstrip("s")}):
            return "ai_verified"
        return "raw_only"


    def _writing_cards_review_status(
        self,
        paper_id: UUID,
        writing_cards: list[WritingCard],
        audits_by_card: dict[str, list[dict[str, Any]]],
        conflicts_by_card: dict[str, list[dict[str, Any]]],
    ) -> str:
        if not writing_cards:
            return "missing"
        if any(self._audit_list_marks_ai_verified(audits_by_card.get(str(card.id), [])) for card in writing_cards):
            return "ai_verified"
        if any(writing_card_gate(card).can_use_for_writing for card in writing_cards):
            return "ai_verified"
        if any(conflicts_by_card.get(str(card.id)) for card in writing_cards):
            return "raw_only"
        return self._collection_review_status(paper_id, "writing_cards", True)


    def _reviewed_writing_card_paper_ids(self, paper_ids: set[UUID]) -> set[UUID]:
        if not paper_ids:
            return set()

        writing_card_rows = self.session.execute(
            select(WritingCard.id, WritingCard.paper_id).where(WritingCard.paper_id.in_(paper_ids))
        ).all()
        if not writing_card_rows:
            return set()

        candidate_paper_ids = {paper_id for _, paper_id in writing_card_rows}
        reviewed_paper_ids: set[UUID] = set()
        expected_fields = {"writing_cards", "writing_card"}

        notes = self.session.scalars(
            select(PaperNote).where(PaperNote.paper_id.in_(candidate_paper_ids))
        ).all()
        for note in notes:
            field = str(note.field_name or "").strip().lower()
            if not self._review_field_matches(field, expected_fields):
                continue
            source = str(note.source or "").lower()
            content = str(note.content or "").lower()
            if source == "ide_ai" or "[ai_reviewed]" in content:
                reviewed_paper_ids.add(note.paper_id)

        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id.in_(candidate_paper_ids))
            .where(PaperCorrection.status == "approved")
        ).all()
        for correction in corrections:
            source = str(correction.source or "").lower()
            reviewer = str(correction.reviewed_by or "").lower()
            if source != "ide_ai" and "ide" not in reviewer:
                continue
            field = str(correction.field_name or "").strip().lower()
            target = str(correction.target_path or "").strip().lower()
            if self._review_field_matches(field, expected_fields) or self._review_field_matches(target, expected_fields):
                reviewed_paper_ids.add(correction.paper_id)

        applied_candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id.in_(candidate_paper_ids))
            .where(ExternalAnalysisCandidate.status.in_(["ai_applied", "ai_reviewed", "materialized"]))
        ).all()
        for candidate in applied_candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            field = str(payload.get("field_name") or "").strip().lower()
            target = str(payload.get("target_path") or "").strip().lower()
            if self._review_field_matches(field, expected_fields) or self._review_field_matches(target, expected_fields):
                reviewed_paper_ids.add(candidate.paper_id)

        audit_candidates = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id.in_(candidate_paper_ids))
            .where(ExternalAnalysisCandidate.candidate_type == "object_review_audit")
        ).all()
        for candidate, run in audit_candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = str(payload.get("target_type") or "").strip().lower()
            if target_type not in expected_fields:
                continue
            source = str(run.source or "").lower()
            source_label = str(run.source_label or "").lower()
            decision = str(payload.get("decision") or "").strip().lower()
            verification = str(payload.get("verification_status") or "").strip().lower()
            if ("ide_ai" in source or "ide" in source_label or "[ai_reviewed]" in json.dumps(payload, ensure_ascii=False).lower()) and (
                decision in {"approve", "approved", "accept", "verified", "revise", "update"}
                or verification in {"verified", "ai_verified", "reviewed"}
            ):
                reviewed_paper_ids.add(candidate.paper_id)

        return reviewed_paper_ids


    def _figures_review_status(
        self,
        paper_id: UUID,
        figures: list[PaperFigure],
        audits_by_figure: dict[str, list[dict[str, Any]]],
        conflicts_by_figure: dict[str, list[dict[str, Any]]],
    ) -> str:
        if not figures:
            return "missing"
        if any(conflicts_by_figure.get(str(figure.id)) for figure in figures):
            return "risk"
        if any(self._figure_has_risk(figure) for figure in figures):
            return "risk"
        if any(self._audit_list_marks_ai_verified(audits_by_figure.get(str(figure.id), [])) for figure in figures):
            return "ai_verified"
        if self._has_ai_review_note(paper_id, field_names={"figures", "figure"}):
            return "ai_verified"
        if self._has_ai_applied_candidate(paper_id, field_names={"figures"}, target_prefixes={"figures"}):
            return "ai_verified"
        if self._has_ai_approved_correction(paper_id, field_names={"figures"}, target_prefixes={"figures"}):
            return "ai_verified"
        return "raw_only"


    @staticmethod
    def _figure_has_risk(figure: PaperFigure) -> bool:
        crop_status = str(getattr(figure, "crop_status", "") or "").lower()
        role = str(getattr(figure, "figure_role", "") or "").lower()
        return (not getattr(figure, "image_path", None)) or crop_status in {"missing", "failed", "needs_review"} or role == "noise"


    @staticmethod
    def _dft_review_status(
        dft_results: list[DFTResult],
        audits_by_result: dict[str, list[dict[str, Any]]],
        conflicts_by_result: dict[str, list[dict[str, Any]]],
    ) -> str:
        if not dft_results:
            return "missing"
        if any(conflicts_by_result.get(str(item.id)) for item in dft_results):
            return "conflict"
        if any(item.candidate_status == "Needs_Human_Confirmation" for item in dft_results):
            return "conflict"
        reviewed_statuses = {"ML_Ready", "human_reviewed_needs_evidence", "Gemini_Verified", "Rejected"}
        if any(item.candidate_status in reviewed_statuses for item in dft_results):
            return "reviewed"
        if any(audits_by_result.get(str(item.id)) for item in dft_results):
            return "reviewed"
        return "candidate"


    @staticmethod
    def _audit_list_marks_ai_verified(audits: list[dict[str, Any]]) -> bool:
        for audit in audits:
            source = str(audit.get("source") or "").lower()
            source_label = str(audit.get("source_label") or "").lower()
            decision = str(audit.get("decision") or "").lower()
            verification = str(audit.get("verification_status") or "").lower()
            if ("ide_ai" in source or "ide" in source_label or "[ai_reviewed]" in str(audit).lower()) and (
                decision in {"approve", "approved", "accept", "verified", "revise", "update"}
                or verification in {"verified", "ai_verified", "reviewed"}
            ):
                return True
        return False


    def _has_ai_review_note(self, paper_id: UUID, *, field_names: set[str]) -> bool:
        normalized = {item.lower() for item in field_names}
        notes = self.session.scalars(
            select(PaperNote)
            .where(PaperNote.paper_id == paper_id)
            .order_by(PaperNote.created_at.desc())
            .limit(100)
        ).all()
        for note in notes:
            source = str(note.source or "").lower()
            content = str(note.content or "").lower()
            field = str(note.field_name or "").strip().lower()
            if not self._review_field_matches(field, normalized):
                continue
            if source == "ide_ai" or "[ai_reviewed]" in content:
                return True
        return False


    def _has_ai_applied_candidate(
        self,
        paper_id: UUID,
        *,
        field_names: set[str],
        target_prefixes: set[str],
    ) -> bool:
        normalized_fields = {item.lower() for item in field_names}
        normalized_prefixes = {item.lower() for item in target_prefixes}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
            .where(ExternalAnalysisCandidate.status.in_(["ai_applied", "ai_reviewed", "materialized"]))
            .order_by(ExternalAnalysisCandidate.created_at.desc())
            .limit(100)
        ).all()
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            field = str(payload.get("field_name") or "").strip().lower()
            target = str(payload.get("target_path") or "").strip().lower()
            if self._review_field_matches(field, normalized_fields):
                return True
            if self._review_field_matches(target, normalized_prefixes):
                return True
        return False


    def _has_ai_approved_correction(
        self,
        paper_id: UUID,
        *,
        field_names: set[str],
        target_prefixes: set[str],
    ) -> bool:
        normalized_fields = {item.lower() for item in field_names}
        normalized_prefixes = {item.lower() for item in target_prefixes}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.status == "approved")
            .order_by(PaperCorrection.created_at.desc())
            .limit(200)
        ).all()
        for correction in corrections:
            source = str(correction.source or "").lower()
            reviewer = str(correction.reviewed_by or "").lower()
            if source != "ide_ai" and "ide" not in reviewer:
                continue
            field = str(correction.field_name or "").strip().lower()
            target = str(correction.target_path or "").strip().lower()
            if self._review_field_matches(field, normalized_fields):
                return True
            if self._review_field_matches(target, normalized_prefixes):
                return True
        return False


    @staticmethod
    def _review_field_matches(value: str, expected: set[str]) -> bool:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return False
        if normalized in expected:
            return True
        return any(
            normalized.startswith(prefix + ":")
            or normalized.startswith(prefix + "/")
            or normalized.startswith(prefix + ".")
            for prefix in expected
        )


    def _table_corrections_by_target(
        self,
        paper_id: UUID | set[UUID],
        table_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not table_ids:
            return {}
        paper_ids = paper_id if isinstance(paper_id, set) else {paper_id}
        corrections_by_table: dict[str, list[dict[str, Any]]] = {table_id: [] for table_id in table_ids}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id.in_(paper_ids))
            .where(PaperCorrection.status.in_(["pending", "requires_resolution", "approved", "rejected"]))
            .order_by(PaperCorrection.created_at.desc())
        ).all()
        for correction in corrections:
            target_type, target_id, target_field = self._parse_correction_target_path(correction.target_path)
            if target_type == "codex_item" and target_id in table_ids:
                target_type = "tables"
                target_field = correction.field_name or target_field
            if target_type not in {"table", "tables", "paper_table", "paper_tables"}:
                continue
            if target_id not in table_ids:
                continue
            corrections_by_table.setdefault(target_id, []).append(
                {
                    "correction_id": str(correction.id),
                    "field_name": str(target_field or correction.field_name or "").strip(),
                    "status": correction.status,
                    "source": correction.source,
                    "reviewed_by": correction.reviewed_by,
                    "created_at": correction.created_at.isoformat() if correction.created_at else None,
                }
            )
        return corrections_by_table


    def _figure_object_review_audits(
        self,
        paper_id: UUID,
        figure_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        return self._object_review_audits_by_target(
            paper_id,
            figure_ids,
            target_types={"figure", "figures", "paper_figure", "paper_figures"},
        )


    def _object_review_audits_by_target(
        self,
        paper_id: UUID,
        target_ids: set[str],
        *,
        target_types: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not target_ids:
            return {}
        audits_by_target: dict[str, list[dict[str, Any]]] = {target_id: [] for target_id in target_ids}
        deduped_by_target: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {target_id: {} for target_id in target_ids}
        normalized_target_types = {target_type.strip().lower() for target_type in target_types}
        candidates = self.session.scalars(
            select(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.paper_id == paper_id)
            .where(ExternalAnalysisCandidate.candidate_type == "object_review_audit")
            .order_by(ExternalAnalysisCandidate.created_at.desc())
        ).all()
        for candidate in candidates:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = str(payload.get("target_type") or "").strip().lower()
            decision = str(payload.get("decision") or "").strip().lower()
            target_id = str(
                payload.get("target_id")
                or payload.get("figure_id")
                or payload.get("writing_card_id")
                or payload.get("mechanism_claim_id")
                or payload.get("record_id")
                or ""
            )
            if (
                target_type == "dft_results"
                and (target_id.lower() == "new" or decision == "new_candidate")
                and str(candidate.materialized_target_type or "").strip().lower() == "dft_results"
                and str(candidate.materialized_target_id or "").strip()
            ):
                target_id = str(candidate.materialized_target_id).strip()
            if target_id not in target_ids or target_type not in normalized_target_types:
                continue
            audit_payload = self._object_review_audit_payload(candidate, payload)
            dedupe_key = self._object_review_audit_dedupe_key(target_type, audit_payload)
            target_bucket = deduped_by_target.setdefault(target_id, {})
            existing = target_bucket.get(dedupe_key)
            if existing is None or self._object_review_audit_payload_rank(audit_payload) > self._object_review_audit_payload_rank(existing):
                target_bucket[dedupe_key] = audit_payload
        for target_id, deduped in deduped_by_target.items():
            audits_by_target[target_id] = sorted(
                deduped.values(),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )[:5]
        return audits_by_target


    @staticmethod
    def _object_review_audit_payload(
        candidate: ExternalAnalysisCandidate,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "candidate_id": str(candidate.id),
            "candidate_type": candidate.candidate_type,
            "status": candidate.status,
            "target_type": payload.get("target_type"),
            "target_id": (
                payload.get("target_id")
                or payload.get("figure_id")
                or payload.get("writing_card_id")
                or payload.get("mechanism_claim_id")
                or payload.get("record_id")
            ),
            "field_name": payload.get("field_name") or payload.get("field"),
            "source": str(payload.get("source") or "unknown"),
            "source_label": payload.get("source_label"),
            "agent_role": payload.get("agent_role"),
            "model_name": payload.get("model_name"),
            "decision": payload.get("decision") or payload.get("verdict"),
            "recommended_action": payload.get("recommended_action"),
            "verification_status": payload.get("verification_status", "unverified"),
            "confidence": payload.get("confidence") if payload.get("confidence") is not None else candidate.confidence,
            "reason": payload.get("reason") or payload.get("reviewer_note") or payload.get("summary"),
            "evidence_checked": payload.get("evidence_checked"),
            "evidence_location": payload.get("evidence_location"),
            "blocking_errors": payload.get("blocking_errors") or [],
            "corrected_value": payload.get("corrected_value"),
            "created_at": candidate.created_at.isoformat() if candidate.created_at else None,
        }


    @staticmethod
    def _object_review_audit_dedupe_key(target_type: str, payload: dict[str, Any]) -> tuple[Any, ...]:
        field_name = str(payload.get("field_name") or "").strip()
        decision = str(payload.get("decision") or "").strip().lower()
        if target_type == "dft_results" and decision == "new_candidate" and field_name in {"", "dft_results"}:
            field_name = "dft_results"
        evidence = payload.get("evidence_location")
        corrected = payload.get("corrected_value")
        return (
            str(payload.get("source_label") or payload.get("source") or "").strip().lower(),
            decision,
            field_name,
            json.dumps(corrected, sort_keys=True, ensure_ascii=False, default=str),
            json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str),
        )


    @staticmethod
    def _object_review_audit_payload_rank(payload: dict[str, Any]) -> tuple[int, int]:
        field_name = str(payload.get("field_name") or "").strip()
        corrected = payload.get("corrected_value")
        return (
            1 if field_name else 0,
            1 if corrected not in (None, "", [], {}) else 0,
        )


    def _figure_approved_corrections(
        self,
        paper_id: UUID,
        figure_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not figure_ids:
            return {}
        corrections_by_figure: dict[str, list[dict[str, Any]]] = {figure_id: [] for figure_id in figure_ids}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.status == "approved")
            .order_by(PaperCorrection.created_at.desc())
        ).all()
        for correction in corrections:
            target_type, target_id, target_field = self._parse_correction_target_path(correction.target_path)
            if target_type not in {"figure", "figures", "paper_figure", "paper_figures"}:
                continue
            if target_id not in figure_ids:
                continue
            field_name = target_field or correction.field_name
            corrections_by_figure.setdefault(target_id, []).append(
                {
                    "correction_id": str(correction.id),
                    "field_name": str(field_name or "").strip(),
                    "source": correction.source,
                    "reviewed_by": correction.reviewed_by,
                    "created_at": correction.created_at.isoformat() if correction.created_at else None,
                }
            )
        return corrections_by_figure


    def _figure_pending_corrections(
        self,
        paper_id: UUID,
        figure_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not figure_ids:
            return {}
        corrections_by_figure: dict[str, list[dict[str, Any]]] = {figure_id: [] for figure_id in figure_ids}
        corrections = self.session.scalars(
            select(PaperCorrection)
            .where(PaperCorrection.paper_id == paper_id)
            .where(PaperCorrection.status.in_(["pending", "requires_resolution"]))
            .order_by(PaperCorrection.created_at.desc())
        ).all()
        for correction in corrections:
            target_type, target_id, target_field = self._parse_correction_target_path(correction.target_path)
            if target_type not in {"figure", "figures", "paper_figure", "paper_figures"}:
                continue
            if target_id not in figure_ids:
                continue
            field_name = target_field or correction.field_name
            corrections_by_figure.setdefault(target_id, []).append(
                {
                    "correction_id": str(correction.id),
                    "field_name": str(field_name or "").strip(),
                    "source": correction.source,
                    "reviewed_by": correction.reviewed_by,
                    "status": correction.status,
                    "created_at": correction.created_at.isoformat() if correction.created_at else None,
                }
            )
        return corrections_by_figure


    @staticmethod
    def _parse_correction_target_path(target_path: str | None) -> tuple[str, str, str | None]:
        parts = [part.strip() for part in str(target_path or "").split(":")]
        if len(parts) >= 2:
            target_type = parts[0].lower()
            target_id = parts[1]
            target_field = parts[2] if len(parts) >= 3 and parts[2] else None
            return target_type, target_id, target_field
        return "", "", None
