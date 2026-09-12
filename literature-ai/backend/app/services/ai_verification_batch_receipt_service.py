from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AIVerificationBatchReceipt, Paper
from app.schemas.ai_verification import (
    AIFieldVerificationApplyRequest,
    AIFieldVerificationSubmission,
)
from app.services.ai_verification_service import (
    AIVerificationService,
    AuthenticatedAIVerificationIdentity,
)
from app.utils.ai_verification import (
    ai_target_fingerprint,
    canonical_ai_target_type,
    get_ai_target,
    normalize_evidence_text,
    read_pdf_page_text,
)
from app.utils.review_safety import required_review_fields


class AIVerificationRequestConflict(ValueError):
    code = "request_id_content_conflict"


class AIVerificationBatchReceiptService:
    """Persist direct-apply idempotency receipts in the review write transaction.

    A receipt is inserted before field processing.  The unique database key,
    rather than process-local state, serializes duplicate requests.  The caller
    must commit the session before invoking ``attach_readback``.
    """

    def __init__(self, session: Session, verification: AIVerificationService | None = None) -> None:
        self.session = session
        self.verification = verification or AIVerificationService(session)

    @staticmethod
    def _request_fingerprint(submissions: list[dict[str, Any]]) -> str:
        encoded = json.dumps(
            submissions, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def apply(
        self,
        *,
        paper_id: UUID,
        request_id: str,
        submissions: list[dict[str, Any]],
        identity: AuthenticatedAIVerificationIdentity,
    ) -> tuple[dict[str, Any], bool]:
        request = AIFieldVerificationApplyRequest.model_validate({
            "request_id": request_id,
            "submissions": submissions,
        })
        request_id = request.request_id
        submissions = request.submissions
        if len(submissions) > self.verification.batch_limit:
            raise ValueError(f"AI verification batch exceeds limit {self.verification.batch_limit}")

        fingerprint = self._request_fingerprint(submissions)
        existing = self._find(paper_id, identity.source_identity, request_id)
        if existing is not None:
            return self._existing(existing, fingerprint), True

        receipt = AIVerificationBatchReceipt(
            paper_id=paper_id,
            source_identity=identity.source_identity,
            request_id=request_id,
            request_fingerprint=fingerprint,
            status="processing",
            receipt_payload=None,
        )
        try:
            # The savepoint lets a concurrent winner commit its receipt before
            # this transaction reads it, without poisoning the outer write.
            with self.session.begin_nested():
                self.session.add(receipt)
                self.session.flush()
        except IntegrityError:
            existing = self._find(paper_id, identity.source_identity, request_id)
            if existing is None:
                raise
            return self._existing(existing, fingerprint), True

        # Validate and canonicalize the whole request before obtaining a
        # transaction lock.  In particular, all transactions acquire their
        # multi-field locks in the same order; acquiring [A, B] while another
        # transaction holds B then waits for A would otherwise deadlock.
        item_results: list[dict[str, Any]] = []
        pending: list[tuple[int, AIFieldVerificationSubmission]] = []
        seen_fields: set[tuple[str, str, str]] = set()
        for item_index, raw in enumerate(submissions):
            try:
                submission = AIFieldVerificationSubmission.model_validate(raw)
                submission, field_key = self._canonical_direct_submission(submission)
                if field_key is not None and field_key in seen_fields:
                    item_results.append({
                        "item_index": item_index,
                        **self._no_write(
                            "duplicate_field_in_request",
                            "duplicate_target_id_and_field_name_in_same_request",
                            submission,
                        ),
                    })
                    continue
                if field_key is not None:
                    seen_fields.add(field_key)
                pending.append((item_index, submission))
            except ValidationError as exc:
                item_results.append({
                    "item_index": item_index,
                    "outcome": "format_error",
                    "status": "invalid",
                    "database_writes": False,
                    "errors": [
                        {
                            "field_path": ".".join(str(part) for part in error["loc"]),
                            "reason": error["msg"],
                            "type": error["type"],
                        }
                        for error in exc.errors()
                    ],
                })

        # This transaction-scoped PostgreSQL lock is required even when no
        # review row exists yet.  It serializes distinct request_ids through
        # both the state check and the eventual unique review-row insert.
        self._lock_direct_fields(paper_id, [submission for _index, submission in pending])
        for item_index, submission in pending:
            try:
                preflight_error = self._direct_preflight(paper_id, submission)
                if preflight_error is not None:
                    item_results.append({"item_index": item_index, **preflight_error})
                    continue
                processed = self.verification.process_batch(
                    paper_id=paper_id,
                    submissions=[submission],
                    identity=identity,
                    dry_run=False,
                    commit=False,
                )
                item_results.append({"item_index": item_index, **processed["items"][0]})
            except ValidationError as exc:  # Defensive: canonical data was already validated above.
                item_results.append({"item_index": item_index, "outcome": "format_error", "status": "invalid", "database_writes": False, "errors": [{"field_path": ".".join(str(part) for part in error["loc"]), "reason": error["msg"], "type": error["type"]} for error in exc.errors()]})

        item_results.sort(key=lambda item: int(item["item_index"]))
        payload = {
            "schema_version": "ai_verification_batch_receipt.v2",
            "request_id": request_id,
            "paper_id": str(paper_id),
            "authenticated_identity": identity.source_identity,
            "submitted_at": datetime.now(UTC).isoformat(),
            "committed": True,
            # This immutable submission outcome is deliberately separate from
            # later readback.  It intentionally contains just the recovery
            # fields rather than a second copy of the submitted package.
            "items": [self._compact_item(item, submissions[item["item_index"]]) for item in item_results],
            "counts": self._counts(item_results),
            "current_readback": {"status": "pending_commit_readback", "items": []},
        }
        receipt.status = "committed"
        receipt.receipt_payload = payload
        receipt.committed_at = datetime.now(UTC)
        self.session.add(receipt)
        self.session.flush()
        return payload, False

    @staticmethod
    def _is_valid_submission(raw: dict[str, Any]) -> bool:
        try:
            AIFieldVerificationSubmission.model_validate(raw)
            return True
        except ValidationError:
            return False

    def _direct_preflight(
        self, paper_id: UUID, submission: AIFieldVerificationSubmission,
    ) -> dict[str, Any] | None:
        """Reject direct-protocol scope/staleness before generic write paths."""
        try:
            canonical, target = get_ai_target(
                self.session, paper_id=paper_id, target_type=submission.target_type, target_id=submission.target_id,
            )
        except Exception as exc:
            return self._no_write("invalid_target", type(exc).__name__)
        if canonical != "dft_results":
            return self._no_write("invalid_target_type", "apply_ai_verification_batch_only_supports_dft_results")
        if submission.field_name not in required_review_fields(canonical, target):
            return self._no_write("invalid_field", "dft_field_not_required")
        existing = self.verification._find_review(paper_id, canonical, submission.target_id, submission.field_name)
        terminal_status = self.verification.dft_field_terminal_status(
            paper_id=paper_id, target_type=canonical, target=target,
            field_name=submission.field_name, review=existing,
        )
        if terminal_status is not None:
            return {
                "target_type": canonical, "target_id": submission.target_id, "field_name": submission.field_name,
                "outcome": "skipped_terminal", "status": terminal_status,
                "blocked_reasons": ["existing_terminal_review_preserved"], "database_writes": False,
            }
        current_fingerprint = ai_target_fingerprint(canonical, target)
        if submission.expected_target_fingerprint != current_fingerprint:
            return self._no_write("write_conflict", "write_conflict:target_snapshot_stale", submission)
        if existing is not None and (
            submission.expected_write_version is None
            or int(existing.write_version or 1) != submission.expected_write_version
        ):
            return self._no_write("write_conflict", "write_conflict:review_version_stale", submission)
        if submission.decision == "reject":
            return self._reject_preflight(paper_id, submission)
        return None

    @staticmethod
    def _compact_item(item: dict[str, Any], raw_submission: dict[str, Any]) -> dict[str, Any]:
        """Keep one compact, immutable result per requested field."""
        evidence_location = item.get("evidence_location") if isinstance(item.get("evidence_location"), dict) else {}
        return {
            "item_index": item["item_index"],
            "record_id": item.get("target_id"),
            "target_type": item.get("target_type", "dft_results"),
            "field_name": item.get("field_name"),
            "requested_decision": raw_submission.get("decision"),
            "outcome": item.get("outcome"),
            "status": item.get("status"),
            "blocked_reasons": item.get("blocked_reasons", []),
            "errors": item.get("errors", []),
            "evidence_location": {
                "source_paper_id": item.get("source_paper_id") or evidence_location.get("source_paper_id"),
                "page": evidence_location.get("page", raw_submission.get("page")),
                "table_id": evidence_location.get("table_id", raw_submission.get("table_id")),
                "source_row_index": evidence_location.get("source_row_index", raw_submission.get("source_row_index")),
                "source_column_index": evidence_location.get("source_column_index", raw_submission.get("source_column_index")),
            },
            "target_snapshot_fingerprint": item.get("target_snapshot_fingerprint"),
            "database_writes": bool(item.get("database_writes")),
        }

    @staticmethod
    def _canonical_direct_submission(
        submission: AIFieldVerificationSubmission,
    ) -> tuple[AIFieldVerificationSubmission, tuple[str, str, str] | None]:
        """Return a canonical DFT submission and its logical lock identity.

        Invalid/non-DFT inputs are intentionally left for per-item preflight
        reporting.  UUID spelling variants must not produce different locks or
        different review-row identities for the same target.
        """
        try:
            target_type = canonical_ai_target_type(submission.target_type)
            target_id = str(UUID(str(submission.target_id)))
        except (TypeError, ValueError):
            return submission, None
        normalized = submission.model_copy(update={"target_type": target_type, "target_id": target_id})
        if target_type != "dft_results":
            return normalized, None
        return normalized, (target_type, target_id, normalized.field_name.strip().casefold())

    def _lock_direct_fields(
        self, paper_id: UUID, submissions: list[AIFieldVerificationSubmission],
    ) -> None:
        """Take every logical DFT-field lock in a deterministic batch order."""
        keys = {
            field_key
            for submission in submissions
            for _normalized, field_key in (self._canonical_direct_submission(submission),)
            if field_key is not None
        }
        for target_type, target_id, field_name in sorted(keys):
            self._lock_direct_field(paper_id, target_type, target_id, field_name)

    def _lock_direct_field(self, paper_id: UUID, target_type: str, target_id: str, field_name: str) -> None:
        """Serialize one logical direct-apply field, including first insert.

        ``extraction_field_reviews`` has a uniqueness constraint, but it alone
        cannot make the preflight status check and subsequent mutation atomic
        when the row is absent.  PostgreSQL advisory transaction locks give
        every direct request for the same logical field the same critical
        section without introducing a placeholder review row.
        """
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        digest = hashlib.sha256(
            f"direct-ai-field-v1:{paper_id}:{target_type}:{target_id}:{field_name}".encode("utf-8")
        ).digest()
        lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key},
        )

    @staticmethod
    def _no_write(outcome: str, reason: str, submission: AIFieldVerificationSubmission | None = None) -> dict[str, Any]:
        return {
            "target_type": "dft_results" if submission is None else submission.target_type,
            "target_id": None if submission is None else submission.target_id,
            "field_name": None if submission is None else submission.field_name,
            "outcome": outcome, "status": "pending", "blocked_reasons": [reason], "database_writes": False,
        }

    def _reject_preflight(self, paper_id: UUID, submission: AIFieldVerificationSubmission) -> dict[str, Any] | None:
        if submission.page is None or not submission.evidence_text.strip():
            return self._no_write("reject_evidence_required", "reject_requires_page_and_locator_text", submission)
        context, error = self.verification._validate_defer_evidence_context(paper_id, submission)
        if error is not None:
            return self._no_write("reject_evidence_required", error, submission)
        # Resolve via the same source paper selected by the associated-SI gate.
        evidence_paper = self.session.get(Paper, context["evidence_paper_id"])
        if evidence_paper is None:
            return self._no_write("reject_evidence_required", "missing_evidence_paper", submission)
        page_text, read_error, _path = read_pdf_page_text(evidence_paper, submission.page)
        if read_error is not None or normalize_evidence_text(submission.counter_evidence_text) not in normalize_evidence_text(page_text or ""):
            return self._no_write("reject_evidence_required", "counter_evidence_not_on_source_page", submission)
        return None

    @staticmethod
    def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "submitted": len(items),
            "format_errors": sum(item.get("outcome") == "format_error" for item in items),
            "applied": sum(bool(item.get("database_writes")) for item in items),
            "skipped": sum(not bool(item.get("database_writes")) and item.get("outcome") != "format_error" for item in items),
            "accepted": sum(item.get("outcome") in {"auto_verified", "auto_repaired"} for item in items),
            "deferred": sum(item.get("outcome") == "auto_deferred" for item in items),
            "rejected": sum(item.get("outcome") == "auto_rejected" for item in items),
        }

    def _find(self, paper_id: UUID, source_identity: str, request_id: str) -> AIVerificationBatchReceipt | None:
        return self.session.scalar(
            select(AIVerificationBatchReceipt).where(
                AIVerificationBatchReceipt.paper_id == paper_id,
                AIVerificationBatchReceipt.source_identity == source_identity,
                AIVerificationBatchReceipt.request_id == request_id,
            )
        )

    @staticmethod
    def _existing(receipt: AIVerificationBatchReceipt, fingerprint: str) -> dict[str, Any]:
        if receipt.request_fingerprint != fingerprint:
            raise AIVerificationRequestConflict(
                "request_id_content_conflict: request_id was already used with different submissions"
            )
        payload = deepcopy(receipt.receipt_payload or {})
        payload["replayed"] = True
        return payload

    def get(self, *, paper_id: UUID, request_id: str, identity: AuthenticatedAIVerificationIdentity) -> dict[str, Any]:
        payload = self.attach_readback(paper_id=paper_id, request_id=request_id, identity=identity)
        payload["replayed"] = True
        return payload

    def attach_readback(self, *, paper_id: UUID, request_id: str, identity: AuthenticatedAIVerificationIdentity) -> dict[str, Any]:
        receipt = self._find(paper_id, identity.source_identity, request_id)
        if receipt is None:
            raise LookupError("ai_verification_receipt_not_found")
        payload = deepcopy(receipt.receipt_payload or {})
        readback_items: list[dict[str, Any]] = []
        for item in payload.get("items", []):
            if item.get("outcome") == "format_error":
                continue
            target_id, field_name = item.get("record_id") or item.get("target_id"), item.get("field_name")
            if not target_id or not field_name:
                continue
            current: dict[str, Any] = {
                "item_index": item["item_index"],
                "record_id": str(target_id),
                "field_name": str(field_name),
                "requested_decision": item.get("requested_decision"),
            }
            try:
                canonical, target = get_ai_target(
                    self.session, paper_id=paper_id, target_type=str(item["target_type"]), target_id=str(target_id),
                )
                review = self.verification._find_review(paper_id, canonical, str(target_id), str(field_name))
                current["target_type"] = canonical
                current["target_fingerprint"] = ai_target_fingerprint(canonical, target)
                current["review_status"] = review.reviewer_status if review is not None else "no_review_row"
                current["review_write_version"] = int(review.write_version) if review is not None else None
                # A direct write and a terminal-skip both claim that a
                # canonical review row exists.  Neither may be confirmed from
                # the original payload if that authoritative row disappeared.
                requires_review = bool(item.get("database_writes")) or item.get("outcome") == "skipped_terminal"
                current["confirmed"] = review is not None if requires_review else True
            except Exception as exc:
                current["target_readback_error"] = type(exc).__name__
                current["confirmed"] = False
            readback_items.append(current)
        confirmed = bool(readback_items) and all(item.get("confirmed") is True for item in readback_items)
        current_readback = {
            "status": "confirmed" if confirmed else "not_confirmed",
            "read_at": datetime.now(UTC).isoformat(),
            "items": readback_items,
        }
        payload["current_readback"] = current_readback
        receipt.receipt_payload = payload
        self.session.add(receipt)
        self.session.flush()
        return payload
