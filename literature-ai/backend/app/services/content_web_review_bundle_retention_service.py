from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    ContentWebReviewBundleV2,
    ContentWebReviewLocalVerificationResult,
    Paper,
)


SAFE_RETENTION_STATUSES = {"generated", "stale"}


class ContentWebReviewBundleRetentionService:
    """Delete only unused v2 packages that cannot carry audit outcomes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def cleanup(
        self,
        *,
        paper_id: UUID | None = None,
        older_than_days: int = 30,
        limit: int = 100,
        dry_run: bool = True,
        exclude_bundle_ids: set[UUID] | None = None,
        current_scope_fingerprints: dict[
            tuple[UUID, tuple[str, ...]], str
        ]
        | None = None,
    ) -> dict[str, Any]:
        if older_than_days < 0:
            raise ValueError("content_web_review_retention_older_than_days_must_be_nonnegative")
        if limit < 1:
            raise ValueError("content_web_review_retention_limit_must_be_positive")
        query = select(ContentWebReviewBundleV2)
        if paper_id is not None:
            query = query.where(ContentWebReviewBundleV2.paper_id == paper_id)
        bundles = list(
            self.session.scalars(
                query.order_by(
                    ContentWebReviewBundleV2.created_at.desc(),
                    ContentWebReviewBundleV2.id.desc(),
                ).limit(limit)
            ).all()
        )
        local_counts = self._local_result_counts(bundles)
        sql_null_proposal_ids = self._sql_null_proposal_ids(bundles)
        classification = self._classify(
            bundles,
            local_counts=local_counts,
            sql_null_proposal_ids=sql_null_proposal_ids,
            older_than_days=older_than_days,
            exclude_bundle_ids=exclude_bundle_ids or set(),
            current_scope_fingerprints=current_scope_fingerprints or {},
        )
        planned = [
            *classification["duplicate_ids"],
            *classification["expired_ids"],
        ]
        loaded_by_id = {bundle.id: bundle for bundle in bundles}
        actual_duplicate_ids: list[UUID] = []
        actual_expired_ids: list[UUID] = []
        protected_count = len(classification["protected_ids"])
        if dry_run:
            actual_duplicate_ids = list(classification["duplicate_ids"])
            actual_expired_ids = list(classification["expired_ids"])
        else:
            duplicate_set = set(classification["duplicate_ids"])
            for bundle_id in planned:
                result = self.session.execute(
                    delete(ContentWebReviewBundleV2)
                    .where(
                        ContentWebReviewBundleV2.id == bundle_id,
                        ContentWebReviewBundleV2.status.in_(SAFE_RETENTION_STATUSES),
                        ContentWebReviewBundleV2.proposal_payload.is_(None),
                        ~select(ContentWebReviewLocalVerificationResult.id)
                        .where(
                            ContentWebReviewLocalVerificationResult.bundle_id
                            == ContentWebReviewBundleV2.id
                        )
                        .exists(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 1:
                    loaded = loaded_by_id.get(bundle_id)
                    if loaded is not None and loaded in self.session:
                        self.session.expunge(loaded)
                    if bundle_id in duplicate_set:
                        actual_duplicate_ids.append(bundle_id)
                    else:
                        actual_expired_ids.append(bundle_id)
                else:
                    # The final DELETE predicate is authoritative.  If a
                    # concurrent proposal or local result appeared, treat the
                    # row as protected and leave it untouched.
                    protected_count += 1
        reported_ids = [*actual_duplicate_ids, *actual_expired_ids]
        return {
            "dry_run": dry_run,
            "scanned_count": len(bundles),
            "duplicate_deleted_count": len(actual_duplicate_ids),
            "expired_deleted_count": len(actual_expired_ids),
            "protected_count": protected_count,
            "deleted_bundle_ids": [str(bundle_id) for bundle_id in reported_ids],
        }

    def history(
        self,
        *,
        paper_id: UUID,
        module: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("content_web_review_history_limit_must_be_positive")
        bundles = list(
            self.session.scalars(
                select(ContentWebReviewBundleV2)
                .where(ContentWebReviewBundleV2.paper_id == paper_id)
                .order_by(
                    ContentWebReviewBundleV2.created_at.desc(),
                    ContentWebReviewBundleV2.id.desc(),
                )
            ).all()
        )
        if module is not None:
            bundles = [
                bundle
                for bundle in bundles
                if self._selected_modules(bundle) == [module]
            ]
        local_counts = self._local_result_counts(bundles)
        sql_null_proposal_ids = self._sql_null_proposal_ids(bundles)
        classification = self._classify(
            bundles,
            local_counts=local_counts,
            sql_null_proposal_ids=sql_null_proposal_ids,
            older_than_days=30,
            exclude_bundle_ids=set(),
            current_scope_fingerprints={},
        )
        reusable_ids = set(classification["reusable_ids"])
        cleanup_ids = {
            *classification["duplicate_ids"],
            *classification["expired_ids"],
        }
        protected_ids = set(classification["protected_ids"])
        items = [
            {
                "bundle_id": str(bundle.id),
                "status": bundle.status,
                "created_at": bundle.created_at,
                "updated_at": bundle.updated_at,
                "selected_modules": self._selected_modules(bundle),
                "bundle_fingerprint": bundle.snapshot_fingerprint,
                "has_proposal": bundle.id not in sql_null_proposal_ids,
                "local_result_count": local_counts.get(bundle.id, 0),
                "reusable": bundle.id in reusable_ids,
                "cleanup_eligible": bundle.id in cleanup_ids,
            }
            for bundle in bundles[:limit]
        ]
        return {
            "paper_id": str(paper_id),
            "module": module,
            "total_count": len(bundles),
            "reusable_count": len(reusable_ids),
            "protected_count": len(protected_ids),
            "cleanup_eligible_count": len(cleanup_ids),
            "estimated_manifest_bytes": sum(
                self._json_size(bundle.manifest) for bundle in bundles
            ),
            "estimated_proposal_bytes": sum(
                self._json_size(bundle.proposal_payload)
                for bundle in bundles
                if bundle.proposal_payload is not None
            ),
            "items": items,
            "storage_estimate_note": (
                "JSON content estimate only; not exact PostgreSQL disk usage."
            ),
        }

    def _classify(
        self,
        bundles: list[ContentWebReviewBundleV2],
        *,
        local_counts: dict[UUID, int],
        sql_null_proposal_ids: set[UUID],
        older_than_days: int,
        exclude_bundle_ids: set[UUID],
        current_scope_fingerprints: dict[
            tuple[UUID, tuple[str, ...]], str
        ],
    ) -> dict[str, list[UUID]]:
        safe: list[ContentWebReviewBundleV2] = []
        protected_ids: list[UUID] = []
        for bundle in bundles:
            if (
                bundle.status in SAFE_RETENTION_STATUSES
                and bundle.id in sql_null_proposal_ids
                and local_counts.get(bundle.id, 0) == 0
            ):
                safe.append(bundle)
            else:
                protected_ids.append(bundle.id)

        current_cache = dict(current_scope_fingerprints)
        groups: dict[
            tuple[UUID, str, tuple[str, ...], str],
            list[ContentWebReviewBundleV2],
        ] = defaultdict(list)
        for bundle in safe:
            groups[
                (
                    bundle.paper_id,
                    bundle.policy_version,
                    tuple(self._selected_modules(bundle)),
                    bundle.snapshot_fingerprint,
                )
            ].append(bundle)

        reusable_ids: list[UUID] = []
        duplicate_ids: list[UUID] = []
        for (
            group_paper_id,
            policy_version,
            modules,
            snapshot_fingerprint,
        ), rows in groups.items():
            current = self._current_fingerprint(
                group_paper_id,
                modules,
                current_cache,
            )
            reusable = [
                row
                for row in rows
                if row.status == "generated"
                and policy_version == self._policy_version()
                and current == snapshot_fingerprint
            ]
            if not reusable:
                continue
            reusable.sort(key=self._newest_key, reverse=True)
            excluded_reusable = [
                row for row in reusable if row.id in exclude_bundle_ids
            ]
            keeper = excluded_reusable[0] if excluded_reusable else reusable[0]
            reusable_ids.append(keeper.id)
            duplicate_ids.extend(
                row.id
                for row in rows
                if row.id != keeper.id and row.id not in exclude_bundle_ids
            )

        duplicate_set = set(duplicate_ids)
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            days=older_than_days
        )
        expired_ids: list[UUID] = []
        for bundle in safe:
            if (
                bundle.id in duplicate_set
                or bundle.id in exclude_bundle_ids
                or bundle.created_at is None
                or bundle.created_at > cutoff
            ):
                continue
            modules = tuple(self._selected_modules(bundle))
            current = self._current_fingerprint(
                bundle.paper_id,
                modules,
                current_cache,
            )
            if current is not None and current != bundle.snapshot_fingerprint:
                expired_ids.append(bundle.id)
        return {
            "reusable_ids": reusable_ids,
            "duplicate_ids": duplicate_ids,
            "expired_ids": expired_ids,
            "protected_ids": protected_ids,
        }

    def _current_fingerprint(
        self,
        paper_id: UUID,
        modules: tuple[str, ...],
        cache: dict[tuple[UUID, tuple[str, ...]], str],
    ) -> str | None:
        key = (paper_id, modules)
        if key in cache:
            return cache[key]
        if not modules:
            return None
        from app.services.content_web_review_bundle_v2_service import (
            ContentWebReviewBundleV2Service,
        )

        try:
            selected_modules = ContentWebReviewBundleV2Service._selected_modules(
                module=None,
                modules=list(modules),
            )
            paper = self.session.get(Paper, paper_id)
            if paper is None:
                return None
            manifest = ContentWebReviewBundleV2Service(self.session)._build_manifest(
                paper,
                selected_modules=selected_modules,
            )
        except (TypeError, ValueError):
            return None
        fingerprint = str(manifest.get("bundle_fingerprint") or "")
        if not fingerprint:
            return None
        cache[key] = fingerprint
        return fingerprint

    def _local_result_counts(
        self,
        bundles: list[ContentWebReviewBundleV2],
    ) -> dict[UUID, int]:
        ids = [bundle.id for bundle in bundles]
        if not ids:
            return {}
        return {
            bundle_id: int(count)
            for bundle_id, count in self.session.execute(
                select(
                    ContentWebReviewLocalVerificationResult.bundle_id,
                    func.count(ContentWebReviewLocalVerificationResult.id),
                )
                .where(
                    ContentWebReviewLocalVerificationResult.bundle_id.in_(ids)
                )
                .group_by(ContentWebReviewLocalVerificationResult.bundle_id)
            ).all()
        }

    def _sql_null_proposal_ids(
        self,
        bundles: list[ContentWebReviewBundleV2],
    ) -> set[UUID]:
        ids = [bundle.id for bundle in bundles]
        if not ids:
            return set()
        return set(
            self.session.scalars(
                select(ContentWebReviewBundleV2.id).where(
                    ContentWebReviewBundleV2.id.in_(ids),
                    ContentWebReviewBundleV2.proposal_payload.is_(None),
                )
            ).all()
        )

    @staticmethod
    def _selected_modules(bundle: ContentWebReviewBundleV2) -> list[str]:
        manifest = bundle.manifest if isinstance(bundle.manifest, dict) else {}
        modules = manifest.get("selected_modules")
        if not isinstance(modules, list):
            return []
        return sorted({str(value) for value in modules})

    @staticmethod
    def _policy_version() -> str:
        from app.services.content_web_review_bundle_v2_service import POLICY_VERSION

        return POLICY_VERSION

    @staticmethod
    def _newest_key(bundle: ContentWebReviewBundleV2) -> tuple[datetime, str]:
        return bundle.created_at or datetime.min, str(bundle.id)

    @staticmethod
    def _json_size(value: Any) -> int:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        )
