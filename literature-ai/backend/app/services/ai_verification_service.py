from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import (
    AuditLog,
    DFTAuditIssue,
    EvidenceClaim,
    EvidenceLocator,
    ExtractionFieldReview,
    Paper,
    PaperSection,
)
from app.schemas.ai_verification import AIVerificationSubmission
from app.services.dft_audit_issue_lifecycle_service import DFT_AUDIT_ISSUE_PENDING_STATUSES
from app.services.content_knowledge_service import ContentKnowledgeService
from app.services.evidence_page_recovery import EvidencePageRecoveryService, compact_page_text
from app.utils.ai_verification import (
    AI_VERIFICATION_CAPABILITY,
    AI_VERIFICATION_POLICY_VERSION,
    ai_field_snapshot,
    ai_target_fingerprint,
    canonical_ai_target_type,
    get_ai_target,
    locator_fingerprint,
    matching_locator,
    normalize_evidence_text,
    read_pdf_page_text,
    stable_hash,
)
from app.utils.review_safety import writing_card_authoritative_chain_gate


@dataclass(frozen=True)
class AuthenticatedAIVerificationIdentity:
    source_identity: str
    source_label: str
    model_agent: str
    capabilities: frozenset[str]
    identity_verified: bool


class AIVerificationService:
    """Single-AI admission service with deterministic evidence rechecks.

    The caller supplies one AI judgment. This service never calls another model,
    never asks for consensus, and never maps the caller to a human/Owner identity.
    """

    MAX_TASK_PAGE_SIZE = 50
    MAX_SUBMISSION_BATCH_SIZE = 20
    _NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?")
    _WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+./-]{2,}")
    _STOPWORDS = {
        "the", "and", "for", "that", "with", "this", "from", "were", "was", "are",
        "into", "using", "used", "study", "result", "results", "show", "shows", "can",
    }

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    @property
    def batch_limit(self) -> int:
        return max(
            1,
            min(
                self.MAX_SUBMISSION_BATCH_SIZE,
                int(self.settings.ai_verification_batch_limit),
            ),
        )

    def list_tasks(
        self,
        *,
        paper_id: UUID,
        limit: int = 20,
        offset: int = 0,
        recover_evidence: bool = True,
        target_type: str | None = None,
    ) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")
        bounded = max(1, min(int(limit), self.MAX_TASK_PAGE_SIZE))
        page_offset = int(offset)
        if page_offset < 0:
            raise ValueError("offset must be greater than or equal to zero")
        tasks: list[dict[str, Any]] = []
        target_specs = (
            ("mechanism_claims", "claim_text"),
            ("dft_results", "value"),
            ("electrochemical_performance", "capacity"),
            ("sections", "text"),
            ("section_page_fragments", "text"),
            ("writing_cards", "evidence_chain"),
        )
        from app.utils.ai_verification import _TARGET_MODELS  # internal policy registry

        supported_target_types = {item[0] for item in target_specs}
        normalized_target_type = str(target_type or "").strip() or None
        if normalized_target_type is not None:
            if normalized_target_type not in supported_target_types:
                raise ValueError(f"Unsupported AI verification target_type: {normalized_target_type}")
            target_specs = tuple(item for item in target_specs if item[0] == normalized_target_type)

        pending_targets: list[tuple[str, str, Any, ExtractionFieldReview | None]] = []
        for current_target_type, field_name in target_specs:
            target_model = _TARGET_MODELS[current_target_type]
            target_query = select(target_model).where(target_model.paper_id == paper_id)
            if current_target_type == "section_page_fragments":
                target_query = target_query.where(
                    EvidenceClaim.source_type == "section_page_fragment"
                )
            rows = self.session.scalars(target_query.order_by(target_model.id.asc())).all()
            for target in rows:
                existing = self._find_review(
                    paper_id,
                    current_target_type,
                    str(target.id),
                    field_name,
                )
                if existing is not None and existing.reviewer_status in {"verified", "ai_verified"}:
                    continue
                pending_targets.append((current_target_type, field_name, target, existing))

        total = len(pending_targets)
        page_targets = pending_targets[page_offset : page_offset + bounded]
        recovery_service = EvidencePageRecoveryService(self.session, self.settings)
        recovery_statuses: Counter[str] = Counter()
        exact_candidate_count = 0
        candidate_count = 0
        page_fragment_count = 0

        for current_target_type, field_name, target, existing in page_targets:
            snapshot = ai_field_snapshot(current_target_type, target, field_name)
            if recover_evidence:
                recovery = recovery_service.recover_for_target(
                    paper=paper,
                    target_type=current_target_type,
                    target_id=str(target.id),
                    field_name=field_name,
                    target_value=snapshot.get("value"),
                    evidence_text=str(snapshot.get("evidence_text") or ""),
                    evidence_types=list(getattr(target, "evidence_types", None) or []),
                    limit=3,
                )
                candidates = recovery["candidates"]
            else:
                locators = self._candidate_locators(
                    paper_id,
                    current_target_type,
                    str(target.id),
                    field_name,
                )
                candidates = [
                    {
                        "page": locator.page,
                        "quoted_text": locator.evidence_text,
                        "evidence_text": locator.evidence_text,
                        "locator_status": locator.locator_status,
                        "source_type": locator.source_type,
                        "extraction_source": locator.parser_source,
                        "match_method": "persisted_locator",
                        "warning_reason": None,
                    }
                    for locator in locators[:3]
                ]
                recovery = {
                    "status": "disabled",
                    "candidate_count": len(candidates),
                    "exact_candidate_count": sum(
                        item.get("locator_status") in {"exact_page", "exact_bbox"}
                        for item in candidates
                    ),
                    "candidates": candidates,
                    "blocked_reasons": [],
                    "page_text_statuses": {},
                    "database_writes": False,
                }
            recovery_statuses[str(recovery["status"])] += 1
            candidate_count += int(recovery["candidate_count"])
            exact_candidate_count += int(recovery["exact_candidate_count"])
            page_fragment_recovery = None
            if (
                recover_evidence
                and
                current_target_type == "sections"
                and str(getattr(target, "section_type", "") or "").casefold() == "body"
            ):
                page_fragment_recovery = recovery_service.recover_section_page_fragments(
                    paper=paper,
                    section=target,
                    limit=50,
                )
                page_fragment_count += int(page_fragment_recovery.get("fragment_count") or 0)
            task = {
                "paper_id": str(paper_id),
                "target_type": current_target_type,
                "target_id": str(target.id),
                "field_name": field_name,
                "target_snapshot": snapshot,
                "target_snapshot_fingerprint": ai_target_fingerprint(current_target_type, target),
                "expected_write_version": int(existing.write_version or 1) if existing else None,
                "current_status": existing.reviewer_status if existing else "pending",
                "evidence_candidates": candidates,
                "evidence_recovery": {
                    key: value for key, value in recovery.items() if key != "candidates"
                },
            }
            if page_fragment_recovery is not None:
                task["section_page_fragment_recovery"] = page_fragment_recovery
            tasks.append(task)

        returned = len(tasks)
        next_offset = page_offset + returned
        has_more = next_offset < total
        return {
            "paper_id": str(paper_id),
            "target_type": normalized_target_type,
            "total": total,
            "returned": returned,
            "offset": page_offset,
            "limit": bounded,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "task_count": returned,
            "tasks": tasks,
            "recover_evidence": recover_evidence,
            "evidence_recovery_summary": {
                "status_distribution": dict(sorted(recovery_statuses.items())),
                "candidate_count": candidate_count,
                "exact_candidate_count": exact_candidate_count,
                "section_page_fragment_count": page_fragment_count,
            },
            "batch_limit": self.batch_limit,
            "max_page_size": self.MAX_TASK_PAGE_SIZE,
            "single_ai": True,
            "second_ai_used": False,
            "embedding_requests": 0,
            "embedding_role": "retrieval_only",
            "database_writes": False,
        }

    def process_batch(
        self,
        *,
        paper_id: UUID,
        submissions: list[AIVerificationSubmission | dict[str, Any]],
        identity: AuthenticatedAIVerificationIdentity,
        dry_run: bool = True,
        commit: bool = True,
    ) -> dict[str, Any]:
        self._require_identity(identity)
        if not submissions:
            raise ValueError("At least one AI verification submission is required")
        if len(submissions) > self.batch_limit:
            raise ValueError(f"AI verification batch exceeds limit {self.batch_limit}")
        if self.session.get(Paper, paper_id) is None:
            raise LookupError("Paper not found")

        items: list[dict[str, Any]] = []
        for raw in submissions:
            submission = raw if isinstance(raw, AIVerificationSubmission) else AIVerificationSubmission.model_validate(raw)
            try:
                if dry_run:
                    items.append(self._process_one(paper_id, submission, identity, dry_run=True))
                    continue
                with self.session.begin_nested():
                    items.append(self._process_one(paper_id, submission, identity, dry_run=False))
            except Exception as exc:
                items.append(
                    {
                        "target_type": submission.target_type,
                        "target_id": submission.target_id,
                        "field_name": submission.field_name,
                        "outcome": "exception",
                        "status": "needs_human",
                        "blocked_reasons": [
                            f"{'validation' if dry_run else 'write'}_failed:{type(exc).__name__}"
                        ],
                        "database_writes": False,
                    }
                )
        if not dry_run:
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        counts = {name: sum(item.get("outcome") == name for item in items) for name in (
            "auto_verified", "auto_repaired", "auto_rejected", "exception"
        )}
        return {
            "paper_id": str(paper_id),
            "dry_run": dry_run,
            "single_ai": True,
            "second_ai_used": False,
            "policy_version": AI_VERIFICATION_POLICY_VERSION,
            "capability": AI_VERIFICATION_CAPABILITY,
            "actor_type": "ai",
            "embedding_requests": 0,
            "embedding_role": "retrieval_only",
            **counts,
            "items": items,
            "database_writes": False if dry_run else any(item.get("database_writes") for item in items),
        }

    def _process_one(
        self,
        paper_id: UUID,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        canonical, target = get_ai_target(
            self.session,
            paper_id=paper_id,
            target_type=submission.target_type,
            target_id=submission.target_id,
        )
        required_core_fields = {
            "mechanism_claims": "claim_text",
            "sections": "text",
            "section_page_fragments": "text",
            "writing_cards": "evidence_chain",
        }
        required_field = required_core_fields.get(canonical)
        if required_field is not None and submission.field_name != required_field:
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception",
                status="exception" if canonical in {"sections", "section_page_fragments", "writing_cards"} else "needs_human",
                reasons=[f"{canonical}_requires_{required_field}"], dry_run=dry_run,
            )
        if canonical in {"sections", "section_page_fragments", "writing_cards"} and submission.decision == "correct":
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception", status="exception",
                reasons=["content_object_auto_repair_not_authorized"], dry_run=dry_run,
            )
        snapshot = ai_field_snapshot(canonical, target, submission.field_name)
        current_fingerprint = ai_target_fingerprint(canonical, target)
        existing = self._find_review(paper_id, canonical, submission.target_id, submission.field_name)
        idempotency_key = self._idempotency_key(paper_id, canonical, submission, identity)
        if self._is_idempotent(existing, idempotency_key):
            return {
                "target_type": canonical,
                "target_id": submission.target_id,
                "field_name": submission.field_name,
                "outcome": (
                    "auto_verified" if existing.reviewer_status == "ai_verified"
                    else "auto_rejected" if existing.reviewer_status == "rejected"
                    else "exception"
                ),
                "status": existing.reviewer_status,
                "blocked_reasons": [],
                "idempotent": True,
                "database_writes": False,
            }
        conflict_reason = self._write_conflict_reason(existing, submission, current_fingerprint)
        if conflict_reason:
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception", status="needs_human", reasons=[conflict_reason], dry_run=dry_run,
            )
        if existing is not None and existing.reviewer_status == "verified":
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome="exception", status="needs_human", reasons=["human_verified_requires_human_override"], dry_run=dry_run,
            )

        if submission.decision in {"reject", "exception"}:
            outcome = "auto_rejected" if submission.decision == "reject" else "exception"
            status = "rejected" if submission.decision == "reject" else (
                "exception" if canonical in {"sections", "section_page_fragments", "writing_cards"} else "needs_human"
            )
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome=outcome, status=status,
                reasons=["ai_rejected" if status == "rejected" else "ai_exception"], dry_run=dry_run,
            )

        paper = self.session.get(Paper, paper_id)
        reasons: list[str] = []
        evidence_checks: dict[str, bool] = {
            "target_exists": True,
            "target_belongs_to_paper": True,
            "evidence_text_present": bool(submission.evidence_text.strip()),
            "confidence_threshold": submission.confidence >= self.settings.ai_verification_min_confidence,
            "no_unresolved_conflict": not self._has_unresolved_conflict(canonical, target),
        }
        page_text: str | None = None
        pdf_error = "missing_page" if submission.page is None else None
        if paper is not None and submission.page is not None:
            page_text, pdf_error, _path = read_pdf_page_text(paper, submission.page)
        evidence_checks["real_pdf_present"] = pdf_error not in {"missing_real_pdf", "unreadable_pdf"}
        evidence_checks["page_valid"] = pdf_error is None
        evidence_checks["evidence_on_pdf_page"] = (
            pdf_error is None
            and bool(normalize_evidence_text(submission.evidence_text))
            and normalize_evidence_text(submission.evidence_text) in normalize_evidence_text(page_text)
        )

        value_for_gate = submission.proposed_value if submission.decision == "correct" else snapshot["value"]
        evidence_checks.update(self._content_checks(canonical, target, submission.field_name, value_for_gate, snapshot.get("unit"), submission.evidence_text))
        for key, passed in evidence_checks.items():
            if not passed:
                reasons.append(key)
        if reasons:
            exception_reasons = {
                "target_exists",
                "target_belongs_to_paper",
                "confidence_threshold",
                "no_unresolved_conflict",
                "real_pdf_present",
                "page_valid",
                "evidence_text_present",
                "material_identity_present",
            }
            reject_reasons = {"evidence_on_pdf_page", "numeric_value_matches", "unit_matches", "content_supported_by_evidence"}
            outcome = (
                "exception"
                if any(reason in exception_reasons for reason in reasons)
                else "auto_rejected"
                if any(reason in reject_reasons for reason in reasons)
                else "exception"
            )
            status = "rejected" if outcome == "auto_rejected" else (
                "exception" if canonical in {"sections", "section_page_fragments", "writing_cards"} else "needs_human"
            )
            return self._finalize_failure(
                paper_id, canonical, target, submission, identity,
                outcome=outcome, status=status, reasons=reasons, dry_run=dry_run,
                evidence_checks=evidence_checks,
            )

        locator = matching_locator(
            self.session,
            paper_id=paper_id,
            target_type=canonical,
            target_id=submission.target_id,
            field_name=submission.field_name,
            page=int(submission.page),
            evidence_text=submission.evidence_text,
        )
        locator_recovered = locator is None
        if not dry_run and locator is None:
            locator = EvidenceLocator(
                paper_id=paper_id,
                source_type="pdf",
                target_type=canonical,
                target_id=submission.target_id,
                field_name=submission.field_name,
                evidence_text=submission.evidence_text,
                page=int(submission.page),
                locator_status="exact_page",
                locator_confidence=submission.confidence,
                parser_source="single_ai_verification",
            )
            self.session.add(locator)
            self.session.flush()

        corrected = submission.decision == "correct"
        if corrected and not dry_run:
            self._apply_correction(canonical, target, submission.field_name, submission.proposed_value)
            self.session.add(target)
            self.session.flush()
        final_fingerprint = self._projected_fingerprint(canonical, target, submission) if dry_run else ai_target_fingerprint(canonical, target)
        effective_locator_fingerprint = (
            locator_fingerprint(locator)
            if locator is not None
            else stable_hash({
                "paper_id": str(paper_id), "target_type": canonical, "target_id": submission.target_id,
                "field_name": submission.field_name, "page": submission.page,
                "evidence_text": normalize_evidence_text(submission.evidence_text), "locator_status": "exact_page",
            })
        )
        locator_checks = {
            "exact_page_or_bbox": True,
            "locator_matches_target": True,
            "locator_matches_field": True,
            "locator_snapshot_current": True,
        }
        outcome = "auto_repaired" if corrected or locator_recovered else "auto_verified"
        if not dry_run:
            review = self._upsert_review(paper_id, canonical, submission.target_id, submission.field_name)
            review.original_value = snapshot["value"]
            review.reviewed_value = submission.proposed_value if corrected else snapshot["value"]
            review.unit = snapshot.get("unit")
            review.evidence_text = submission.evidence_text
            review.reviewer_status = "ai_verified"
            review.reviewer = identity.model_agent
            review.reviewer_note = submission.reasoning_summary
            review.target_resolution_status = "active"
            review.last_resolved_target_id = submission.target_id
            review.target_fingerprint = final_fingerprint
            review.review_payload = {
                "ai_verification": self._verification_payload(
                    submission, identity,
                    decision="corrected" if corrected else "verified",
                    target_fingerprint=final_fingerprint,
                    locator_fingerprint_value=effective_locator_fingerprint,
                    evidence_checks=evidence_checks,
                    locator_checks=locator_checks,
                    idempotency_key=idempotency_key,
                    outcome=outcome,
                )
            }
            self.session.add(review)
            self.session.flush()
            self._add_audit(paper_id, canonical, submission, identity, review.reviewer_status, outcome, [], review.review_payload)
            self._sync_content_projection(canonical, target)
        return {
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "outcome": outcome,
            "status": "ai_verified",
            "blocked_reasons": [],
            "evidence_checks": evidence_checks,
            "locator_checks": locator_checks,
            "target_snapshot_fingerprint": final_fingerprint,
            "idempotent": False,
            "database_writes": not dry_run,
        }

    def _finalize_failure(
        self,
        paper_id: UUID,
        canonical: str,
        target: Any,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        *,
        outcome: str,
        status: str,
        reasons: list[str],
        dry_run: bool,
        evidence_checks: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        if not dry_run:
            review = self._upsert_review(paper_id, canonical, submission.target_id, submission.field_name)
            if review.reviewer_status != "verified":
                snapshot = ai_field_snapshot(canonical, target, submission.field_name)
                review.original_value = snapshot["value"]
                review.reviewed_value = None
                review.unit = snapshot.get("unit")
                review.evidence_text = submission.evidence_text or snapshot.get("evidence_text")
                review.reviewer_status = status
                review.reviewer = identity.model_agent
                review.reviewer_note = submission.reasoning_summary
                review.target_resolution_status = "active"
                review.last_resolved_target_id = submission.target_id
                review.target_fingerprint = ai_target_fingerprint(canonical, target)
                payload = {
                    "ai_verification": self._verification_payload(
                        submission, identity, decision=status,
                        target_fingerprint=review.target_fingerprint,
                        locator_fingerprint_value="",
                        evidence_checks=evidence_checks or {reason: False for reason in reasons},
                        locator_checks={"exact_page_or_bbox": False},
                        idempotency_key=self._idempotency_key(paper_id, canonical, submission, identity),
                        outcome=outcome,
                    )
                }
                review.review_payload = payload
                self.session.add(review)
                self.session.flush()
                self._add_audit(paper_id, canonical, submission, identity, status, outcome, reasons, payload)
                self._sync_content_projection(canonical, target)
        return {
            "target_type": canonical,
            "target_id": submission.target_id,
            "field_name": submission.field_name,
            "outcome": outcome,
            "status": status,
            "blocked_reasons": list(dict.fromkeys(reasons)),
            "evidence_checks": evidence_checks or {},
            "idempotent": False,
            "database_writes": not dry_run,
        }

    def _verification_payload(
        self,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        *,
        decision: str,
        target_fingerprint: str,
        locator_fingerprint_value: str,
        evidence_checks: dict[str, bool],
        locator_checks: dict[str, bool],
        idempotency_key: str,
        outcome: str,
    ) -> dict[str, Any]:
        return {
            "actor_type": "ai",
            "identity_verified": True,
            "source_identity": identity.source_identity,
            "source_label": identity.source_label,
            "model_agent": identity.model_agent,
            "capability": AI_VERIFICATION_CAPABILITY,
            "policy_version": AI_VERIFICATION_POLICY_VERSION,
            "single_ai": True,
            "second_ai_used": False,
            "confidence": submission.confidence,
            "decision": decision,
            "outcome": outcome,
            "reasoning_summary": submission.reasoning_summary,
            "page": submission.page,
            "evidence_checks": evidence_checks,
            "locator_checks": locator_checks,
            "target_snapshot_fingerprint": target_fingerprint,
            "locator_fingerprint": locator_fingerprint_value,
            "idempotency_key": idempotency_key,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def _content_checks(
        self,
        canonical: str,
        target: Any,
        field_name: str,
        value: Any,
        unit: Any,
        evidence_text: str,
    ) -> dict[str, bool]:
        evidence = normalize_evidence_text(evidence_text)
        value_text = normalize_evidence_text(value)
        checks = {"content_supported_by_evidence": True}
        if canonical == "writing_cards":
            chain = target.evidence_chain if isinstance(target.evidence_chain, list) else []
            submitted = normalize_evidence_text(evidence_text)
            checks["evidence_matches_chain_item"] = bool(submitted) and any(
                submitted == normalize_evidence_text(item.get("text"))
                for item in chain
                if isinstance(item, dict)
            )
            chain_gate = writing_card_authoritative_chain_gate(self.session, target)
            checks["authoritative_evidence_chain"] = chain_gate.can_use_for_writing
            return checks
        if canonical == "sections":
            complete_single_page_text = (
                bool(compact_page_text(value))
                and compact_page_text(value) == compact_page_text(evidence_text)
            )
            if str(getattr(target, "section_type", "") or "").casefold() == "body":
                checks["content_supported_by_evidence"] = complete_single_page_text
                checks["complete_section_page_coverage"] = complete_single_page_text
                checks["numeric_value_matches"] = complete_single_page_text
                checks["key_entities_consistent"] = complete_single_page_text
                return checks
            if complete_single_page_text:
                checks["content_supported_by_evidence"] = True
                checks["numeric_value_matches"] = True
                checks["key_entities_consistent"] = True
                return checks
        if canonical == "section_page_fragments":
            exact_fragment = (
                bool(compact_page_text(value))
                and compact_page_text(value) == compact_page_text(evidence_text)
            )
            parent = (
                self.session.get(PaperSection, target.section_id)
                if target.section_id is not None
                else None
            )
            checks.update(
                {
                    "content_supported_by_evidence": exact_fragment,
                    "single_physical_pdf_page": (
                        target.page_start is not None
                        and target.page_start == target.page_end
                        and target.page_start >= 1
                    ),
                    "parent_section_bound": bool(
                        parent is not None
                        and parent.paper_id == target.paper_id
                        and compact_page_text(target.evidence_text)
                        in compact_page_text(parent.text)
                    ),
                    "numeric_value_matches": exact_fragment,
                    "unit_matches": exact_fragment,
                    "key_entities_consistent": exact_fragment,
                    "direction_consistent": exact_fragment,
                }
            )
            return checks
        if canonical in {"mechanism_claims", "sections"}:
            tokens = {token.casefold() for token in self._WORD_RE.findall(str(value or "")) if token.casefold() not in self._STOPWORDS}
            evidence_tokens = {token.casefold() for token in self._WORD_RE.findall(evidence_text)}
            checks["content_supported_by_evidence"] = bool(value_text) and (
                value_text in evidence or len(tokens & evidence_tokens) / max(1, len(tokens)) >= 0.45
            )
        numbers = self._NUMBER_RE.findall(str(value or ""))
        if isinstance(value, (int, float)):
            numbers = [str(value)]
        if numbers:
            evidence_numbers = [float(item) for item in self._NUMBER_RE.findall(evidence_text)]
            checks["numeric_value_matches"] = all(
                any(abs(float(number) - candidate) <= max(1e-8, abs(float(number)) * 1e-6) for candidate in evidence_numbers)
                for number in numbers
            )
        if unit:
            normalized_unit = normalize_evidence_text(unit).replace(" ", "")
            normalized_page_units = evidence.replace(" ", "")
            checks["unit_matches"] = normalized_unit in normalized_page_units
        if canonical == "dft_results":
            checks["material_identity_present"] = bool(target.catalyst_sample_id) or bool((target.evidence_payload or {}).get("material_identity"))
            for entity_name in ("adsorbate", "reaction_step"):
                entity = normalize_evidence_text(getattr(target, entity_name, None))
                if entity:
                    checks[f"{entity_name}_consistent"] = entity in evidence
        return checks

    def _sync_content_projection(self, canonical: str, target: Any) -> None:
        source_type_by_target = {
            "sections": "section",
            "section_page_fragments": "section_page_fragment",
            "writing_cards": "writing_card",
        }
        source_type = source_type_by_target.get(canonical)
        if source_type is None:
            return
        ContentKnowledgeService(self.session).sync_items(
            paper_id=target.paper_id,
            include_candidates=True,
            source_types=[source_type],
            source_ids=[str(target.id)],
        )

    def _has_unresolved_conflict(self, canonical: str, target: Any) -> bool:
        if canonical != "dft_results":
            return False
        return self.session.scalar(
            select(DFTAuditIssue.id).where(
                DFTAuditIssue.paper_id == target.paper_id,
                DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)),
                or_(DFTAuditIssue.result_id == target.id, DFTAuditIssue.target_id == str(target.id)),
            ).limit(1)
        ) is not None

    @staticmethod
    def _apply_correction(canonical: str, target: Any, field_name: str, proposed_value: Any) -> None:
        if proposed_value is None:
            raise ValueError("correct decision requires proposed_value")
        field_map = {
            "mechanism_claims": {"claim_text": "claim_text", "claim_type": "claim_type", "key_species": "evidence_types"},
            "dft_results": {"adsorbate": "adsorbate", "energy_type": "property_type", "value": "value", "reaction_step": "reaction_step"},
            "electrochemical_performance": {
                "sulfur_loading": "sulfur_loading_mg_cm2", "sulfur_content": "sulfur_content_wt_percent",
                "electrolyte_sulfur_ratio": "electrolyte_sulfur_ratio", "capacity": "capacity_value",
                "cycle_number": "cycle_number", "rate": "rate", "decay_per_cycle": "decay_per_cycle",
            },
            "sections": {"text": "text"},
            "writing_cards": {"research_gap": "research_gap", "proposed_solution": "proposed_solution", "core_hypothesis": "core_hypothesis", "evidence_chain": "evidence_chain"},
        }
        attr = field_map.get(canonical, {}).get(field_name)
        if not attr:
            raise ValueError(f"AI auto-repair is not supported for {canonical}.{field_name}")
        setattr(target, attr, proposed_value)

    def _projected_fingerprint(self, canonical: str, target: Any, submission: AIVerificationSubmission) -> str:
        if submission.decision != "correct":
            return ai_target_fingerprint(canonical, target)
        attr_map = {
            ("mechanism_claims", "claim_text"): "claim_text",
            ("sections", "text"): "text",
            ("writing_cards", "research_gap"): "research_gap",
            ("writing_cards", "proposed_solution"): "proposed_solution",
            ("writing_cards", "core_hypothesis"): "core_hypothesis",
        }
        attr = attr_map.get((canonical, submission.field_name))
        if not attr:
            return stable_hash({"before": ai_target_fingerprint(canonical, target), "field": submission.field_name, "value": submission.proposed_value})
        original = getattr(target, attr)
        try:
            setattr(target, attr, submission.proposed_value)
            return ai_target_fingerprint(canonical, target)
        finally:
            setattr(target, attr, original)

    def _candidate_locators(self, paper_id: UUID, target_type: str, target_id: str, field_name: str) -> list[EvidenceLocator]:
        rows = self.session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.target_id == target_id,
                EvidenceLocator.field_name == field_name,
            ).order_by(EvidenceLocator.page.asc().nulls_last()).limit(10)
        ).all()
        return [row for row in rows if self._same_target_type(target_type, row.target_type)]

    @staticmethod
    def _same_target_type(left: str, right: str | None) -> bool:
        try:
            return canonical_ai_target_type(left) == canonical_ai_target_type(str(right or ""))
        except ValueError:
            return False

    def _find_review(self, paper_id: UUID, target_type: str, target_id: str, field_name: str) -> ExtractionFieldReview | None:
        return self.session.scalar(select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_type == target_type,
            ExtractionFieldReview.target_id == target_id,
            ExtractionFieldReview.field_name == field_name,
        ))

    def _upsert_review(self, paper_id: UUID, target_type: str, target_id: str, field_name: str) -> ExtractionFieldReview:
        review = self._find_review(paper_id, target_type, target_id, field_name)
        if review is None:
            review = ExtractionFieldReview(
                paper_id=paper_id, target_type=target_type, target_id=target_id,
                field_name=field_name, target_resolution_status="active", last_resolved_target_id=target_id,
            )
            self.session.add(review)
            self.session.flush()
        return review

    @staticmethod
    def _is_idempotent(review: ExtractionFieldReview | None, key: str) -> bool:
        payload = review.review_payload if review is not None and isinstance(review.review_payload, dict) else {}
        ai = payload.get("ai_verification") if isinstance(payload, dict) else None
        return isinstance(ai, dict) and ai.get("idempotency_key") == key

    @staticmethod
    def _write_conflict_reason(
        review: ExtractionFieldReview | None,
        submission: AIVerificationSubmission,
        current_fingerprint: str,
    ) -> str | None:
        if submission.expected_target_fingerprint != current_fingerprint:
            return "write_conflict:target_snapshot_stale"
        if review is not None:
            if submission.expected_write_version is None:
                return "write_conflict:review_version_required"
            if int(review.write_version or 1) != submission.expected_write_version:
                return "write_conflict:review_version_stale"
        return None

    @staticmethod
    def _idempotency_key(
        paper_id: UUID,
        canonical: str,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
    ) -> str:
        return stable_hash({
            "paper_id": str(paper_id), "target_type": canonical, "target_id": submission.target_id,
            "field_name": submission.field_name, "decision": submission.decision,
            "confidence": submission.confidence, "evidence_text": normalize_evidence_text(submission.evidence_text),
            "page": submission.page, "proposed_value": submission.proposed_value,
            "expected_target_fingerprint": submission.expected_target_fingerprint,
            "source_identity": identity.source_identity, "policy_version": AI_VERIFICATION_POLICY_VERSION,
        })

    @staticmethod
    def _require_identity(identity: AuthenticatedAIVerificationIdentity) -> None:
        if not identity.identity_verified or not identity.source_identity.strip():
            raise PermissionError("AI verification identity is not server-authenticated")
        if AI_VERIFICATION_CAPABILITY not in identity.capabilities:
            raise PermissionError(f"Missing capability: {AI_VERIFICATION_CAPABILITY}")

    def _add_audit(
        self,
        paper_id: UUID,
        canonical: str,
        submission: AIVerificationSubmission,
        identity: AuthenticatedAIVerificationIdentity,
        status: str,
        outcome: str,
        reasons: list[str],
        payload: dict[str, Any],
    ) -> None:
        self.session.add(AuditLog(
            paper_id=paper_id,
            action="single_ai_verification_decision",
            source=identity.source_identity,
            target_type=canonical,
            target_id=submission.target_id,
            payload={
                "actor_type": "ai", "source_identity": identity.source_identity,
                "source_label": identity.source_label, "model_agent": identity.model_agent,
                "capability": AI_VERIFICATION_CAPABILITY, "policy_version": AI_VERIFICATION_POLICY_VERSION,
                "field_name": submission.field_name, "status": status, "outcome": outcome,
                "blocked_reasons": reasons, "single_ai": True, "second_ai_used": False,
                **payload,
            },
        ))
