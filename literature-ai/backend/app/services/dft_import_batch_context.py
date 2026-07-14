from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.db.models import DFTAuditIssue, ExternalAnalysisCandidate


IssueIdentityKey = tuple[str, str, str, str]
IssueFingerprintKey = tuple[str, str]
SourceRelationKey = tuple[UUID, UUID]


@dataclass
class DFTImportSavepointOverlay:
    issues_by_identity: dict[IssueIdentityKey, DFTAuditIssue] = field(default_factory=dict)
    issues_by_fingerprint: dict[IssueFingerprintKey, DFTAuditIssue] = field(default_factory=dict)
    owned_issue_ids: set[UUID] = field(default_factory=set)
    source_relations: set[SourceRelationKey] = field(default_factory=set)


@dataclass
class DFTImportBatchContext:
    """Transaction-local preload state for one bounded DFT import.

    Database rows loaded before the row loop live in the base indexes. Objects
    created inside a row savepoint live only in ``overlay`` until that savepoint
    exits successfully. This keeps a rolled-back ORM object out of later rows.
    """

    paper_id: UUID
    locked_candidate_ids: set[UUID] = field(default_factory=set)
    candidates_by_id: dict[UUID, ExternalAnalysisCandidate] = field(default_factory=dict)
    candidate_snapshots: dict[UUID, str] = field(default_factory=dict)
    locked_issue_ids: set[UUID] = field(default_factory=set)
    owned_issue_ids: set[UUID] = field(default_factory=set)
    issues_by_identity: dict[IssueIdentityKey, DFTAuditIssue] = field(default_factory=dict)
    issues_by_fingerprint: dict[IssueFingerprintKey, DFTAuditIssue] = field(default_factory=dict)
    source_relations: set[SourceRelationKey] = field(default_factory=set)
    overlay: DFTImportSavepointOverlay | None = None

    def begin_savepoint(self) -> None:
        if self.overlay is not None:
            raise RuntimeError("dft_import_savepoint_overlay_already_active")
        self.overlay = DFTImportSavepointOverlay()

    def commit_savepoint(self) -> None:
        overlay = self._require_overlay()
        self.issues_by_identity.update(overlay.issues_by_identity)
        self.issues_by_fingerprint.update(overlay.issues_by_fingerprint)
        self.owned_issue_ids.update(overlay.owned_issue_ids)
        self.source_relations.update(overlay.source_relations)
        self.overlay = None

    def rollback_savepoint(self) -> None:
        self.overlay = None

    def issue_by_identity(self, key: IssueIdentityKey) -> DFTAuditIssue | None:
        if self.overlay is not None and key in self.overlay.issues_by_identity:
            return self.overlay.issues_by_identity[key]
        return self.issues_by_identity.get(key)

    def issue_by_fingerprint(self, key: IssueFingerprintKey) -> DFTAuditIssue | None:
        if self.overlay is not None and key in self.overlay.issues_by_fingerprint:
            return self.overlay.issues_by_fingerprint[key]
        return self.issues_by_fingerprint.get(key)

    def register_issue(
        self,
        *,
        identity_key: IssueIdentityKey,
        fingerprint_key: IssueFingerprintKey,
        issue: DFTAuditIssue,
        owned: bool,
    ) -> None:
        overlay = self._require_overlay()
        overlay.issues_by_identity[identity_key] = issue
        overlay.issues_by_fingerprint[fingerprint_key] = issue
        if owned:
            overlay.owned_issue_ids.add(issue.id)

    def issue_lock_is_held(self, issue_id: UUID) -> bool:
        return (
            issue_id in self.locked_issue_ids
            or issue_id in self.owned_issue_ids
            or (self.overlay is not None and issue_id in self.overlay.owned_issue_ids)
        )

    def source_relation_exists(self, key: SourceRelationKey) -> bool:
        return key in self.source_relations or (
            self.overlay is not None and key in self.overlay.source_relations
        )

    def register_source_relation(self, key: SourceRelationKey) -> None:
        self._require_overlay().source_relations.add(key)

    def _require_overlay(self) -> DFTImportSavepointOverlay:
        if self.overlay is None:
            raise RuntimeError("dft_import_savepoint_overlay_not_active")
        return self.overlay
