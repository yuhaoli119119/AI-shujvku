from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult, Paper, PaperRelationship
from app.domain.reaction_taxonomy import (
    PROFILE_VERSION,
    classify_reaction_record,
    normalize_reaction_type,
    validate_reaction_record,
)
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.utils.ai_verification import ai_target_fingerprint, cached_read_pdf_page_text, normalize_evidence_text
from app.utils.dft_candidate_status import DFT_STATUS_FIELD_VERIFIED, DFT_STATUS_READY_AI
from app.utils.review_safety import is_export_eligible_extraction


class DFTReactionLabelError(ValueError):
    """Raised when a controlled reaction-label write is not allowed."""


# The quote has to carry Li-S reaction context or name the adsorbed species; a
# bare page number or caption fragment is not an attribution.  These are the
# platform's own SRR context markers (``reaction_taxonomy`` required_context_terms).
_SRR_QUOTE_TERMS = (
    "li-s",
    "li\u2013s",
    "lithium sulfur",
    "lithium-sulfur",
    "polysulfide",
    "polysulphide",
    "srr",
    "sulfur reduction",
    "sulphur reduction",
)

# Every column this service may write, always audited as a complete old/new pair.
AUDITED_FIELDS = (
    "reaction_type",
    "reaction_validation_status",
    "reaction_profile_version",
    "reaction_type_source",
    "reaction_type_confidence",
)

LABEL_SOURCE = "controlled_reaction_label_entry"


