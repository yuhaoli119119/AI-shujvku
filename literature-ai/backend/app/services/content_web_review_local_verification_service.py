from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    ContentWebReviewBundleV2,
    ContentWebReviewLocalVerificationResult,
    EvidenceLocator,
    ExtractionFieldReview,
    ModuleWriteLock,
    Paper,
    PaperCorrection,
    utcnow,
)
from app.services.content_web_review_bundle_v2_service import (
    POLICY_VERSION,
    ContentWebReviewBundleV2Service,
)
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.review_service import ReviewService


OUTCOMES = {"CONFIRMED", "REVISED", "REJECTED", "NEEDS_HUMAN"}
ALLOWED_OUTCOMES_BY_DECISION = {
    # A local verifier may decline to decide, but it must never reinterpret a
    # validated web proposal into a different state transition.  In
    # particular, a web PASS cannot become a correction just because the
    # local verifier supplied a replacement value.
    "PASS": {"CONFIRMED", "NEEDS_HUMAN"},
    "REVISE": {"REVISED", "NEEDS_HUMAN"},
    "REJECT": {"REJECTED", "NEEDS_HUMAN"},
}
RESULT_FIELDS = {
    "plan_item_id",
    "object_snapshot_hash",
    "outcome",
    "checked_evidence_ids",
    "checked_pages",
    "verification_note",
    "verified_value",
}
PAGE_FIELDS = {"source_paper_id", "source_pdf_sha256", "page", "page_asset_sha256"}


