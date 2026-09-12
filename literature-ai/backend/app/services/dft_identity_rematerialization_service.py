"""Controlled, audited re-materialization of DFT identity v2 for an explicit allowlist.

Why this exists
---------------
Identity v2 (``subject_key`` / ``observation_key`` / ``identity_payload``) is the
platform's scientific-duplicate primitive.  Rows created before the v2 write path
existed carry ``identity_version = NULL`` and can therefore never collide with a
new extraction; and a change to the identity algorithm (for example teaching it a
context key that stored evidence already contains) leaves previously written keys
stale.

There is deliberately no incidental backfill: ``DFTAuditIssueLifecycleService
.identity_for_result`` is documented as read-only for legacy rows.  This module is
the *explicit* maintenance entry for the two legitimate cases above.  It never
invents data -- it only re-derives identity from the row and its stored evidence.

Guarantees enforced here
------------------------
* paper scope: every record must belong to the requested ``paper_id``;
* explicit allowlist: only ``dft_result_id`` values present in ``expectations``
  are touched;
* stale refusal: the caller must pass the exact current
  ``identity_version`` / ``subject_key`` / ``observation_key`` by value; any drift
  aborts the whole batch before a single row is written;
* collision refusal: a record whose new ``observation_key`` already exists on
  another row of the same paper (or twice inside the batch) is skipped, never
  merged and never overwritten;
* NULL-safety: a record whose identity is still incomplete (missing required
  identity fields) keeps ``observation_key = NULL`` and is reported, not faked;
* idempotency: a record already holding the target identity is reported
  ``idempotent`` and not written;
* audit: one ``audit_logs`` row per changed record with the before/after keys.
"""

from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DFTResult, AuditLog
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService


class DFTIdentityRematerializationService:
    """Re-derive and persist identity v2 for a bounded, explicitly named allowlist."""

    ACTOR = "b0102_identity_rematerialization"
    ACTION = "rematerialized_dft_identity_v2"

    def __init__(self, session: Session):
        self.session = session
        self.lifecycle = DFTAuditIssueLifecycleService(session)

    # ------------------------------------------------------------------ helpers
    def _load(self, paper_id: UUID, record_ids: Iterable[UUID]) -> list[DFTResult]:
        wanted = list(dict.fromkeys(UUID(str(value)) for value in record_ids))
        if not wanted:
            raise ValueError("record allowlist must not be empty")
        rows = list(
            self.session.scalars(
                select(DFTResult).where(
                    DFTResult.paper_id == paper_id,
                    DFTResult.id.in_(wanted),
                )
            ).all()
        )
        found = {row.id for row in rows}
        missing = [str(value) for value in wanted if value not in found]
        if missing:
            raise LookupError(f"dft_result_not_found_in_paper: {', '.join(missing)}")
        by_id = {row.id: row for row in rows}
        return [by_id[value] for value in wanted]

    def _compute(self, row: DFTResult):
        return self.lifecycle.build_identity(
            paper_id=row.paper_id,
            payload=self.lifecycle.authoritative_payload_for_result(row),
        )

    @staticmethod
    def _key_of(row: DFTResult) -> tuple[Any, str | None, str | None]:
        return (row.identity_version, row.subject_key, row.observation_key)

    def _existing_observation_keys(
        self, *, paper_id: UUID, exclude_ids: set[UUID]
    ) -> dict[str, str]:
        rows = self.session.scalars(
            select(DFTResult).where(
                DFTResult.paper_id == paper_id,
                DFTResult.identity_version == 2,
                DFTResult.observation_key.is_not(None),
            )
        ).all()
        return {
            str(row.observation_key): str(row.id)
            for row in rows
            if row.id not in exclude_ids
        }

    # -------------------------------------------------------------------- plan
    def plan(
        self, *, paper_id: UUID, record_ids: Iterable[UUID]
    ) -> dict[str, Any]:
        """Read-only: report the exact before/after identity for the allowlist."""

        rows = self._load(paper_id, record_ids)
        planned = []
        for row in rows:
            ident = self._compute(row)
            same = (
                row.identity_version == 2
                and row.subject_key == ident.subject_key
                and (row.observation_key or None) == (ident.observation_key or None)
            )
            if ident.observation_key is None:
                change = "left_null"
            elif same:
                change = "idempotent"
            else:
                change = "rekey"
            planned.append(
                {
                    "dft_result_id": str(row.id),
                    "current_identity_version": row.identity_version,
                    "current_subject_key": row.subject_key,
                    "current_observation_key": row.observation_key,
                    "new_subject_key": ident.subject_key,
                    "new_observation_key": ident.observation_key,
                    "errors": list(ident.error_codes),
                    "change": change,
                }
            )
        return {
            "paper_id": str(paper_id),
            "planned_count": len(planned),
            "changes": {
                "rekey": sum(1 for item in planned if item["change"] == "rekey"),
                "idempotent": sum(1 for item in planned if item["change"] == "idempotent"),
                "left_null": sum(1 for item in planned if item["change"] == "left_null"),
            },
            "records": planned,
        }

    # ------------------------------------------------------------------- apply
    def apply(
        self,
        *,
        paper_id: UUID,
        expectations: list[dict[str, Any]],
        reason: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply the re-materialization under the guarantees documented above."""

        if not str(reason or "").strip():
            raise ValueError("apply requires a non-empty reason")
        if not expectations:
            raise ValueError("apply requires a non-empty expectations list")

        rows = self._load(paper_id, [item["dft_result_id"] for item in expectations])
        row_by_id = {row.id: row for row in rows}

        # 1. stale refusal (whole batch, before any write).
        stale = []
        for item in expectations:
            row = row_by_id[UUID(str(item["dft_result_id"]))]
            expected = (
                item.get("expected_identity_version"),
                item.get("expected_subject_key"),
                item.get("expected_observation_key"),
            )
            if self._key_of(row) != expected:
                stale.append(
                    {
                        "dft_result_id": str(row.id),
                        "expected": [None if v is None else str(v) for v in expected],
                        "current": [None if v is None else str(v) for v in self._key_of(row)],
                    }
                )
        if stale:
            raise ValueError(f"stale_identity_snapshot: {len(stale)} record(s) drifted")

        # 2. compute targets, detect in-batch and cross-row collisions.
        computed = {row.id: self._compute(row_by_id[row.id]) for row in rows}
        batch_ids = set(row_by_id)
        occupied = self._existing_observation_keys(paper_id=paper_id, exclude_ids=batch_ids)
        key_holders: dict[str, list[UUID]] = {}
        for row in rows:
            key = computed[row.id].observation_key
            if key:
                key_holders.setdefault(key, []).append(row.id)
        collision_ids: set[UUID] = set()
        for row in rows:
            key = computed[row.id].observation_key
            if not key:
                continue
            if key in occupied or len(key_holders[key]) > 1:
                collision_ids.add(row.id)

        # 3. classify.
        to_write: list[DFTResult] = []
        results = []
        for row in rows:
            ident = computed[row.id]
            if row.id in collision_ids:
                state = "collision_refused"
            elif ident.observation_key is None:
                state = "left_null"
            elif self._key_of(row) == (2, ident.subject_key, ident.observation_key):
                state = "idempotent"
            else:
                state = "rekeyed"
                to_write.append(row)
            results.append(
                {
                    "dft_result_id": str(row.id),
                    "status": state,
                    "before": {
                        "identity_version": row.identity_version,
                        "subject_key": row.subject_key,
                        "observation_key": row.observation_key,
                    },
                    "after": {
                        "identity_version": 2 if state == "rekeyed" else row.identity_version,
                        "subject_key": ident.subject_key if state == "rekeyed" else row.subject_key,
                        "observation_key": ident.observation_key if state == "rekeyed" else row.observation_key,
                    },
                    "errors": list(ident.error_codes),
                }
            )

        if dry_run or not to_write:
            return {
                "status": "dry_run" if dry_run else "no_changes",
                "paper_id": str(paper_id),
                "written": 0,
                "results": results,
            }

        # 4. atomic swap: release keys first, then write.
        for row in to_write:
            self.lifecycle.clear_result_observation_key_for_rekey(row)
        self.session.flush()
        for row in to_write:
            self.lifecycle.apply_result_identity(row, computed[row.id])
            self.session.add(row)
        self.session.flush()

        # 5. audit.
        for row in to_write:
            before = next(item for item in results if item["dft_result_id"] == str(row.id))["before"]
            self.session.add(
                AuditLog(
                    paper_id=paper_id,
                    action=self.ACTION,
                    source=DFTIdentityRematerializationService.ACTOR,
                    target_type="dft_results",
                    target_id=str(row.id),
                    payload={
                        "reason": reason,
                        "before": before,
                        "after": {
                            "identity_version": row.identity_version,
                            "subject_key": row.subject_key,
                            "observation_key": row.observation_key,
                        },
                    },
                )
            )
        self.session.flush()

        return {
            "status": "applied",
            "paper_id": str(paper_id),
            "written": len(to_write),
            "results": results,
        }

    def readback(
        self, *, paper_id: UUID, record_ids: Iterable[UUID]
    ) -> dict[str, Any]:
        """Post-write verification: stored identity must equal recomputed identity."""

        rows = self._load(paper_id, record_ids)
        mismatch = []
        null_keys = []
        stale_key_computed_null = []
        for row in rows:
            ident = self._compute(row)
            if ident.observation_key is None:
                null_keys.append(str(row.id))
                if row.observation_key:
                    stale_key_computed_null.append(str(row.id))
                continue
            if (
                row.identity_version != 2
                or row.subject_key != ident.subject_key
                or (row.observation_key or None) != ident.observation_key
            ):
                mismatch.append(
                    {
                        "dft_result_id": str(row.id),
                        "stored": [row.identity_version, row.subject_key, row.observation_key],
                        "computed": [2, ident.subject_key, ident.observation_key],
                    }
                )
        return {
            "paper_id": str(paper_id),
            "checked": len(rows),
            "mismatch": mismatch,
            "null_observation_key": null_keys,
            "stored_key_but_computed_null": stale_key_computed_null,
            "ok": not mismatch and not stale_key_computed_null,
        }