class DFTReactionLabelService:
    """Controlled writer for ``reaction_type`` / ``reaction_validation_status``.

    The classifier is used as a *check*, never as the source of truth.  The caller
    must supply a verbatim quote from a stored page of the target paper or of its
    linked supplementary; the server recomputes both the reaction classification
    and the validation verdict from the record itself.  Consequences:

    * an arbitrary ``valid`` claim is impossible - the status is derived;
    * an existing different attribution is never silently overwritten, and an
      existing ``out_of_scope`` is never auto-changed;
    * the record must already be evidence-verified for its own required fields;
    * the write takes a transaction-scoped advisory lock (the same mechanism the
      direct-apply field locks use) and then *re-reads* the row under
      ``SELECT ... FOR UPDATE`` before deciding, so a check-then-write race cannot
      lose a concurrent update;
    * the audit row stores the complete previous and new values of every column
      this service writes;
    * a repeated identical request is a no-op.
    """

    LABEL_FIELDS = ("reaction_type", "reaction_validation_status")

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ public
    def assign(
        self,
        *,
        paper_id: UUID,
        record_id: Any,
        reaction_type: Any,
        evidence: Any,
        expected_target_fingerprint: str,
        dry_run: bool = True,
        commit: bool = False,
        actor: str = "mcp_ai",
    ) -> dict[str, Any]:
        row = self._load_record(paper_id=paper_id, record_id=record_id)

        # Serialise every label decision for this record, then re-read under a row
        # lock: the fingerprint comparison below is only meaningful after this.
        self._lock_record(paper_id=paper_id, record_id=row.id)
        row = self._reload_locked(record_id=row.id)

        current_fingerprint = ai_target_fingerprint("dft_results", row)
        if str(expected_target_fingerprint or "").strip() != current_fingerprint:
            raise DFTReactionLabelError("target_fingerprint_mismatch")

        requested_type = normalize_reaction_type(reaction_type)
        if requested_type == "UNKNOWN":
            raise DFTReactionLabelError("requested_reaction_type_not_supported")

        existing_type = (
            normalize_reaction_type(row.reaction_type) if str(row.reaction_type or "").strip() else None
        )
        if existing_type and existing_type != requested_type:
            raise DFTReactionLabelError("existing_reaction_type_differs")
        if str(row.reaction_validation_status or "").strip().lower() == "out_of_scope":
            # A stored ``out_of_scope`` verdict is a recorded decision and is never
            # overwritten silently.  It is only stale when the record's own
            # *current* fields no longer justify it -- for example after a reviewed
            # correction of ``property_type`` / ``adsorbate``.  Re-derive the
            # verdict from the row itself, so a corrected record is not stranded
            # forever, and keep refusing whenever the stored verdict still holds.
            stored_verdict = validate_reaction_record(
                existing_type or requested_type,
                {
                    "adsorbate": row.adsorbate,
                    "property_type": row.property_type,
                    "reaction_step": row.reaction_step,
                },
            )
            if not stored_verdict.get("valid"):
                raise DFTReactionLabelError("existing_out_of_scope_not_auto_changed")

        # The record must already be evidence-verified for its own required fields.
        gate = is_export_eligible_extraction(self.session, row, target_type="dft_results")
        if not gate.eligible:
            raise DFTReactionLabelError(
                "record_not_evidence_verified: " + ",".join(list(gate.reasons)[:5])
            )

        evidence_check = self._validate_evidence(paper_id=paper_id, row=row, evidence=evidence)

        # Server-side recomputation.  The classifier only confirms the attribution
        # that the supplied page evidence already supports; it never supplies it.
        payload = {
            "adsorbate": row.adsorbate,
            "property_type": row.property_type,
            "reaction_step": row.reaction_step,
            "evidence_text": evidence_check["quote"],
        }
        classification = classify_reaction_record(payload)
        if normalize_reaction_type(classification.get("reaction_type")) != requested_type:
            raise DFTReactionLabelError(
                f"reaction_type_not_supported_by_record:{classification.get('reason')}"
            )
        validation = validate_reaction_record(requested_type, payload)
        if not validation.get("valid"):
            raise DFTReactionLabelError(
                "reaction_record_out_of_scope:" + ",".join(validation.get("reasons") or [])
            )
        new_status = str(validation["status"])

        original = {field: getattr(row, field) for field in AUDITED_FIELDS}
        new_values = {
            "reaction_type": requested_type,
            "reaction_validation_status": new_status,
            "reaction_profile_version": PROFILE_VERSION,
            "reaction_type_source": LABEL_SOURCE,
            "reaction_type_confidence": classification.get("confidence"),
        }

        if all(original[field] == new_values[field] for field in AUDITED_FIELDS):
            return {
                "status": "already_applied",
                "wrote": False,
                "record_id": str(row.id),
                "reaction_type": requested_type,
                "reaction_validation_status": new_status,
                "target_fingerprint": current_fingerprint,
                "evidence": evidence_check,
                "candidate_status_refresh": self._refresh_candidate_status_after_label(
                    row, actor=actor
                ),
            }

        plan: dict[str, Any] = {
            "record_id": str(row.id),
            "original": original,
            "new": new_values,
            "target_fingerprint": current_fingerprint,
            "evidence": evidence_check,
            "classification": classification,
            "validation": validation,
        }
        if dry_run or not commit:
            plan["status"] = "dry_run_ok"
            plan["wrote"] = False
            return plan

        for field, value in new_values.items():
            setattr(row, field, value)
        self.session.add(row)
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="assign_dft_reaction_label",
                source=actor,
                target_type="dft_results",
                target_id=str(row.id),
                payload={
                    "actor": actor,
                    "record_id": str(row.id),
                    "original": original,
                    "new": new_values,
                    "evidence": evidence_check,
                    "classification": classification,
                    "validation": validation,
                    "target_fingerprint_before": current_fingerprint,
                },
            )
        )
        self.session.flush()
        plan["candidate_status_refresh"] = self._refresh_candidate_status_after_label(
            row, actor=actor
        )
        plan["status"] = "applied"
        plan["wrote"] = True
        return plan

    def _refresh_candidate_status_after_label(
        self, row: DFTResult, *, actor: str
    ) -> dict[str, Any] | None:
        """Upgrade a verified record to ML-ready once the new label makes it eligible.

        The reaction attribution is part of the ML contract, so writing it can turn
        a ``field_verified_ml_pending`` record into an exportable one -- and no
        other entry point recomputes the stored status afterwards.  Upgrade only:
        this writer never downgrades a record it was not asked to weaken, and it
        fails closed (an unevaluable record keeps its current label).
        """

        if str(row.candidate_status or "").strip() != DFT_STATUS_FIELD_VERIFIED:
            return None
        from app.services.dft_ml_readiness import evaluate_dft_record_ml_readiness

        try:
            ready = bool(evaluate_dft_record_ml_readiness(self.session, row).ml_ready)
        except Exception:  # noqa: BLE001 - never promote a record that cannot be evaluated
            return None
        if not ready:
            return None
        previous = row.candidate_status
        row.candidate_status = DFT_STATUS_READY_AI
        self.session.add(row)
        self.session.add(
            AuditLog(
                paper_id=row.paper_id,
                action="refresh_dft_candidate_status_after_reaction_label",
                source=actor,
                target_type="dft_results",
                target_id=str(row.id),
                payload={
                    "actor": actor,
                    "previous_candidate_status": str(previous or ""),
                    "candidate_status": DFT_STATUS_READY_AI,
                    "reason": "ml_readiness_contract_passed_after_reaction_label",
                    "reaction_type": row.reaction_type,
                    "reaction_validation_status": row.reaction_validation_status,
                },
            )
        )
        self.session.flush()
        return {"previous": str(previous or ""), "candidate_status": DFT_STATUS_READY_AI, "wrote": True}

    def compensate(
        self,
        *,
        paper_id: UUID,
        record_id: Any,
        audit_log_id: Any,
        actor: str = "mcp_ai",
        dry_run: bool = True,
        commit: bool = False,
    ) -> dict[str, Any]:
        """Revert one earlier label write, driven by that write's own audit row.

        The original audit row is never deleted or rewritten: the revert appends a
        new ``compensate_dft_reaction_label`` row.  The revert only proceeds while
        the record's current values are still exactly what the referenced change
        wrote, so a later update from another actor is never overwritten.
        """

        try:
            resolved_audit_id = audit_log_id if isinstance(audit_log_id, UUID) else UUID(str(audit_log_id))
        except (TypeError, ValueError):
            raise DFTReactionLabelError("invalid_audit_log_id")
        audited = self.session.get(AuditLog, resolved_audit_id)
        if audited is None or audited.action != "assign_dft_reaction_label":
            raise DFTReactionLabelError("compensation_source_audit_row_not_found")
        if str(audited.paper_id) != str(paper_id):
            raise DFTReactionLabelError("compensation_source_audit_row_wrong_paper")
        payload = audited.payload if isinstance(audited.payload, dict) else {}
        if str(payload.get("record_id") or audited.target_id or "") != str(record_id):
            raise DFTReactionLabelError("compensation_source_audit_row_target_mismatch")
        written = payload.get("new") or {}
        original = payload.get("original") or {}

        row = self._load_record(paper_id=paper_id, record_id=record_id)
        self._lock_record(paper_id=paper_id, record_id=row.id)
        row = self._reload_locked(record_id=row.id)

        current = {field: getattr(row, field) for field in AUDITED_FIELDS}
        if any(current[field] != written.get(field) for field in AUDITED_FIELDS):
            raise DFTReactionLabelError("record_changed_since_this_write_refusing_compensation")

        restored = {field: original.get(field) for field in AUDITED_FIELDS}
        result: dict[str, Any] = {
            "record_id": str(row.id),
            "source_audit_log_id": str(audited.id),
            "current": current,
            "restored": restored,
        }
        if dry_run or not commit:
            result["status"] = "dry_run_ok"
            result["wrote"] = False
            return result

        for field, value in restored.items():
            setattr(row, field, value)
        self.session.add(row)
        self.session.add(
            AuditLog(
                paper_id=paper_id,
                action="compensate_dft_reaction_label",
                source=actor,
                target_type="dft_results",
                target_id=str(row.id),
                payload={
                    "actor": actor,
                    "record_id": str(row.id),
                    "source_audit_log_id": str(audited.id),
                    "original": current,
                    "new": restored,
                    "reason": "compensation_of_failed_verification_run",
                },
            )
        )
        self.session.flush()
        result["status"] = "compensated"
        result["wrote"] = True
        return result

    # ----------------------------------------------------------------- helpers
    def _lock_record(self, *, paper_id: UUID, record_id: UUID) -> None:
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        digest = hashlib.sha256(f"dft-reaction-label-v1:{paper_id}:{record_id}".encode("utf-8")).digest()
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": int.from_bytes(digest[:8], byteorder="big", signed=True)},
        )

    def _reload_locked(self, *, record_id: UUID) -> DFTResult:
        self.session.expire_all()
        row = self.session.scalars(
            select(DFTResult)
            .where(DFTResult.id == record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            raise DFTReactionLabelError("dft_result_not_found_for_paper")
        return row

    def _load_record(self, *, paper_id: UUID, record_id: Any) -> DFTResult:
        try:
            resolved = record_id if isinstance(record_id, UUID) else UUID(str(record_id))
        except (TypeError, ValueError):
            raise DFTReactionLabelError("invalid_record_id")
        row = self.session.get(DFTResult, resolved)
        if row is None or row.paper_id != paper_id:
            raise DFTReactionLabelError("dft_result_not_found_for_paper")
        return row

    def _authorized_evidence_paper(self, *, paper_id: UUID, source_paper_id: Any) -> Paper:
        try:
            candidate = UUID(str(source_paper_id).strip())
        except (AttributeError, TypeError, ValueError):
            raise DFTReactionLabelError("invalid_evidence_source_paper_id")
        if candidate == paper_id:
            paper = self.session.get(Paper, paper_id)
            if paper is None:
                raise DFTReactionLabelError("target_paper_not_found")
            return paper
        related = self.session.scalar(
            select(PaperRelationship.id)
            .where(
                PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                or_(
                    and_(
                        PaperRelationship.source_paper_id == paper_id,
                        PaperRelationship.target_paper_id == candidate,
                    ),
                    and_(
                        PaperRelationship.target_paper_id == paper_id,
                        PaperRelationship.source_paper_id == candidate,
                    ),
                ),
            )
            .limit(1)
        )
        if related is None:
            raise DFTReactionLabelError("evidence_paper_not_linked_to_target")
        paper = self.session.get(Paper, candidate)
        if paper is None:
            raise DFTReactionLabelError("evidence_paper_not_found")
        return paper

    def _validate_evidence(self, *, paper_id: UUID, row: DFTResult, evidence: Any) -> dict[str, Any]:
        if not isinstance(evidence, dict):
            raise DFTReactionLabelError("evidence_required")
        quote = str(evidence.get("quote") or "").strip()
        if not quote:
            raise DFTReactionLabelError("evidence_quote_required")
        try:
            page_number = int(evidence.get("page"))
        except (TypeError, ValueError):
            raise DFTReactionLabelError("evidence_page_required")
        if page_number < 1:
            raise DFTReactionLabelError("evidence_page_required")

        paper = self._authorized_evidence_paper(
            paper_id=paper_id, source_paper_id=evidence.get("source_paper_id")
        )
        page_text, error, _path = cached_read_pdf_page_text(self.session, paper, page_number)
        if error is not None or not page_text:
            raise DFTReactionLabelError(f"evidence_page_unreadable:{error or 'empty_page'}")
        if normalize_evidence_text(quote) not in normalize_evidence_text(page_text):
            raise DFTReactionLabelError("evidence_quote_not_on_page")
        if not self._quote_supports_attribution(quote, row):
            raise DFTReactionLabelError("evidence_quote_does_not_support_reaction_attribution")
        return {
            "source_paper_id": str(paper.id),
            "page": page_number,
            "quote": quote,
            "quote_on_page": True,
        }

    @staticmethod
    def _quote_supports_attribution(quote: str, row: DFTResult) -> bool:
        lowered = (
            quote.casefold()
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .replace("\u2212", "-")
        )
        if any(term in lowered for term in _SRR_QUOTE_TERMS):
            return True
        adsorbate = str(row.adsorbate or "").casefold().strip()
        return bool(adsorbate) and adsorbate in lowered