class ContentWebReviewLocalVerificationError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class ContentWebReviewLocalVerificationService:
    """Apply only server-planned content checks under short per-object locks."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.bundle_service = ContentWebReviewBundleV2Service(session)

    def apply(
        self,
        *,
        bundle_id: UUID,
        results: list[dict[str, Any]],
        partial: bool,
        source_prefix: str,
        identity_verified: bool,
    ) -> dict[str, Any]:
        actor = str(source_prefix or "").strip()
        if not identity_verified or not actor:
            raise PermissionError("content_web_local_verification_identity_required")
        bundle = self._bundle(bundle_id)
        if not bundle.proposal_payload:
            raise ContentWebReviewLocalVerificationError("content_web_local_verification_proposal_required")
        if bundle.status not in {
            "web_proposal_validated",
            "awaiting_local_verification",
            "partial",
            "awaiting_human",
            "finalized",
            "stale",
        }:
            raise ContentWebReviewLocalVerificationError("content_web_local_verification_bundle_state_invalid")

        plan = self.bundle_service._local_plan(bundle)
        expected = {item["plan_item_id"]: item for item in plan["required_object_checks"]}
        normalized = self._validate_results(results, expected)
        existing_rows = list(self.session.scalars(
            select(ContentWebReviewLocalVerificationResult).where(
                ContentWebReviewLocalVerificationResult.bundle_id == bundle.id
            )
        ).all())
        existing = {str(row.plan_item_id): row for row in existing_rows}
        terminal_existing_plan_item_ids = {
            plan_item_id for plan_item_id, row in existing.items() if row.status != "failed"
        }
        for item in normalized:
            row = existing.get(item["plan_item_id"])
            if row is not None and row.payload_hash != item["payload_hash"]:
                raise ContentWebReviewLocalVerificationError(
                    "content_web_local_verification_idempotency_conflict",
                    item["plan_item_id"],
                )
        if not partial:
            covered = terminal_existing_plan_item_ids | {item["plan_item_id"] for item in normalized}
            missing = sorted(set(expected) - covered)
            if missing:
                raise ContentWebReviewLocalVerificationError(
                    "content_web_local_verification_incomplete_batch",
                    ",".join(missing),
                )

        selected_modules = list((bundle.manifest or {}).get("selected_modules") or [])
        preflight_manifest = self.bundle_service._build_manifest(
            self.bundle_service._paper(bundle.paper_id), selected_modules=selected_modules
        )
        preflight_targets = {item["plan_item_id"]: item for item in preflight_manifest.get("targets", [])}
        page_cache: dict[tuple[Any, ...], str | None] = {}
        metrics = {
            "logical_page_read_count": 0,
            "physical_page_read_attempt_count": 0,
            "page_read_retry_count": 0,
            "page_cache_hit_count": 0,
        }
        retrying_plan_item_ids = {
            item["plan_item_id"]
            for item in normalized
            if existing.get(item["plan_item_id"]) is not None
            and existing[item["plan_item_id"]].status == "failed"
        }
        actionable = [
            item
            for item in normalized
            if item["plan_item_id"] not in existing or item["plan_item_id"] in retrying_plan_item_ids
        ]
        for item in actionable:
            current = preflight_targets.get(item["plan_item_id"])
            if current is not None:
                self._read_page_asset_once(current, page_cache, metrics)
        metrics["logical_page_read_count"] = len(page_cache)

        response_rows: list[ContentWebReviewLocalVerificationResult] = []
        for item in normalized:
            prior = existing.get(item["plan_item_id"])
            if prior is not None and prior.status != "failed":
                response_rows.append(prior)
                continue
            target = expected[item["plan_item_id"]]
            current = preflight_targets.get(item["plan_item_id"])
            stale_reasons = self._dependency_reasons(
                bundle=bundle,
                expected=target,
                current=current,
                current_manifest=preflight_manifest,
                page_cache=page_cache,
            )
            if stale_reasons:
                row = self._store_result(
                    bundle=bundle,
                    target=target,
                    item=item,
                    actor=actor,
                    status="stale",
                    stale_reasons=stale_reasons,
                    error_code="content_web_local_verification_stale",
                    existing_row=prior,
                )
                response_rows.append(row)
                continue
            if item["outcome"] == "NEEDS_HUMAN":
                before = target.get("formal_gate_snapshot") or self._gate_snapshot(bundle, target)
                row = self._store_result(
                    bundle=bundle,
                    target=target,
                    item=item,
                    actor=actor,
                    status="awaiting_human",
                    formal_gate_before=before,
                    formal_gate_after=before,
                    existing_row=prior,
                )
                response_rows.append(row)
                continue

            lock: ModuleWriteLock | None = None
            try:
                module = self._module_for(target)
                conflict = self._active_lock_conflict(bundle.paper_id, module, actor)
                if conflict:
                    raise ContentWebReviewLocalVerificationError(
                        "module_write_lock_conflict",
                        f"{conflict.module_name}:{conflict.locked_by}",
                    )
                with self.session.begin_nested():
                    lock = ModuleWriteLockService(self.session).acquire(
                        paper_id=bundle.paper_id,
                        module_name=module,
                        locked_by=actor,
                        ttl_minutes=5,
                        meta={"bundle_id": str(bundle.id), "plan_item_id": item["plan_item_id"]},
                    )
                with self.session.begin_nested():
                    lock_manifest = self.bundle_service._build_manifest(
                        self.bundle_service._paper(bundle.paper_id), selected_modules=selected_modules
                    )
                    lock_target = next(
                        (row for row in lock_manifest.get("targets", []) if row["plan_item_id"] == item["plan_item_id"]),
                        None,
                    )
                    if lock_target is not None:
                        self._read_page_asset_once(lock_target, page_cache, metrics)
                    locked_stale = self._dependency_reasons(
                        bundle=bundle,
                        expected=target,
                        current=lock_target,
                        current_manifest=lock_manifest,
                        page_cache=page_cache,
                    )
                    if locked_stale:
                        raise ContentWebReviewLocalVerificationError(
                            "content_web_local_verification_locked_stale",
                            ",".join(locked_stale),
                        )
                    row = self._apply_one(
                        bundle, target, item, actor, lock.lock_token, existing_row=prior
                    )
                response_rows.append(row)
            except ContentWebReviewLocalVerificationError as exc:
                status = "stale" if exc.code == "content_web_local_verification_locked_stale" else "failed"
                reasons = (exc.detail or "").split(",") if status == "stale" and exc.detail else []
                row = self._store_result(
                    bundle=bundle,
                    target=target,
                    item=item,
                    actor=actor,
                    status=status,
                    stale_reasons=reasons,
                    error_code=exc.code,
                    existing_row=prior,
                )
                response_rows.append(row)
            except Exception as exc:
                row = self._store_result(
                    bundle=bundle,
                    target=target,
                    item=item,
                    actor=actor,
                    status="failed",
                    error_code=f"content_web_local_verification_apply_failed:{type(exc).__name__}",
                    existing_row=prior,
                )
                response_rows.append(row)
            finally:
                if lock is not None:
                    try:
                        with self.session.begin_nested():
                            ModuleWriteLockService(self.session).release(
                                lock_token=lock.lock_token,
                                released_by=actor,
                            )
                    except Exception:
                        active = self.session.get(ModuleWriteLock, lock.id)
                        if active is not None and active.status == "active":
                            active.status = "released"
                            active.released_at = utcnow()
                            self.session.add(active)
                            self.session.flush()

        metrics["logical_page_read_count"] = len(page_cache)
        bundle.manifest = {**(bundle.manifest or {}), "local_verification_metrics": metrics}
        self.session.add(bundle)
        self.session.flush()
        status = self.status(bundle.id)
        bundle.status = status["status"]
        self.session.add(bundle)
        self.session.flush()
        return {
            **status,
            "idempotent": not retrying_plan_item_ids and all(
                item["plan_item_id"] in terminal_existing_plan_item_ids for item in normalized
            ),
            "submitted_results": [self._serialize_result(row) for row in response_rows],
            "metrics": metrics,
        }

    def status(self, bundle_id: UUID) -> dict[str, Any]:
        bundle = self._bundle(bundle_id)
        if not bundle.proposal_payload:
            return {
                "bundle_id": str(bundle.id),
                "status": bundle.status,
                "object_counts": {"required": 0, "applied": 0, "pending": 0, "stale": 0, "failed": 0, "awaiting_human": 0},
                "results": [],
            }
        plan = self.bundle_service._local_plan(bundle)
        expected = {item["plan_item_id"]: item for item in plan["required_object_checks"]}
        rows = list(self.session.scalars(
            select(ContentWebReviewLocalVerificationResult).where(
                ContentWebReviewLocalVerificationResult.bundle_id == bundle.id
            ).order_by(ContentWebReviewLocalVerificationResult.created_at)
        ).all())
        counts = {
            "required": len(expected),
            "applied": sum(row.status == "applied" for row in rows),
            "stale": sum(row.status == "stale" for row in rows),
            "failed": sum(row.status == "failed" for row in rows),
            "awaiting_human": sum(row.status == "awaiting_human" for row in rows),
        }
        counts["pending"] = max(0, counts["required"] - len(rows))
        if counts["stale"]:
            status = "stale"
        elif counts["pending"] or counts["failed"]:
            status = "partial"
        elif counts["awaiting_human"]:
            status = "awaiting_human"
        else:
            status = "finalized"

        gate_rows: list[dict[str, Any]] = []
        seen_objects: set[tuple[str, str]] = set()
        before_counts = {"writing": 0, "citation": 0, "rag": 0}
        after_counts = {"writing": 0, "citation": 0, "rag": 0}
        for target in expected.values():
            key = (target["target_type"], target["target_id"])
            if key in seen_objects:
                continue
            seen_objects.add(key)
            before = target.get("formal_gate_snapshot") or {}
            after = self._gate_snapshot(bundle, target)
            self._increment_gate_counts(before_counts, before)
            self._increment_gate_counts(after_counts, after)
            gate_rows.append({
                "target_type": target["target_type"],
                "target_id": target["target_id"],
                "before": before,
                "after": after,
            })
        delta = {key: after_counts[key] - before_counts[key] for key in before_counts}
        return {
            "bundle_id": str(bundle.id),
            "status": status,
            "object_counts": counts,
            "pending_plan_item_ids": sorted(set(expected) - {str(row.plan_item_id) for row in rows}),
            "formal_content_object_gate_snapshot": gate_rows,
            "formal_eligibility_before": before_counts,
            "formal_eligibility_after": after_counts,
            "formal_eligibility_delta": delta,
            "metrics": dict((bundle.manifest or {}).get("local_verification_metrics") or {}),
            "results": [self._serialize_result(row) for row in rows],
        }

    def _validate_results(
        self,
        results: list[dict[str, Any]],
        expected: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(results, list) or not results:
            raise ContentWebReviewLocalVerificationError("content_web_local_verification_results_required")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in results:
            if not isinstance(raw, dict):
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_result_must_be_object")
            if set(raw) - RESULT_FIELDS:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_unknown_result_field")
            required = RESULT_FIELDS - {"verified_value"}
            if required - set(raw):
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_missing_result_field")
            plan_item_id = str(raw.get("plan_item_id") or "")
            if plan_item_id in seen:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_duplicate_plan_item", plan_item_id)
            seen.add(plan_item_id)
            target = expected.get(plan_item_id)
            if target is None:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_unknown_plan_item", plan_item_id)
            outcome = raw.get("outcome")
            if outcome not in OUTCOMES:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_invalid_outcome", plan_item_id)
            decision = target.get("decision")
            if outcome not in ALLOWED_OUTCOMES_BY_DECISION.get(decision, set()):
                raise ContentWebReviewLocalVerificationError(
                    "content_web_local_verification_outcome_not_allowed",
                    plan_item_id,
                )
            if raw.get("object_snapshot_hash") != target["object_snapshot_hash"]:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_wrong_object_hash", plan_item_id)
            evidence_ids = raw.get("checked_evidence_ids")
            if not isinstance(evidence_ids, list) or len(evidence_ids) != len(set(evidence_ids)):
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_invalid_evidence_ids", plan_item_id)
            if evidence_ids != [target["evidence_ref_id"]]:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_wrong_evidence_ids", plan_item_id)
            required_pages = []
            if target.get("page") is not None and target.get("requires_page_render"):
                required_pages = [{key: target[key] for key in PAGE_FIELDS}]
            checked_pages = raw.get("checked_pages")
            if not isinstance(checked_pages, list) or any(
                not isinstance(page, dict) or set(page) != PAGE_FIELDS for page in checked_pages
            ):
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_invalid_checked_pages", plan_item_id)
            page_keys = [tuple(page[key] for key in sorted(PAGE_FIELDS)) for page in checked_pages]
            if len(page_keys) != len(set(page_keys)) or checked_pages != required_pages:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_wrong_checked_pages", plan_item_id)
            note = str(raw.get("verification_note") or "").strip()
            if not note:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_note_required", plan_item_id)
            verified_value = raw.get("verified_value")
            if outcome == "REVISED" and "verified_value" not in raw:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_revised_value_required", plan_item_id)
            if outcome != "REVISED" and verified_value is not None:
                raise ContentWebReviewLocalVerificationError("content_web_local_verification_unexpected_verified_value", plan_item_id)
            if outcome == "REVISED" and verified_value != target.get("proposed_value"):
                raise ContentWebReviewLocalVerificationError(
                    "content_web_local_verification_revised_value_mismatch",
                    plan_item_id,
                )
            item = {
                "plan_item_id": plan_item_id,
                "object_snapshot_hash": raw["object_snapshot_hash"],
                "outcome": outcome,
                "checked_evidence_ids": evidence_ids,
                "checked_pages": checked_pages,
                "verification_note": note,
                "verified_value": verified_value,
            }
            item["payload_hash"] = self.bundle_service._hash(item)
            normalized.append(item)
        return normalized

    def _dependency_reasons(
        self,
        *,
        bundle: ContentWebReviewBundleV2,
        expected: dict[str, Any],
        current: dict[str, Any] | None,
        current_manifest: dict[str, Any],
        page_cache: dict[tuple[Any, ...], str | None],
    ) -> list[str]:
        reasons: list[str] = []
        manifest = bundle.manifest or {}
        if bundle.policy_version != POLICY_VERSION or manifest.get("policy_version") != POLICY_VERSION:
            reasons.append("policy_version_changed")
        if current is None:
            return sorted(set([*reasons, "target_missing"]))
        if current["object_snapshot_hash"] != expected["object_snapshot_hash"]:
            reasons.append("object_snapshot_changed")
        current_evidence = current["evidence"]
        if current_evidence["evidence_asset_sha256"] != expected["evidence_asset_sha256"]:
            reasons.append("evidence_asset_changed")
        if current_evidence["page_asset_sha256"] != expected["page_asset_sha256"]:
            reasons.append("page_asset_changed")
        if current_evidence.get("source_pdf_sha256") != expected.get("source_pdf_sha256"):
            reasons.append("source_pdf_changed")
        if current_evidence.get("page") is None:
            reasons.append("page_unresolved")
        if current_evidence.get("page_asset_status") not in {"materialized", "rendered_for_bundle"}:
            reasons.append("page_asset_unavailable")
        if current_evidence.get("page") is not None and expected.get("requires_page_render"):
            key = self._page_key(current_evidence)
            if page_cache.get(key) != current_evidence.get("page_asset_sha256"):
                reasons.append("page_asset_unavailable")
        expected_gate = self._effective_gate_baseline(bundle, expected)
        if expected_gate != current.get("formal_gate_snapshot"):
            reasons.append("review_gate_changed")
        return sorted(set(reasons))

    def _effective_gate_baseline(self, bundle: ContentWebReviewBundleV2, target: dict[str, Any]) -> dict[str, Any]:
        related = self.session.scalars(
            select(ContentWebReviewLocalVerificationResult).where(
                ContentWebReviewLocalVerificationResult.bundle_id == bundle.id,
                ContentWebReviewLocalVerificationResult.target_type == target["target_type"],
                ContentWebReviewLocalVerificationResult.target_id == target["target_id"],
                ContentWebReviewLocalVerificationResult.status == "applied",
            ).order_by(ContentWebReviewLocalVerificationResult.applied_at.desc())
        ).first()
        return dict(related.formal_gate_after) if related is not None and related.formal_gate_after else dict(target.get("formal_gate_snapshot") or {})

    def _apply_one(
        self,
        bundle: ContentWebReviewBundleV2,
        target: dict[str, Any],
        item: dict[str, Any],
        actor: str,
        lock_token: str,
        existing_row: ContentWebReviewLocalVerificationResult | None = None,
    ) -> ContentWebReviewLocalVerificationResult:
        canonical, record = self.bundle_service._resolve_formal_target(
            bundle.paper_id, target["target_type"], target["target_id"]
        )
        if record is None:
            raise ContentWebReviewLocalVerificationError("content_web_local_verification_target_missing")
        attribute = target["field_name"]
        current_value = getattr(record, attribute)
        if item["outcome"] == "REVISED" and item["verified_value"] == current_value:
            raise ContentWebReviewLocalVerificationError("content_web_local_verification_same_value_revision")
        before = self.bundle_service._formal_gate_snapshot(bundle.paper_id, target["target_type"], target["target_id"])
        evidence_payload = self._evidence_payload(bundle, target, item)
        review: ExtractionFieldReview | None = None
        locator: EvidenceLocator | None = None
        correction: PaperCorrection | None = None
        if item["outcome"] == "REVISED":
            collection = canonical
            correction = PaperCorrection(
                paper_id=bundle.paper_id,
                source=actor,
                field_name="abstract" if collection == "abstract" else collection,
                target_path=(
                    "abstract"
                    if collection == "abstract"
                    else f"{collection}:{target['target_id']}:{target['field_name']}"
                ),
                operation="replace",
                proposed_value=item["verified_value"],
                reason=item["verification_note"],
                evidence_payload=evidence_payload,
                status="pending",
            )
            self.session.add(correction)
            self.session.flush()
            correction = ReviewService(self.session).approve_correction(
                correction.id,
                reviewer=actor,
                write_lock_tokens=[lock_token],
                write_lock_owner=actor,
            )
            review, locator = self._review_and_locator(bundle.paper_id, canonical, target)
            if review is None or locator is None:
                raise ContentWebReviewLocalVerificationError(
                    "content_web_local_verification_canonical_review_missing"
                )
            # ReviewService's correction path must have materialized the
            # canonical records above. Re-upsert through the same locked seam
            # only to supersede any historical alias rows safely.
            review, locator = ReviewService(self.session).apply_content_verification_review(
                paper_id=bundle.paper_id,
                collection=canonical,
                target_id=target["target_id"],
                field_name=target["field_name"],
                original_value=current_value,
                reviewed_value=item["verified_value"],
                reviewer_status="verified",
                reviewer=actor,
                reviewer_note=item["verification_note"],
                evidence_payload=evidence_payload,
                write_lock_tokens=[lock_token],
                write_lock_owner=actor,
                audit_payload={"bundle_id": str(bundle.id), "plan_item_id": item["plan_item_id"], "outcome": "REVISED"},
            )
        else:
            status = "verified" if item["outcome"] == "CONFIRMED" else "rejected"
            review, locator = ReviewService(self.session).apply_content_verification_review(
                paper_id=bundle.paper_id,
                collection=canonical,
                target_id=target["target_id"],
                field_name=target["field_name"],
                original_value=current_value,
                reviewed_value=current_value,
                reviewer_status=status,
                reviewer=actor,
                reviewer_note=item["verification_note"],
                evidence_payload=evidence_payload,
                write_lock_tokens=[lock_token],
                write_lock_owner=actor,
                audit_payload={"bundle_id": str(bundle.id), "plan_item_id": item["plan_item_id"], "outcome": item["outcome"]},
            )
        self.session.flush()
        after = self.bundle_service._formal_gate_snapshot(bundle.paper_id, target["target_type"], target["target_id"])
        applied_value = getattr(record, attribute)
        applied_hash = self.bundle_service._hash({
            "target_type": target["target_type"],
            "target_id": target["target_id"],
            "field_name": target["field_name"],
            "value": applied_value,
        })
        return self._store_result(
            bundle=bundle,
            target=target,
            item=item,
            actor=actor,
            status="applied",
            formal_gate_before=before,
            formal_gate_after=after,
            correction_id=correction.id if correction else None,
            review_id=review.id if review else None,
            locator_id=locator.id if locator else None,
            applied_object_snapshot_hash=applied_hash,
            existing_row=existing_row,
        )

    def _review_and_locator(
        self, paper_id: UUID, canonical: str, target: dict[str, Any]
    ) -> tuple[ExtractionFieldReview | None, EvidenceLocator | None]:
        review = self.session.scalar(select(ExtractionFieldReview).where(
            ExtractionFieldReview.paper_id == paper_id,
            ExtractionFieldReview.target_type == canonical,
            ExtractionFieldReview.target_id == target["target_id"],
            ExtractionFieldReview.field_name == target["field_name"],
        ))
        locator = self.session.scalar(select(EvidenceLocator).where(
            EvidenceLocator.paper_id == paper_id,
            EvidenceLocator.target_type == canonical,
            EvidenceLocator.target_id == target["target_id"],
            EvidenceLocator.field_name == target["field_name"],
        ).order_by(EvidenceLocator.updated_at.desc()))
        return review, locator

    def _store_result(
        self,
        *,
        bundle: ContentWebReviewBundleV2,
        target: dict[str, Any],
        item: dict[str, Any],
        actor: str,
        status: str,
        stale_reasons: list[str] | None = None,
        error_code: str | None = None,
        formal_gate_before: dict[str, Any] | None = None,
        formal_gate_after: dict[str, Any] | None = None,
        correction_id: UUID | None = None,
        review_id: UUID | None = None,
        locator_id: UUID | None = None,
        applied_object_snapshot_hash: str | None = None,
        existing_row: ContentWebReviewLocalVerificationResult | None = None,
    ) -> ContentWebReviewLocalVerificationResult:
        row = existing_row or ContentWebReviewLocalVerificationResult(
            bundle_id=bundle.id,
            plan_item_id=UUID(item["plan_item_id"]),
        )
        row.payload_hash = item["payload_hash"]
        row.target_type = target["target_type"]
        row.target_id = target["target_id"]
        row.field_name = target["field_name"]
        row.object_snapshot_hash = item["object_snapshot_hash"]
        row.applied_object_snapshot_hash = applied_object_snapshot_hash
        row.outcome = item["outcome"]
        row.checked_evidence_ids = item["checked_evidence_ids"]
        row.checked_pages = item["checked_pages"]
        row.verification_note = item["verification_note"]
        row.verified_value = item["verified_value"]
        row.status = status
        row.stale_reasons = stale_reasons or []
        row.error_code = error_code
        row.formal_gate_before = formal_gate_before
        row.formal_gate_after = formal_gate_after
        row.correction_id = correction_id
        row.review_id = review_id
        row.locator_id = locator_id
        row.applied_by = actor
        row.applied_at = utcnow()
        self.session.add(row)
        self.session.add(AuditLog(
            paper_id=bundle.paper_id,
            action="apply_content_web_review_local_verification",
            source=actor,
            target_type=target["target_type"],
            target_id=target["target_id"],
            payload={
                "bundle_id": str(bundle.id),
                "plan_item_id": item["plan_item_id"],
                "outcome": item["outcome"],
                "status": status,
                "error_code": error_code,
                "stale_reasons": stale_reasons or [],
            },
        ))
        self.session.flush()
        return row

    def _evidence_payload(
        self, bundle: ContentWebReviewBundleV2, target: dict[str, Any], item: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "page": target["page"],
            "bbox": target.get("bbox"),
            "quoted_text": target["evidence_excerpt"],
            "source_pdf": target.get("page_asset_ref"),
            "source_pdf_sha256": target["source_pdf_sha256"],
            "page_asset_sha256": target["page_asset_sha256"],
            "evidence_asset_sha256": target["evidence_asset_sha256"],
            "evidence_ref_id": target["evidence_ref_id"],
            "local_verification": {
                "bundle_id": str(bundle.id),
                "plan_item_id": item["plan_item_id"],
                "checked_evidence_ids": item["checked_evidence_ids"],
                "checked_pages": item["checked_pages"],
            },
        }

    def _read_page_asset_once(
        self,
        current: dict[str, Any],
        cache: dict[tuple[Any, ...], str | None],
        metrics: dict[str, int],
    ) -> None:
        evidence = current["evidence"]
        requires_page_render = current.get("requires_page_render")
        if requires_page_render is None:
            requires_page_render = self.bundle_service._requires_page_render(current, evidence)
        if not requires_page_render:
            return
        if evidence.get("page") is None:
            return
        key = self._page_key(evidence)
        if key in cache:
            metrics["page_cache_hit_count"] += 1
            return
        metrics["physical_page_read_attempt_count"] += 1
        content = self.bundle_service._asset_bytes_for_evidence(evidence)
        cache[key] = hashlib.sha256(content).hexdigest() if content is not None else None

    @staticmethod
    def _page_key(evidence: dict[str, Any]) -> tuple[Any, ...]:
        return (
            evidence.get("source_paper_id"),
            evidence.get("source_pdf_sha256"),
            evidence.get("page"),
            evidence.get("page_asset_sha256"),
        )

    def _gate_snapshot(self, bundle: ContentWebReviewBundleV2, target: dict[str, Any]) -> dict[str, Any]:
        return self.bundle_service._formal_gate_snapshot(
            bundle.paper_id, target["target_type"], target["target_id"]
        )

    @staticmethod
    def _increment_gate_counts(counts: dict[str, int], gate: dict[str, Any]) -> None:
        writing = bool(gate.get("can_use_for_writing"))
        citation = bool(gate.get("can_use_for_citation"))
        counts["writing"] += int(writing)
        counts["citation"] += int(citation)
        counts["rag"] += int(writing or citation)

    @staticmethod
    def _module_for(target: dict[str, Any]) -> str:
        return {
            "paper_abstract": "metadata",
            "paper_section": "sections",
            "mechanism_claim": "mechanism_claims",
            "writing_card": "writing_cards",
        }[target["target_type"]]

    def _active_lock_conflict(self, paper_id: UUID, module: str, actor: str) -> ModuleWriteLock | None:
        locks = ModuleWriteLockService(self.session)._active_conflicts(
            paper_id=paper_id,
            module_name=module,
            now=utcnow(),
        )
        # Never renew/release a pre-existing lease, even when it happens to use
        # the same source_prefix. This tool owns only locks it creates itself.
        return locks[0] if locks else None

    def _bundle(self, bundle_id: UUID) -> ContentWebReviewBundleV2:
        bundle = self.session.get(ContentWebReviewBundleV2, bundle_id)
        if bundle is None:
            raise ContentWebReviewLocalVerificationError("content_web_review_v2_bundle_not_found")
        return bundle

    @staticmethod
    def _serialize_result(row: ContentWebReviewLocalVerificationResult) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "bundle_id": str(row.bundle_id),
            "plan_item_id": str(row.plan_item_id),
            "target_type": row.target_type,
            "target_id": row.target_id,
            "field_name": row.field_name,
            "payload_hash": row.payload_hash,
            "object_snapshot_hash": row.object_snapshot_hash,
            "applied_object_snapshot_hash": row.applied_object_snapshot_hash,
            "outcome": row.outcome,
            "checked_evidence_ids": row.checked_evidence_ids,
            "checked_pages": row.checked_pages,
            "verification_note": row.verification_note,
            "verified_value": row.verified_value,
            "status": row.status,
            "stale_reasons": row.stale_reasons,
            "error_code": row.error_code,
            "formal_gate_before": row.formal_gate_before,
            "formal_gate_after": row.formal_gate_after,
            "correction_id": str(row.correction_id) if row.correction_id else None,
            "review_id": str(row.review_id) if row.review_id else None,
            "locator_id": str(row.locator_id) if row.locator_id else None,
            "applied_by": row.applied_by,
            "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        }
