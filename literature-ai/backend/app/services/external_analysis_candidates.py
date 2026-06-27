from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db.models import ExternalAnalysisCandidate, ExternalAnalysisRun, Paper
from app.services.dft_rescan_policy import normalize_source_document_type
from app.services.external_analysis_models import (
    ExternalAnalysisNormalizedModel,
    ExternalAuditOpinionModel,
    ExternalObjectReviewAuditModel,
)
from app.services.external_analysis_normalization import ExternalAnalysisNormalizationMixin
from app.utils.artifact_status import build_paper_artifact_status
from app.utils.protocol_tracking import protocol_snapshot


class ExternalAnalysisCandidatePersistenceMixin:
    def _external_audit_precondition(self, paper: Paper) -> dict[str, Any]:
        artifact_status = build_paper_artifact_status(paper, settings=self.settings)
        blocking_errors = list(artifact_status.get("blocking_errors") or [])
        return {
            "status": "ready" if artifact_status.get("artifact_ready_for_external_audit") else "artifact_precondition_failed",
            "blocking_errors": blocking_errors,
            "artifact_ready_for_external_audit": bool(artifact_status.get("artifact_ready_for_external_audit")),
        }

    def _create_candidates(self, run: ExternalAnalysisRun, normalized: ExternalAnalysisNormalizedModel) -> None:
        for opinion in normalized.external_audit_opinions:
            self._create_external_audit_candidate(run, opinion)
        for audit in normalized.object_review_audits:
            self._create_object_review_candidate(run, audit)
        for note in normalized.review_notes:
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="note",
                    normalized_payload=note.model_dump(mode="json"),
                    confidence=note.confidence,
                    mapping_reason=note.mapping_reason,
                    evidence_payload={
                        "page": note.page,
                        "section_title": note.section_title,
                        "quoted_text": note.quoted_text,
                    },
                    status="pending",
                )
            )
        for correction in normalized.correction_proposals:
            status = self._correction_candidate_status(correction)
            mapping_reason = correction.mapping_reason
            if (
                correction.field_name == "catalyst_samples"
                and status == "requires_resolution"
                and "evidence anchor" not in str(mapping_reason or "").lower()
            ):
                mapping_reason = (
                    "Catalyst sample corrections require at least one PDF evidence anchor: "
                    "page, section, quoted_text, table, or figure."
                )
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="correction",
                    normalized_payload=correction.model_dump(mode="json"),
                    confidence=correction.confidence,
                    mapping_reason=mapping_reason,
                    evidence_payload=correction.evidence_payload,
                    status=status,
                )
            )
        for relationship in normalized.supporting_papers:
            status = "pending" if relationship.target_paper_id else "requires_resolution"
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="relationship",
                    normalized_payload=relationship.model_dump(mode="json"),
                    confidence=relationship.confidence,
                    mapping_reason=relationship.mapping_reason,
                    evidence_payload={"note": relationship.note},
                    status=status,
                )
            )
        for item in normalized.unmapped_items:
            self.session.add(
                ExternalAnalysisCandidate(
                    run_id=run.id,
                    paper_id=run.paper_id,
                    candidate_type="unmapped",
                    normalized_payload=item,
                    confidence=item.get("confidence") if isinstance(item, dict) else None,
                    mapping_reason="Could not safely map this external analysis item",
                    status="requires_resolution",
                )
            )

    def _create_object_review_candidate(self, run: ExternalAnalysisRun, audit: ExternalObjectReviewAuditModel) -> None:
        payload = audit.model_dump(mode="json")
        payload.update(
            {
                "paper_id": str(run.paper_id),
                "run_id": str(run.id),
                "source": audit.source or run.source,
                "source_label": audit.source_label or run.source_label,
                "candidate_type": "object_review_audit",
                "status": "candidate",
                "verification_status": "unverified",
                "writes_final_truth": False,
                "human_confirmation_required": True,
            }
        )
        evidence_payload = {
            "source": payload.get("source"),
            "source_label": payload.get("source_label"),
            "target_type": payload.get("target_type"),
            "target_id": payload.get("target_id"),
            "field_name": payload.get("field_name"),
            "decision": payload.get("decision"),
            "evidence_checked": payload.get("evidence_checked"),
            "evidence_location": payload.get("evidence_location"),
            "source_document_type": normalize_source_document_type(
                (payload.get("evidence_location") or {}).get("source_document_type")
                if isinstance(payload.get("evidence_location"), dict)
                else None
            ),
            "dedupe_signature": payload.get("dedupe_signature"),
            "supporting_evidence": payload.get("supporting_evidence") or [],
            "borrowed_from_reference": bool(payload.get("borrowed_from_reference")),
            "blocking_errors": payload.get("blocking_errors") or [],
            "recommended_action": payload.get("recommended_action"),
            "verification_status": "unverified",
            "raw_payload": payload.get("raw_payload"),
            "protocol": protocol_snapshot("gemini_audit_protocol"),
            "writes_final_truth": False,
            "human_confirmation_required": True,
        }
        self.session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=run.paper_id,
                candidate_type="object_review_audit",
                normalized_payload=payload,
                confidence=audit.confidence,
                mapping_reason=audit.mapping_reason or "Imported object-level external review audit candidate",
                evidence_payload=evidence_payload,
                status="candidate",
            )
        )

    def _create_external_audit_candidate(self, run: ExternalAnalysisRun, opinion: ExternalAuditOpinionModel) -> None:
        payload = opinion.model_dump(mode="json")
        payload.update(
            {
                "paper_id": str(run.paper_id),
                "run_id": str(run.id),
                "source": run.source,
                "source_label": run.source_label,
                "candidate_type": "external_audit_opinion",
                "status": "candidate",
                "verification_status": "unverified",
                "writes_final_truth": False,
                "requires_human_confirmation": True,
            }
        )
        evidence_payload = {
            "source": run.source,
            "source_label": run.source_label,
            "verdict": payload.get("verdict"),
            "recommended_action": payload.get("recommended_action"),
            "suspected_missing": payload.get("suspected_missing") or [],
            "evidence_examples": payload.get("evidence_examples") or [],
            "verification_status": "unverified",
            "raw_payload": payload.get("raw_payload"),
            "protocol": protocol_snapshot("gemini_audit_protocol"),
            "writes_final_truth": False,
            "requires_human_confirmation": True,
        }
        self.session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=run.paper_id,
                candidate_type="external_audit_opinion",
                normalized_payload=payload,
                confidence=opinion.confidence,
                mapping_reason=opinion.mapping_reason or "Imported paper-level external audit opinion",
                evidence_payload=evidence_payload,
                status="candidate",
            )
        )

    def _with_paper_level_audit_opinion(
        self,
        normalized: ExternalAnalysisNormalizedModel,
        *,
        raw_payload: dict[str, Any] | list[Any] | str | None,
        source: str,
        paper_id: UUID,
    ) -> ExternalAnalysisNormalizedModel:
        if normalized.external_audit_opinions:
            return normalized
        opinion = self._paper_level_audit_opinion(raw_payload=raw_payload, source=source, paper_id=paper_id)
        if opinion is None:
            return normalized
        return normalized.model_copy(update={"external_audit_opinions": [opinion]})

    @staticmethod
    def _normalized_from_run(run: ExternalAnalysisRun) -> ExternalAnalysisNormalizedModel:
        if isinstance(run.normalized_payload, dict):
            try:
                return ExternalAnalysisNormalizedModel.model_validate(run.normalized_payload)
            except Exception:
                pass
        return ExternalAnalysisNormalizedModel()

    @staticmethod
    def _paper_level_audit_opinion(
        *,
        raw_payload: dict[str, Any] | list[Any] | str | None,
        source: str,
        paper_id: UUID,
    ) -> ExternalAuditOpinionModel | None:
        if not isinstance(raw_payload, dict):
            return None
        if isinstance(raw_payload.get("candidates"), list) and raw_payload.get("candidates"):
            return None
        if ExternalAnalysisNormalizationMixin._extract_object_review_audits(raw_payload):
            return None
        audit_keys = {
            "verdict",
            "recommended_action",
            "suspected_missing",
            "metadata_status",
            "section_structure_status",
            "table_status",
            "figure_status",
            "dft_status",
            "evidence_examples",
        }
        if not any(key in raw_payload for key in audit_keys):
            return None
        suspected_missing = raw_payload.get("suspected_missing") or raw_payload.get("missing_items") or []
        if isinstance(suspected_missing, (str, int, float, bool)):
            suspected_missing = [suspected_missing]
        evidence_examples = raw_payload.get("evidence_examples") or raw_payload.get("evidence") or []
        if isinstance(evidence_examples, (str, int, float, bool, dict)):
            evidence_examples = [evidence_examples]
        confidence = raw_payload.get("confidence")
        try:
            confidence = float(confidence) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence = None
        return ExternalAuditOpinionModel(
            paper_id=str(raw_payload.get("paper_id") or paper_id),
            source=source,
            verdict=str(raw_payload.get("verdict") or "").strip().upper() or None,
            recommended_action=raw_payload.get("recommended_action"),
            suspected_missing=list(suspected_missing) if isinstance(suspected_missing, list) else [],
            metadata_status=raw_payload.get("metadata_status"),
            section_structure_status=raw_payload.get("section_structure_status"),
            table_status=raw_payload.get("table_status"),
            figure_status=raw_payload.get("figure_status"),
            dft_status=raw_payload.get("dft_status"),
            evidence_examples=list(evidence_examples) if isinstance(evidence_examples, list) else [],
            raw_payload=raw_payload,
            confidence=confidence,
            mapping_reason="Paper-level external audit payload imported as candidate opinion",
        )
