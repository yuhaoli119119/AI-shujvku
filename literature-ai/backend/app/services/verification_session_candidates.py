from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
)
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_identity_service import (
    build_dft_scientific_identity,
    normalize_atom_pair,
    normalize_dft_value_kind,
    property_has_symmetric_atom_pair,
)
from app.services.dft_material_binding_service import DFTMaterialBindingService
from app.services.dft_rescan_policy import (
    is_dft_method_only_reaction_step,
    normalize_dft_reaction_step_for_identity,
    normalize_source_document_type,
)
from app.services.supplementary_dft_lifecycle_service import SupplementaryDFTLifecycleService
from app.utils.evidence_anchors import first_pdf_evidence_anchor
from app.utils.review_safety import DFT_REJECTED_STATUSES


class VerificationSessionDFTCandidateMixin:
    def _materialize_new_dft_candidates(
        self,
        *,
        paper_id: UUID,
        reviewer: str,
        candidate_run_id: UUID | None = None,
    ) -> dict[str, Any]:
        stmt = (
            select(
                ExternalAnalysisCandidate,
                ExternalAnalysisRun.id,
                ExternalAnalysisRun.source,
                ExternalAnalysisRun.source_label,
                ExternalAnalysisRun.source_identity,
                ExternalAnalysisRun.source_identity_verified,
            )
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
                ExternalAnalysisCandidate.status.in_(("candidate", "pending", "requires_resolution")),
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        )
        if candidate_run_id is not None:
            stmt = stmt.where(ExternalAnalysisCandidate.run_id == candidate_run_id)
        rows = self.session.execute(stmt).all()
        existing_dft_rows = self.session.scalars(
            select(DFTResult).where(DFTResult.paper_id == paper_id)
        ).all()
        existing_by_signature = self._existing_new_dft_signatures(paper_id, rows=existing_dft_rows)
        existing_by_subject_signature = self._existing_new_dft_subject_signatures(paper_id, rows=existing_dft_rows)
        existing_by_method_step_signature = self._existing_new_dft_method_step_signatures(
            paper_id,
            rows=existing_dft_rows,
        )
        existing_by_identity = {
            str(row.candidate_identity): row
            for row in existing_dft_rows
            if str(row.candidate_identity or "").strip()
        }
        materialized: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        issue_service = DFTAuditIssueService(self.session)
        issue_service.begin_import_batch(paper_id=paper_id)
        issue_lifecycle = DFTAuditIssueLifecycleService(self.session)
        for candidate, run_id, run_source, run_source_label, run_source_identity, run_source_identity_verified in rows:
            run = SimpleNamespace(
                id=run_id,
                source=run_source,
                source_label=run_source_label,
                source_identity=run_source_identity,
                source_identity_verified=run_source_identity_verified,
            )
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            decision = str(payload.get("decision") or "").strip().lower()
            target_id = str(payload.get("target_id") or "").strip().lower()
            if target_type != "dft_results" or (decision != "new_candidate" and target_id != "new"):
                continue
            issue = issue_service.create_or_update_missing_issue(
                paper_id=paper_id,
                candidate=candidate,
                run=run,
                payload=payload,
            )
            if bool(payload.get("borrowed_from_reference")):
                skipped.append({"candidate_id": str(candidate.id), "reason": "borrowed_supporting_reference"})
                self._retire_skipped_new_dft_candidate(candidate, reason="borrowed_supporting_reference")
                continue
            if self._is_supporting_reference_dft_payload(payload):
                skipped.append({"candidate_id": str(candidate.id), "reason": "borrowed_supporting_reference"})
                self._retire_skipped_new_dft_candidate(candidate, reason="borrowed_supporting_reference")
                continue
            candidate_item, reason = self._new_dft_candidate_item(payload, run=run, candidate_id=candidate.id)
            if candidate_item is None:
                skipped.append({"candidate_id": str(candidate.id), "reason": reason})
                if reason == "conflicting_atom_pair_aliases":
                    self._hold_new_dft_candidate_for_decision(candidate, reason=reason)
                    issue_lifecycle.mark_pending(issue, status="needs_user_decision", note=reason)
                else:
                    self._retire_skipped_new_dft_candidate(candidate, reason=reason)
                continue
            signature = candidate_item["signature"]
            existing = existing_by_signature.get(signature) if candidate_item["dedupe_allowed"] else None
            if existing is None:
                method_step_signature = self._new_dft_method_step_compatible_signature(candidate_item)
                if method_step_signature is not None:
                    existing = self._method_step_compatible_existing(
                        candidate_item,
                        existing_by_method_step_signature.get(method_step_signature, []),
                    )
            if existing is None:
                existing = self._same_bound_dft_result(
                    paper_id=paper_id,
                    candidate=candidate,
                    issue=issue,
                    candidate_item=candidate_item,
                )
            if existing is None and candidate_item["dedupe_allowed"]:
                subject_matches = existing_by_subject_signature.get(candidate_item["subject_signature"], [])
                if subject_matches:
                    reason = "conflicting_dft_observation_for_subject"
                    skipped.append({"candidate_id": str(candidate.id), "reason": reason})
                    self._hold_new_dft_candidate_for_decision(candidate, reason=reason)
                    issue_lifecycle.mark_pending(issue, status="needs_user_decision", note=reason)
                    continue
            if existing is not None and str(existing.candidate_status or "").strip().lower() in DFT_REJECTED_STATUSES:
                reason = "exact_dedupe_target_rejected"
                skipped.append({"candidate_id": str(candidate.id), "reason": reason})
                self._hold_new_dft_candidate_for_decision(candidate, reason=reason)
                issue_lifecycle.mark_pending(
                    issue,
                    status="needs_user_decision",
                    note=f"{reason}:{existing.id}",
                )
                continue
            try:
                issue_lifecycle.assert_candidate_binding_compatible(candidate, existing)
                issue_lifecycle.assert_missing_issue_binding_compatible(issue, existing)
            except ValueError as exc:
                reason = str(exc)
                if reason not in {"dft_candidate_bound_to_different_result", "dft_audit_issue_bound_to_different_result"}:
                    raise
                skipped.append({"candidate_id": str(candidate.id), "reason": reason})
                self._hold_new_dft_candidate_for_decision(candidate, reason=reason)
                self._mark_issue_for_binding_conflict(issue_lifecycle, issue, reason=reason)
                continue

            try:
                with self.session.begin_nested():
                    if existing is None:
                        existing, created = self._insert_new_dft_candidate_in_current_savepoint(
                            paper_id=paper_id,
                            candidate_item=candidate_item,
                            source_label=run.source_label or run.source or reviewer,
                        )
                        action = "created" if created else "deduplicated"
                    else:
                        self._maybe_upgrade_method_only_reaction_step(existing, candidate_item)
                        action = "deduplicated"
                    # Recheck after selecting or creating the canonical row so a concurrent
                    # binding change cannot leave partial DFT side effects behind.
                    issue_lifecycle.assert_candidate_binding_compatible(candidate, existing)
                    issue_lifecycle.assert_missing_issue_binding_compatible(issue, existing)
                    material_binding = DFTMaterialBindingService(self.session).ensure_row_binding(
                        row=existing,
                        material_identity=candidate_item["material_identity"],
                    )
                    issue_lifecycle.bind_candidate_to_result(candidate, existing)
                    issue_lifecycle.bind_missing_issue_to_result(
                        issue,
                        existing,
                        repaired_by=reviewer,
                        resolution_note=f"materialized_dft_result:{existing.id}",
                    )
                    support_lifecycle = self._resolve_materialized_support_candidate(
                        paper_id=paper_id,
                        candidate_item=candidate_item,
                        canonical_row=existing,
                        action=action,
                        reviewer=reviewer,
                    )
                    self.session.flush()
            except ValueError as exc:
                reason = str(exc)
                if reason not in {"dft_candidate_bound_to_different_result", "dft_audit_issue_bound_to_different_result"}:
                    raise
                skipped.append({"candidate_id": str(candidate.id), "reason": reason})
                self._hold_new_dft_candidate_for_decision(candidate, reason=reason)
                self._mark_issue_for_binding_conflict(issue_lifecycle, issue, reason=reason)
                continue

            identity = self._new_dft_identity(candidate_item["signature"])
            existing_by_identity[identity] = existing
            if candidate_item["dedupe_allowed"]:
                existing_by_signature[signature] = existing
                subject_rows = existing_by_subject_signature.setdefault(candidate_item["subject_signature"], [])
                if all(row.id != existing.id for row in subject_rows):
                    subject_rows.append(existing)
            method_step_signature = self._new_dft_method_step_compatible_signature(candidate_item)
            if method_step_signature is not None:
                method_rows = existing_by_method_step_signature.setdefault(method_step_signature, [])
                if all(row.id != existing.id for row in method_rows):
                    method_rows.append(existing)
            materialized.append(
                {
                    "candidate_id": str(candidate.id),
                    "action": action,
                    "dft_result_id": str(existing.id),
                    "issue_id": str(issue.id),
                    "property_type": existing.property_type,
                    "value": existing.value,
                    "unit": existing.unit,
                    "material_binding": material_binding,
                    "support_lifecycle": support_lifecycle,
                }
            )
        if materialized:
            self.session.add(
                AuditLog(
                    paper_id=paper_id,
                    action="materialize_new_dft_candidates",
                    source=reviewer,
                    target_type="paper",
                    target_id=str(paper_id),
                    payload={
                        "created_or_linked_count": len(materialized),
                        "skipped_count": len(skipped),
                        "policy": "IDE AI new_candidate rows become unverified DFTResult candidates only; they are not exportable/RAG-ready until the existing DFT safety gate passes.",
                    },
                )
            )
        self.session.flush()
        return {
            "materialized_count": len(materialized),
            "materialized_items": materialized,
            "skipped_count": len(skipped),
            "skipped_items": skipped,
        }

    def _same_bound_dft_result(
        self,
        *,
        paper_id: UUID,
        candidate: ExternalAnalysisCandidate,
        issue: Any,
        candidate_item: dict[str, Any],
    ) -> DFTResult | None:
        candidate_type = str(candidate.materialized_target_type or "").strip()
        candidate_id = str(candidate.materialized_target_id or "").strip()
        issue_type = str(issue.target_type or "").strip()
        issue_id = str(issue.target_id or "").strip()
        if not candidate_id or not issue_id or issue_id.lower() == "new":
            return None
        if candidate_type != "dft_results" or issue_type != "dft_results" or candidate_id != issue_id:
            return None
        try:
            row_id = UUID(candidate_id)
        except ValueError:
            return None
        row = self.session.get(DFTResult, row_id)
        if row is None or row.paper_id != paper_id:
            return None
        identity = self._scientific_identity_for_row(row)
        if identity.observation_signature != candidate_item["observation_signature"]:
            return None
        return row

    @staticmethod
    def _mark_issue_for_binding_conflict(
        issue_lifecycle: DFTAuditIssueLifecycleService,
        issue: Any,
        *,
        reason: str,
    ) -> None:
        if str(issue.status or "").strip().lower() in {"closed", "false_positive"}:
            return
        issue_lifecycle.mark_pending(issue, status="needs_user_decision", note=reason)

    def _new_dft_candidate_item(
        self,
        payload: dict[str, Any],
        *,
        run: ExternalAnalysisRun,
        candidate_id: UUID | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        corrected = payload.get("corrected_value")
        if not isinstance(corrected, dict):
            return None, "missing_structured_corrected_value"
        ml_predicted = payload.get("ml_predicted", corrected.get("ml_predicted"))
        if ml_predicted is True or str(ml_predicted or "").strip().lower() in {"1", "true", "yes"}:
            return None, "ml_predicted_not_dft_result"
        material_identity = self._first_text(
            corrected.get("material_identity"),
            corrected.get("material"),
            corrected.get("catalyst"),
            payload.get("normalized_material"),
            payload.get("normalized_material_or_catalyst"),
        )
        property_type = self._normalize_dft_property(
            self._first_text(
                corrected.get("property_type"),
                corrected.get("property"),
                corrected.get("energy_type"),
                payload.get("normalized_energy_type"),
            )
        )
        scientific_identity = build_dft_scientific_identity(payload)
        if scientific_identity.atom_pair.error_code == "conflicting_atom_pair_aliases":
            return None, "conflicting_atom_pair_aliases"
        value = self._float_or_none(corrected.get("value"))
        value_upper = self._float_or_none(corrected.get("value_upper"))
        value_kind = self._new_dft_value_kind(corrected, value_upper=value_upper)
        unit = self._first_text(corrected.get("unit"))
        evidence = payload.get("evidence_location") or payload.get("evidence_payload")
        pdf_anchor = first_pdf_evidence_anchor(evidence)
        if not material_identity:
            return None, "missing_material_identity"
        if not property_type:
            return None, "missing_property_type"
        if value is None:
            return None, "missing_value"
        if not unit:
            return None, "missing_unit"
        if pdf_anchor is None:
            return None, "missing_pdf_evidence_anchor"
        evidence_payload = evidence if isinstance(evidence, dict) else {"evidence": evidence}
        evidence_payload = {
            **evidence_payload,
            **{
                key: anchor_value
                for key, anchor_value in pdf_anchor.items()
                if anchor_value not in (None, "") and evidence_payload.get(key) in (None, "")
            },
        }
        source_table = self._first_text(corrected.get("source_table"), evidence_payload.get("table"))
        source_section = self._first_text(
            evidence_payload.get("section"),
            evidence_payload.get("section_title"),
            f"Page {evidence_payload.get('page')}" if evidence_payload.get("page") not in (None, "") else None,
        )
        source_figure = self._first_text(corrected.get("source_figure"), evidence_payload.get("figure"), source_table)
        method = self._first_text(corrected.get("method"), corrected.get("calculation_method"))
        temperature = self._first_text(corrected.get("temperature"), corrected.get("temperature_label"))
        reaction_step = self._first_text(
            corrected.get("reaction_step"),
            " | ".join(part for part in [method, temperature] if part),
        )
        adsorbate = self._first_text(corrected.get("adsorbate"), payload.get("adsorbate"))
        evidence_text = self._first_text(
            evidence_payload.get("quoted_text"),
            evidence_payload.get("evidence_text"),
            payload.get("reason"),
        )
        merged_evidence_payload = {
            **evidence_payload,
            "material_identity": material_identity,
            "source_label": run.source_label,
            "source": run.source,
            "corrected_value": corrected,
            "dedupe_signature": payload.get("dedupe_signature"),
            "import_policy": "new_candidate_unverified_dft_result",
        }
        source_dft_result_id = self._first_text(
            corrected.get("source_dft_result_id"),
            corrected.get("source_candidate_id"),
            evidence_payload.get("source_dft_result_id"),
            evidence_payload.get("source_candidate_id"),
        )
        source_paper_id = self._first_text(
            corrected.get("source_paper_id"),
            evidence_payload.get("source_paper_id"),
            evidence_payload.get("related_paper_id"),
        )
        if source_dft_result_id:
            merged_evidence_payload["source_dft_result_id"] = source_dft_result_id
        if source_paper_id:
            merged_evidence_payload["source_paper_id"] = source_paper_id
        identity_fields = {
            "property_subtype": self._first_text(corrected.get("property_subtype"), evidence_payload.get("property_subtype")),
            "active_site_instance_key": self._first_text(corrected.get("active_site_instance_key"), evidence_payload.get("active_site_instance_key")),
            "atom_pair": scientific_identity.atom_pair.canonical,
            "site_label": self._first_text(corrected.get("site_label"), corrected.get("adsorption_site"), evidence_payload.get("site_label")),
            "state_context": self._first_text(corrected.get("state_context"), evidence_payload.get("state_context")),
            "source_table_id": self._first_text(corrected.get("source_table_id"), evidence_payload.get("source_table_id")),
            "source_row_index": self._first_text(corrected.get("source_row_index"), evidence_payload.get("source_row_index")),
            "source_column_index": self._first_text(corrected.get("source_column_index"), evidence_payload.get("source_column_index")),
        }
        merged_evidence_payload.update({key: value for key, value in identity_fields.items() if value not in (None, "")})
        identity_payload = {
            "paper_id": str(payload.get("paper_id") or ""),
            "corrected_value": {
                **corrected,
                "material_identity": material_identity,
                "property_type": property_type,
                "adsorbate": adsorbate,
                "reaction_step": reaction_step,
                "value": value,
                "value_upper": value_upper,
                "value_kind": value_kind,
                "unit": unit,
                **{key: val for key, val in identity_fields.items() if val not in (None, "")},
            },
            "evidence_payload": evidence_payload,
        }
        scientific_identity = build_dft_scientific_identity(identity_payload)
        signature = scientific_identity.observation_signature
        if not scientific_identity.dedupe_allowed:
            signature = f"{signature}:candidate:{candidate_id or 'unbound'}"
        return (
            {
                "material_identity": material_identity,
                "property_type": property_type,
                "adsorbate": adsorbate,
                "value": value,
                "value_upper": value_upper,
                "value_kind": value_kind,
                "unit": unit,
                "reaction_step": reaction_step,
                "source_section": source_section,
                "source_figure": source_figure,
                "evidence_text": evidence_text,
                "confidence": payload.get("confidence"),
                "evidence_payload": merged_evidence_payload,
                "signature": signature,
                "subject_signature": scientific_identity.subject_signature,
                "observation_signature": scientific_identity.observation_signature,
                "dedupe_allowed": scientific_identity.dedupe_allowed,
                "identity_error_code": scientific_identity.atom_pair.error_code,
                "source_dft_result_id": source_dft_result_id,
                "source_paper_id": source_paper_id,
                **identity_fields,
            },
            "",
        )

    def _resolve_materialized_support_candidate(
        self,
        *,
        paper_id: UUID,
        candidate_item: dict[str, Any],
        canonical_row: DFTResult,
        action: str,
        reviewer: str,
    ) -> dict[str, Any] | None:
        source_id = str(candidate_item.get("source_dft_result_id") or "").strip()
        if not source_id:
            return None
        try:
            support_candidate_id = UUID(source_id)
        except ValueError as exc:
            raise ValueError("invalid_source_dft_result_id") from exc
        source_paper_id = str(candidate_item.get("source_paper_id") or "").strip()
        if source_paper_id:
            try:
                expected_source_paper_id = UUID(source_paper_id)
            except ValueError as exc:
                raise ValueError("invalid_source_paper_id") from exc
            source_row = self.session.get(DFTResult, support_candidate_id)
            if source_row is None or source_row.paper_id != expected_source_paper_id:
                raise ValueError("source_dft_result_does_not_belong_to_source_paper")
        return SupplementaryDFTLifecycleService(self.session).resolve(
            main_paper_id=paper_id,
            support_candidate_id=support_candidate_id,
            status="written_back" if action == "created" else "replaced",
            actor=reviewer,
            canonical_dft_result_id=canonical_row.id,
        )

    def _insert_new_dft_candidate(
        self,
        *,
        paper_id: UUID,
        candidate_item: dict[str, Any],
        source_label: str,
        existing_by_identity: dict[str, DFTResult] | None = None,
    ) -> DFTResult:
        identity = self._new_dft_identity(candidate_item["signature"])
        existing = existing_by_identity.get(identity) if existing_by_identity is not None else None
        if existing is None:
            existing = self.session.scalar(
                select(DFTResult).where(
                    DFTResult.paper_id == paper_id,
                    DFTResult.candidate_identity == identity,
                )
            )
        if existing is not None:
            return existing
        try:
            with self.session.begin_nested():
                row, _created = self._insert_new_dft_candidate_in_current_savepoint(
                    paper_id=paper_id,
                    candidate_item=candidate_item,
                    source_label=source_label,
                )
        except IntegrityError:
            winner = self.session.scalar(
                select(DFTResult).where(
                    DFTResult.paper_id == paper_id,
                    DFTResult.candidate_identity == identity,
                )
            )
            if winner is None:
                raise
            row = winner
        if existing_by_identity is not None:
            existing_by_identity[identity] = row
        return row

    def _insert_new_dft_candidate_in_current_savepoint(
        self,
        *,
        paper_id: UUID,
        candidate_item: dict[str, Any],
        source_label: str,
    ) -> tuple[DFTResult, bool]:
        identity = self._new_dft_identity(candidate_item["signature"])
        existing = self.session.scalar(
            select(DFTResult).where(
                DFTResult.paper_id == paper_id,
                DFTResult.candidate_identity == identity,
            )
        )
        if existing is not None:
            return existing, False
        row = DFTResult(
            paper_id=paper_id,
            adsorbate=candidate_item["adsorbate"],
            property_type=candidate_item["property_type"],
            value=candidate_item["value"],
            value_upper=candidate_item.get("value_upper"),
            value_kind=candidate_item.get("value_kind"),
            unit=candidate_item["unit"],
            reaction_step=candidate_item["reaction_step"],
            source_section=candidate_item["source_section"],
            source_figure=candidate_item["source_figure"],
            evidence_text=candidate_item["evidence_text"],
            confidence=candidate_item["confidence"],
            candidate_status="new_candidate",
            evidence_payload=candidate_item["evidence_payload"],
            extraction_protocol_version="ide_ai_new_candidate_v1",
            candidate_identity=identity,
        )
        self.session.add(row)
        self.session.flush()
        self._upsert_new_dft_locator(row, candidate_item["evidence_payload"], source_label=source_label)
        self.session.flush()
        return row, True

    @staticmethod
    def _new_dft_identity(signature: Any) -> str:
        canonical = json.dumps(signature, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _hold_new_dft_candidate_for_decision(self, candidate: ExternalAnalysisCandidate, *, reason: str) -> None:
        candidate.status = "requires_resolution"
        candidate.mapping_reason = reason
        self.session.add(candidate)
        self.session.flush()

    def _new_dft_value_kind(self, corrected: dict[str, Any], *, value_upper: float | None) -> str:
        return normalize_dft_value_kind(
            self._first_text(corrected.get("value_kind"), corrected.get("value_type")),
            value_upper=value_upper,
            property_type=corrected.get("property_type") or corrected.get("property"),
        )

    def _upsert_new_dft_locator(self, row: DFTResult, evidence_payload: dict[str, Any], *, source_label: str) -> None:
        page = self._int_or_none(evidence_payload.get("page"))
        if page is None:
            return
        locator = EvidenceLocator(
            paper_id=row.paper_id,
            source_type="table" if evidence_payload.get("table") else "pdf",
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            page=page,
            section=evidence_payload.get("section") or evidence_payload.get("section_title") or row.source_section,
            evidence_text=str(evidence_payload.get("quoted_text") or evidence_payload.get("evidence_text") or row.evidence_text or "PDF evidence"),
            locator_status="exact_page",
            locator_confidence=float(row.confidence or 0.8),
            parser_source=str(source_label or "external_ai_review")[:32],
        )
        self.session.add(locator)

    def _existing_new_dft_signatures(
        self,
        paper_id: UUID,
        *,
        rows: list[DFTResult] | None = None,
    ) -> dict[str, DFTResult]:
        if rows is None:
            rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[str, DFTResult] = {}
        for row in rows:
            identity = self._scientific_identity_for_row(row)
            if identity.dedupe_allowed:
                signatures.setdefault(identity.observation_signature, row)
        return signatures

    def _existing_new_dft_subject_signatures(
        self,
        paper_id: UUID,
        *,
        rows: list[DFTResult] | None = None,
    ) -> dict[str, list[DFTResult]]:
        if rows is None:
            rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[str, list[DFTResult]] = defaultdict(list)
        for row in rows:
            identity = self._scientific_identity_for_row(row)
            if identity.dedupe_allowed:
                signatures[identity.subject_signature].append(row)
        return signatures

    def _scientific_identity_for_row(self, row: DFTResult):
        evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        material_identity = self._first_text(evidence_payload.get("material_identity"))
        if row.catalyst_sample_id:
            sample = self.session.get(CatalystSample, row.catalyst_sample_id)
            if sample is not None and str(sample.name or "").strip():
                material_identity = str(sample.name).strip()
        return build_dft_scientific_identity(
            {
                "corrected_value": {
                    "material_identity": material_identity,
                    "property_type": row.property_type,
                    "adsorbate": row.adsorbate,
                    "reaction_step": row.reaction_step,
                    "value": row.value,
                    "value_upper": row.value_upper,
                    "value_kind": row.value_kind,
                    "unit": row.unit,
                },
                "evidence_payload": evidence_payload,
            }
        )

    def _existing_new_dft_method_step_signatures(
        self,
        paper_id: UUID,
        *,
        rows: list[DFTResult] | None = None,
    ) -> dict[tuple[str, ...], list[DFTResult]]:
        if rows is None:
            rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[tuple[str, ...], list[DFTResult]] = defaultdict(list)
        for row in rows:
            evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            material_identity = self._first_text(evidence_payload.get("material_identity"))
            if row.catalyst_sample_id:
                sample = self.session.get(CatalystSample, row.catalyst_sample_id)
                if sample is not None and str(sample.name or "").strip():
                    material_identity = str(sample.name).strip()
            signature = self._new_dft_method_step_compatible_signature(
                {
                    "material_identity": material_identity,
                    "property_type": row.property_type,
                    "value": row.value,
                    "value_upper": row.value_upper,
                    "value_kind": row.value_kind,
                    "unit": row.unit,
                    "adsorbate": row.adsorbate,
                    "reaction_step": row.reaction_step,
                    "active_site_instance_key": evidence_payload.get("active_site_instance_key"),
                    "atom_pair": evidence_payload.get("atom_pair"),
                    "site_label": evidence_payload.get("site_label"),
                    "state_context": evidence_payload.get("state_context"),
                }
            )
            if signature is not None:
                signatures[signature].append(row)
        return signatures

    @staticmethod
    def _new_dft_semantic_signature(candidate_item: dict[str, Any]) -> tuple[str, ...]:
        value = candidate_item.get("value")
        value_part = "" if value is None else f"{float(value):.8g}"
        value_upper = candidate_item.get("value_upper")
        value_upper_part = "" if value_upper is None else f"{float(value_upper):.8g}"
        base = [
            candidate_item.get("material_identity"),
            candidate_item.get("property_type"),
            value_part,
            value_upper_part,
            normalize_dft_value_kind(
                candidate_item.get("value_kind"),
                value_upper=value_upper,
                property_type=candidate_item.get("property_type"),
            ),
            candidate_item.get("unit"),
            candidate_item.get("adsorbate"),
            normalize_dft_reaction_step_for_identity(
                candidate_item.get("reaction_step"),
                property_type=candidate_item.get("property_type"),
                adsorbate=candidate_item.get("adsorbate"),
                material=candidate_item.get("material_identity"),
            ),
        ]
        extension = [
            candidate_item.get("property_subtype"),
            candidate_item.get("active_site_instance_key"),
            normalize_atom_pair(
                candidate_item.get("atom_pair"),
                symmetric=property_has_symmetric_atom_pair(candidate_item.get("property_type")),
            ),
            candidate_item.get("site_label"),
            candidate_item.get("state_context"),
        ]
        if any(str(part or "").strip() for part in extension):
            base.extend(extension)
        return tuple(
            str(part or "").strip().lower()
            for part in base
        )

    @staticmethod
    def _new_dft_method_step_compatible_signature(candidate_item: dict[str, Any]) -> tuple[str, ...] | None:
        property_type = str(candidate_item.get("property_type") or "").strip().lower()
        if property_type != "adsorption_energy":
            return None
        value = candidate_item.get("value")
        value_part = "" if value is None else f"{float(value):.8g}"
        value_upper = candidate_item.get("value_upper")
        value_upper_part = "" if value_upper is None else f"{float(value_upper):.8g}"
        base = [
            "method_step_compatible",
            candidate_item.get("material_identity"),
            candidate_item.get("property_type"),
            value_part,
            value_upper_part,
            normalize_dft_value_kind(
                candidate_item.get("value_kind"),
                value_upper=value_upper,
                property_type=candidate_item.get("property_type"),
            ),
            candidate_item.get("unit"),
            candidate_item.get("adsorbate"),
        ]
        extension = [
            candidate_item.get("active_site_instance_key"),
            normalize_atom_pair(
                candidate_item.get("atom_pair"),
                symmetric=property_has_symmetric_atom_pair(candidate_item.get("property_type")),
            ),
            candidate_item.get("site_label"),
            candidate_item.get("state_context"),
        ]
        if any(str(part or "").strip() for part in extension):
            base.extend(extension)
        return tuple(str(part or "").strip().lower() for part in base)

    @staticmethod
    def _method_step_compatible_existing(candidate_item: dict[str, Any], rows: list[DFTResult]) -> DFTResult | None:
        if not rows:
            return None
        candidate_method_only = is_dft_method_only_reaction_step(candidate_item.get("reaction_step"))
        if candidate_method_only:
            specific_rows = [row for row in rows if not is_dft_method_only_reaction_step(row.reaction_step)]
            candidates = specific_rows or rows
            return candidates[0] if len(candidates) == 1 else None

        method_only_rows = [row for row in rows if is_dft_method_only_reaction_step(row.reaction_step)]
        return method_only_rows[0] if len(rows) == 1 and len(method_only_rows) == 1 else None

    def _maybe_upgrade_method_only_reaction_step(self, row: DFTResult, candidate_item: dict[str, Any]) -> None:
        candidate_step = self._first_text(candidate_item.get("reaction_step"))
        if not candidate_step:
            return
        if is_dft_method_only_reaction_step(candidate_step):
            return
        if not is_dft_method_only_reaction_step(row.reaction_step):
            return
        if str(row.candidate_status or "").strip().lower() != "new_candidate":
            return
        row.reaction_step = candidate_step
        self.session.add(row)

    @staticmethod
    def _is_supporting_reference_dft_payload(payload: dict[str, Any]) -> bool:
        evidence = payload.get("evidence_location") or payload.get("evidence_payload")
        evidence = evidence if isinstance(evidence, dict) else {}
        corrected = payload.get("corrected_value") if isinstance(payload.get("corrected_value"), dict) else {}
        source_type = normalize_source_document_type(
            payload.get("source_document_type")
            or payload.get("source_type")
            or evidence.get("source_document_type")
            or evidence.get("source_type")
            or corrected.get("source_document_type")
            or corrected.get("source_type")
        )
        return source_type == "supporting_reference"

    @staticmethod
    def _normalize_dft_property(value: Any) -> str | None:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "activation_energy": "activation_energy",
            "activation": "activation_energy",
            "permeance": "permeance",
            "permeability": "permeance",
            "adsorption_energy": "adsorption_energy",
            "reaction_barrier": "reaction_barrier",
            "permeation_barrier": "permeation_barrier",
            "binding_energy": "binding_energy",
            "metal_support_binding_energy_eb": "metal_support_binding_energy_Eb",
            "stability_parameter_es": "stability_parameter_Es",
            "formation_energy": "formation_energy",
            "cohesive_energy": "cohesive_energy",
            "lowdin_charge": "Lowdin_charge",
            "löwdin_charge": "Lowdin_charge",
            "icohp": "ICOHP",
            "cohp": "COHP",
            "d_orbital_occupancy": "d_orbital_occupancy",
            "dos_at_fermi": "DOS_at_Fermi",
            "bond_length_li_s": "bond_length_Li-S",
            "bond_length_s_s": "bond_length_S-S",
            "bond_length_m_n": "bond_length_M-N",
            "bond_length_m_s": "bond_length_M-S",
            "bond_length_m_m": "bond_length_M-M",
        }
        return aliases.get(text, text or None)

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if value in (None, "", []):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
