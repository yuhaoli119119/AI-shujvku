from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    Paper,
    PaperRelationship,
)
from app.migrations.dft_identity_v2 import MIGRATION_VERSION
from app.services.dft_audit_issue_lifecycle_service import (
    DFT_AUDIT_ISSUE_PENDING_STATUSES,
    DFTAuditIssueLifecycleService,
)
from app.utils.review_safety import bulk_export_gate_results


CANONICAL_MANIFEST_VERSION = "dft_identity_v2_dry_run_v1"

B0102_EXPECTED = {
    "before_dft_total": 375,
    "before_ai_verified_ml_ready": 373,
    "before_rejected": 2,
    "open_missing_dft_result": 368,
    "safe_single_target": 366,
    "identity_split_parent_issues": 2,
    "missing_li2_results": 2,
    "unmapped": 0,
    "unknown_multi_target": 0,
}

B0102_SPLIT_EXPECTATIONS = (
    {
        "issue_id": "23fdcb8b-31d9-4b78-9d3a-a374be9a3050",
        "old_result_id": "fe4f3e50-5aa1-4e67-b99f-80c09158200c",
        "li1_candidate_id": "3111d2e3-2f30-4c25-a94c-8c347db6f58f",
        "li2_candidate_id": "1095771c-43b5-4da9-a7e1-e09b09d4ef71",
        "material": "Graphene@Li2S",
        "property_type": "bond_length",
        "value": "2.11",
        "unit": "Å",
    },
    {
        "issue_id": "c3eeb0be-d023-4ae7-9de7-18ada01e81d2",
        "old_result_id": "90e5f450-6900-4b1f-86a0-3e7579e1da36",
        "li1_candidate_id": "8002c4b1-22b3-42bf-a958-e32a53dd3bc4",
        "li2_candidate_id": "b63df0cb-7e34-4bab-a6cc-9aca3e23e151",
        "material": "FeN4-G@Li2S",
        "property_type": "bond_length",
        "value": "2.18",
        "unit": "Å",
    },
)


class DFTIdentityDryRunError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DFTIdentityDryRunService:
    """Build a deterministic Identity v2 migration manifest without writes.

    The caller must start a PostgreSQL transaction and force it READ ONLY before
    constructing this service. Identity derivation delegates to the existing
    lifecycle service so this module never owns a second scientific identity
    algorithm.
    """

    def __init__(self, session: Session):
        self.session = session
        self.lifecycle = DFTAuditIssueLifecycleService(session)

    def run(
        self,
        *,
        paper_code: str | None,
        data_root: Path | None,
        backup_path: Path,
        expected_backup_sha256: str | None = None,
    ) -> dict[str, Any]:
        self._assert_read_only_transaction()
        self._assert_clean_session("before_dry_run")
        self._assert_identity_schema()

        backup = backup_path.resolve(strict=True)
        backup_hash = file_sha256(backup)
        if expected_backup_sha256 and backup_hash.casefold() != expected_backup_sha256.casefold():
            raise DFTIdentityDryRunError(
                f"backup_sha256_mismatch:{backup_hash}"
            )

        database_before = self.database_data_fingerprint()
        pdf_before = (
            self.pdf_snapshot(paper_code=paper_code, data_root=data_root)
            if paper_code
            else None
        )

        results, result_identities, global_classification = self._global_result_identity_analysis()
        candidate_analysis = self._candidate_issue_analysis()
        detailed = None
        if paper_code:
            detailed = self._paper_reconciliation(
                paper_code=paper_code,
                results=results,
                result_identities=result_identities,
                candidate_analysis=candidate_analysis,
            )

        self._assert_clean_session("after_analysis")
        database_after = self.database_data_fingerprint()
        pdf_after = (
            self.pdf_snapshot(paper_code=paper_code, data_root=data_root)
            if paper_code
            else None
        )
        self._assert_clean_session("after_fingerprints")

        if database_before["sha256"] != database_after["sha256"]:
            raise DFTIdentityDryRunError("database_data_fingerprint_changed")
        if pdf_before is not None and pdf_before["sha256"] != pdf_after["sha256"]:
            raise DFTIdentityDryRunError("pdf_snapshot_fingerprint_changed")

        payload = {
            "manifest_version": CANONICAL_MANIFEST_VERSION,
            "mode": "dry_run",
            "write_capability": False,
            "transaction_read_only": True,
            "backup": {
                "path": str(backup),
                "size": backup.stat().st_size,
                "sha256": backup_hash.upper(),
            },
            "schema": {
                "migration_head": MIGRATION_VERSION,
                "schema_name": "public",
            },
            "database_data_fingerprint": {
                "before": database_before,
                "after": database_after,
                "equal": True,
            },
            "pdf_snapshot_fingerprint": (
                {
                    "before": pdf_before,
                    "after": pdf_after,
                    "equal": True,
                }
                if pdf_before is not None
                else None
            ),
            "global_identity_analysis": global_classification,
            "new_candidate_analysis": candidate_analysis,
            "paper_reconciliation": detailed,
            "preconditions_for_apply": {
                "database_data_fingerprint": database_before["sha256"],
                "pdf_snapshot_fingerprint": pdf_before["sha256"] if pdf_before else None,
                "paper_code": paper_code,
            },
            "dry_run_database_writes": 0,
        }
        return {
            "canonical_payload": payload,
            "canonical_sha256": canonical_sha256(payload),
        }

    def _assert_read_only_transaction(self) -> None:
        if str(self.session.scalar(text("SHOW transaction_read_only"))).casefold() != "on":
            raise DFTIdentityDryRunError("postgres_transaction_must_be_read_only")

    def _assert_clean_session(self, stage: str) -> None:
        dirty = sorted(
            {
                f"{type(row).__name__}:{getattr(row, 'id', '<new>')}"
                for row in (*self.session.new, *self.session.dirty, *self.session.deleted)
            }
        )
        if dirty:
            raise DFTIdentityDryRunError(
                f"orm_write_state_detected:{stage}:{','.join(dirty)}"
            )

    def _assert_identity_schema(self) -> None:
        required = {
            "dft_results": {
                "identity_version",
                "subject_key",
                "observation_key",
                "identity_payload",
            },
            "dft_audit_issues": {
                "result_id",
                "issue_key_version",
                "issue_key",
                "lifecycle_version",
                "lifecycle_stage",
                "resolution_code",
                "parent_issue_id",
                "last_error_code",
                "retry_count",
                "next_retry_at",
            },
        }
        rows = self.session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name IN "
                "('dft_results','dft_audit_issues','dft_audit_issue_sources')"
            )
        ).all()
        columns: dict[str, set[str]] = defaultdict(set)
        for table_name, column_name in rows:
            columns[str(table_name)].add(str(column_name))
        missing = {
            table_name: sorted(expected - columns.get(table_name, set()))
            for table_name, expected in required.items()
            if expected - columns.get(table_name, set())
        }
        if "dft_audit_issue_sources" not in columns:
            missing["dft_audit_issue_sources"] = ["<table>"]
        if missing:
            raise DFTIdentityDryRunError(f"identity_v2_migration_missing:{canonical_json(missing)}")

    def database_data_fingerprint(self) -> dict[str, Any]:
        """Fingerprint every base table in public while excluding sequences/stats."""

        table_names = list(
            self.session.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE' "
                    "ORDER BY table_name COLLATE \"C\""
                )
            ).all()
        )
        bind = self.session.get_bind()
        quote = bind.dialect.identifier_preparer.quote
        tables: list[dict[str, Any]] = []
        total_rows = 0
        for raw_name in table_names:
            table_name = str(raw_name)
            sql = text(f"SELECT to_jsonb(t)::text FROM public.{quote(table_name)} AS t")
            rows = sorted(str(value) for value in self.session.scalars(sql).all())
            digest = hashlib.sha256()
            for row_json in rows:
                encoded = row_json.encode("utf-8")
                digest.update(str(len(encoded)).encode("ascii"))
                digest.update(b":")
                digest.update(encoded)
                digest.update(b"\n")
            total_rows += len(rows)
            tables.append(
                {
                    "table": table_name,
                    "row_count": len(rows),
                    "sha256": digest.hexdigest(),
                }
            )
        return {
            "algorithm": "public-base-tables-to_jsonb-rowset-v1",
            "table_count": len(tables),
            "row_count": total_rows,
            "tables": tables,
            "sha256": canonical_sha256(tables),
        }

    def pdf_snapshot(self, *, paper_code: str, data_root: Path | None) -> dict[str, Any]:
        if data_root is None:
            raise DFTIdentityDryRunError("data_root_required_for_pdf_fingerprint")
        root = data_root.resolve(strict=True)
        paper = self.session.scalar(select(Paper).where(Paper.paper_code == paper_code))
        if paper is None:
            raise DFTIdentityDryRunError(f"paper_code_not_found:{paper_code}")
        relationships = list(
            self.session.scalars(
                select(PaperRelationship).where(
                    (PaperRelationship.source_paper_id == paper.id)
                    | (PaperRelationship.target_paper_id == paper.id)
                )
            ).all()
        )
        linked_ids: set[UUID] = set()
        for relation in relationships:
            relation_type = str(relation.relationship_type or "").casefold()
            if "supplement" not in relation_type:
                continue
            linked_ids.add(
                relation.target_paper_id
                if relation.source_paper_id == paper.id
                else relation.source_paper_id
            )
        linked = (
            list(self.session.scalars(select(Paper).where(Paper.id.in_(linked_ids))).all())
            if linked_ids
            else []
        )
        if len(linked) != len(linked_ids):
            raise DFTIdentityDryRunError("linked_supplementary_paper_missing")

        inventory: list[dict[str, Any]] = []
        resolved_paths: set[Path] = set()
        for source, role in [(paper, "main"), *[(item, "supplementary") for item in linked]]:
            raw = str(source.pdf_path or "").strip()
            if not raw:
                raise DFTIdentityDryRunError(f"paper_pdf_path_missing:{source.id}")
            raw_path = Path(raw)
            resolved = (raw_path if raw_path.is_absolute() else root / raw_path).resolve(strict=True)
            try:
                relative = resolved.relative_to(root)
            except ValueError as exc:
                raise DFTIdentityDryRunError(f"paper_pdf_outside_data_root:{source.id}") from exc
            if not resolved.is_file():
                raise DFTIdentityDryRunError(f"paper_pdf_not_file:{source.id}")
            if resolved in resolved_paths:
                raise DFTIdentityDryRunError(f"ambiguous_duplicate_pdf_path:{relative.as_posix()}")
            resolved_paths.add(resolved)
            inventory.append(
                {
                    "document_role": role,
                    "paper_id": str(source.id),
                    "paper_code": source.paper_code,
                    "database_pdf_path": raw,
                    "path": relative.as_posix(),
                    "size": resolved.stat().st_size,
                    "sha256": file_sha256(resolved),
                }
            )
        inventory.sort(key=lambda item: (item["document_role"], str(item["paper_code"]), item["path"]))
        return {
            "algorithm": "database-related-pdf-files-v1",
            "data_root": str(root),
            "files": inventory,
            "file_count": len(inventory),
            "sha256": canonical_sha256(inventory),
        }

    def _global_result_identity_analysis(
        self,
    ) -> tuple[list[DFTResult], dict[UUID, Any], dict[str, Any]]:
        papers = {row.id: row for row in self.session.scalars(select(Paper)).all()}
        results = list(
            self.session.scalars(
                select(DFTResult).order_by(DFTResult.paper_id.asc(), DFTResult.id.asc())
            ).all()
        )
        identities: dict[UUID, Any] = {}
        serialized: list[dict[str, Any]] = []
        by_paper: dict[UUID, list[tuple[DFTResult, Any]]] = defaultdict(list)
        for row in results:
            identity = self.lifecycle.build_identity(
                paper_id=row.paper_id,
                payload=self.lifecycle.authoritative_payload_for_result(row),
            )
            identities[row.id] = identity
            by_paper[row.paper_id].append((row, identity))
            serialized.append(
                {
                    "paper_id": str(row.paper_id),
                    "paper_code": papers.get(row.paper_id).paper_code if papers.get(row.paper_id) else None,
                    "result_id": str(row.id),
                    "candidate_status": row.candidate_status,
                    "stored_v2": {
                        "identity_version": row.identity_version,
                        "subject_key": row.subject_key,
                        "observation_key": row.observation_key,
                        "identity_payload": row.identity_payload,
                    },
                    "transient_v2": self._identity_payload(identity),
                }
            )

        paper_groups: list[dict[str, Any]] = []
        exact_count = conflict_count = invalid_count = 0
        for paper_id in sorted(by_paper, key=str):
            rows = by_paper[paper_id]
            observation_groups: dict[str, list[str]] = defaultdict(list)
            subject_groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
            invalid: list[dict[str, Any]] = []
            for row, identity in rows:
                if not identity.observation_key:
                    invalid.append(
                        {
                            "result_id": str(row.id),
                            "subject_key": identity.subject_key,
                            "error_codes": list(identity.error_codes),
                        }
                    )
                    continue
                observation_groups[identity.observation_key].append(str(row.id))
                subject_groups[identity.subject_key][identity.observation_key].append(str(row.id))
            exact = [
                {"observation_key": key, "result_ids": sorted(ids)}
                for key, ids in sorted(observation_groups.items())
                if len(ids) > 1
            ]
            conflicts = [
                {
                    "subject_key": subject_key,
                    "observations": [
                        {"observation_key": observation, "result_ids": sorted(ids)}
                        for observation, ids in sorted(observations.items())
                    ],
                }
                for subject_key, observations in sorted(subject_groups.items())
                if len(observations) > 1
            ]
            exact_count += len(exact)
            conflict_count += len(conflicts)
            invalid_count += len(invalid)
            if exact or conflicts or invalid:
                paper = papers.get(paper_id)
                paper_groups.append(
                    {
                        "paper_id": str(paper_id),
                        "paper_code": paper.paper_code if paper else None,
                        "exact_duplicate_groups": exact,
                        "scientific_conflict_groups": conflicts,
                        "invalid_identity_rows": sorted(invalid, key=lambda item: item["result_id"]),
                    }
                )
        return results, identities, {
            "result_count": len(results),
            "exact_duplicate_group_count": exact_count,
            "scientific_conflict_group_count": conflict_count,
            "invalid_identity_row_count": invalid_count,
            "results": serialized,
            "papers_with_classifications": paper_groups,
        }

    def _candidate_issue_analysis(self) -> dict[str, Any]:
        candidates = list(self.session.scalars(select(ExternalAnalysisCandidate)).all())
        new_candidates = [row for row in candidates if self._is_new_candidate(row)]
        candidate_by_id = {row.id: row for row in new_candidates}
        missing_issues = list(
            self.session.scalars(
                select(DFTAuditIssue).where(DFTAuditIssue.issue_type == "missing_dft_result")
            ).all()
        )
        relation_rows = list(self.session.scalars(select(DFTAuditIssueSource)).all())
        relation_by_issue: dict[UUID, set[UUID]] = defaultdict(set)
        for relation in relation_rows:
            relation_by_issue[relation.issue_id].add(relation.candidate_id)

        issues: list[dict[str, Any]] = []
        issue_ids_by_candidate: dict[UUID, list[str]] = defaultdict(list)
        for issue in missing_issues:
            source_info = self._source_resolution(issue, relation_by_issue.get(issue.id, set()))
            for candidate_id in source_info["effective_candidate_ids"]:
                try:
                    issue_ids_by_candidate[UUID(candidate_id)].append(str(issue.id))
                except ValueError:
                    continue
            result_info = self._result_resolution(issue)
            issues.append(
                {
                    "issue_id": str(issue.id),
                    "paper_id": str(issue.paper_id),
                    "status": issue.status,
                    "source_resolution": source_info,
                    "result_resolution": result_info,
                }
            )

        serialized_candidates: list[dict[str, Any]] = []
        for candidate in sorted(new_candidates, key=lambda row: str(row.id)):
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            identity = self.lifecycle.build_identity(paper_id=candidate.paper_id, payload=payload)
            serialized_candidates.append(
                {
                    "candidate_id": str(candidate.id),
                    "paper_id": str(candidate.paper_id),
                    "status": candidate.status,
                    "decision": payload.get("decision"),
                    "target_id": payload.get("target_id"),
                    "materialized_target_type": candidate.materialized_target_type,
                    "materialized_target_id": candidate.materialized_target_id,
                    "identity": self._identity_payload(identity),
                    "evidence_anchors": self._evidence_anchors(
                        payload,
                        candidate.evidence_payload if isinstance(candidate.evidence_payload, dict) else {},
                    ),
                    "associated_missing_issue_ids": sorted(issue_ids_by_candidate.get(candidate.id, [])),
                }
            )
        issues.sort(key=lambda item: item["issue_id"])
        unassociated = sorted(
            str(candidate_id)
            for candidate_id in candidate_by_id
            if not issue_ids_by_candidate.get(candidate_id)
        )
        return {
            "new_candidate_count": len(serialized_candidates),
            "missing_issue_count": len(issues),
            "unassociated_new_candidate_ids": unassociated,
            "candidates": serialized_candidates,
            "missing_issues": issues,
        }

    def _paper_reconciliation(
        self,
        *,
        paper_code: str,
        results: list[DFTResult],
        result_identities: dict[UUID, Any],
        candidate_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        paper = self.session.scalar(select(Paper).where(Paper.paper_code == paper_code))
        if paper is None:
            raise DFTIdentityDryRunError(f"paper_code_not_found:{paper_code}")
        paper_results = [row for row in results if row.paper_id == paper.id]
        statuses: dict[str, int] = defaultdict(int)
        for row in paper_results:
            statuses[str(row.candidate_status or "").casefold()] += 1
        open_issues = list(
            self.session.scalars(
                select(DFTAuditIssue).where(
                    DFTAuditIssue.paper_id == paper.id,
                    DFTAuditIssue.issue_type == "missing_dft_result",
                    DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)),
                )
            ).all()
        )
        candidate_rows = {
            row.id: row
            for row in self.session.scalars(
                select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.paper_id == paper.id)
            ).all()
            if self._is_new_candidate(row)
        }
        candidate_identities = {
            row_id: self.lifecycle.build_identity(
                paper_id=row.paper_id,
                payload=row.normalized_payload if isinstance(row.normalized_payload, dict) else {},
            )
            for row_id, row in candidate_rows.items()
        }
        relation_by_issue: dict[UUID, set[UUID]] = defaultdict(set)
        for relation in self.session.scalars(
            select(DFTAuditIssueSource).where(DFTAuditIssueSource.issue_id.in_([row.id for row in open_issues]))
        ).all():
            relation_by_issue[relation.issue_id].add(relation.candidate_id)

        gates = bulk_export_gate_results(self.session, paper_results, target_type="dft_results")
        all_pending = list(
            self.session.scalars(
                select(DFTAuditIssue).where(
                    DFTAuditIssue.paper_id == paper.id,
                    DFTAuditIssue.status.in_(sorted(DFT_AUDIT_ISSUE_PENDING_STATUSES)),
                )
            ).all()
        )
        conflict_ids_by_result: dict[UUID, list[str]] = defaultdict(list)
        for issue in all_pending:
            if issue.issue_type == "missing_dft_result":
                continue
            result_id = self._resolved_result_uuid(issue)
            if result_id:
                conflict_ids_by_result[result_id].append(str(issue.id))

        safe: list[dict[str, Any]] = []
        split: list[dict[str, Any]] = []
        unmapped: list[dict[str, Any]] = []
        unknown_multi: list[dict[str, Any]] = []
        unsafe: list[dict[str, Any]] = []
        result_by_id = {row.id: row for row in paper_results}
        for issue in sorted(open_issues, key=lambda row: str(row.id)):
            source_info = self._source_resolution(issue, relation_by_issue.get(issue.id, set()))
            source_ids = [UUID(value) for value in source_info["effective_candidate_ids"]]
            sources = [candidate_rows[value] for value in source_ids if value in candidate_rows]
            if not source_ids or len(sources) != len(source_ids):
                unmapped.append({"issue_id": str(issue.id), "reason": "missing_source_candidate"})
                continue
            target_texts = sorted(
                {
                    str(row.materialized_target_id)
                    for row in sources
                    if row.materialized_target_type == "dft_results" and row.materialized_target_id
                }
            )
            target_ids: list[UUID] = []
            for value in target_texts:
                try:
                    target_ids.append(UUID(value))
                except ValueError:
                    pass
            if not target_ids:
                unmapped.append({"issue_id": str(issue.id), "reason": "missing_materialized_target"})
                continue
            if len(target_ids) != 1 or len(target_ids) != len(target_texts):
                unknown_multi.append(
                    {"issue_id": str(issue.id), "materialized_target_ids": target_texts}
                )
                continue
            target_id = target_ids[0]
            target = result_by_id.get(target_id)
            identities = [candidate_identities[row.id] for row in sources]
            subject_keys = {identity.subject_key for identity in identities}
            if len(subject_keys) > 1:
                split.append(
                    self._split_entry(
                        issue=issue,
                        sources=sources,
                        identities=identities,
                        target=target,
                        result_identities=result_identities,
                        paper_results=paper_results,
                        source_info=source_info,
                    )
                )
                continue
            target_identity = result_identities.get(target_id)
            gate = gates.get(str(target_id))
            conditions = {
                "candidate_result_same_paper": bool(
                    target is not None
                    and target.paper_id == issue.paper_id
                    and all(row.paper_id == issue.paper_id for row in sources)
                ),
                "unique_target": len(target_ids) == 1,
                "identity_observation_matches": bool(
                    target_identity is not None
                    and target_identity.observation_key
                    and all(
                        identity.observation_key == target_identity.observation_key
                        for identity in identities
                    )
                ),
                "ai_verified_ml_ready": bool(
                    target is not None
                    and str(target.candidate_status or "").casefold() == "ai_verified_ml_ready"
                ),
                "currently_exportable": bool(gate and gate.eligible),
                "not_rejected": bool(
                    target is not None and str(target.candidate_status or "").casefold() != "rejected"
                ),
                "no_conflict_issue": not conflict_ids_by_result.get(target_id),
            }
            entry = {
                "issue_id": str(issue.id),
                "candidate_id": str(sources[0].id) if len(sources) == 1 else None,
                "candidate_ids": sorted(str(row.id) for row in sources),
                "result_id": str(target_id),
                "subject_key": identities[0].subject_key,
                "observation_key": identities[0].observation_key,
                "canonical_atom_pair": identities[0].atom_pair.canonical,
                "source_resolution": source_info,
                "export_gate": {
                    "eligible": bool(gate and gate.eligible),
                    "reasons": list(gate.reasons) if gate else ["gate_missing"],
                },
                "conflict_issue_ids": sorted(conflict_ids_by_result.get(target_id, [])),
                "conditions": conditions,
            }
            if all(conditions.values()) and len(sources) == 1:
                safe.append(entry)
            else:
                unsafe.append(entry)

        actual = {
            "before_dft_total": len(paper_results),
            "before_ai_verified_ml_ready": statuses.get("ai_verified_ml_ready", 0),
            "before_rejected": statuses.get("rejected", 0),
            "open_missing_dft_result": len(open_issues),
            "safe_single_target": len(safe),
            "identity_split_parent_issues": len(split),
            "missing_li2_results": sum(bool(row.get("li2_result_missing")) for row in split),
            "unmapped": len(unmapped),
            "unknown_multi_target": len(unknown_multi),
        }
        if paper_code == "B0102":
            if unsafe:
                raise DFTIdentityDryRunError(
                    f"B0102_unsafe_single_target:{canonical_json(unsafe[:5])}"
                )
            if actual != B0102_EXPECTED:
                raise DFTIdentityDryRunError(
                    f"B0102_expected_counts_mismatch:{canonical_json({'expected': B0102_EXPECTED, 'actual': actual})}"
                )
            self._assert_b0102_splits(split)
        return {
            "paper_id": str(paper.id),
            "paper_code": paper_code,
            "expected": B0102_EXPECTED if paper_code == "B0102" else None,
            "actual": actual,
            "safe_single_targets": safe,
            "identity_split_parent_issues": split,
            "unmapped": unmapped,
            "unknown_multi_target": unknown_multi,
            "unsafe_single_targets": unsafe,
        }

    def _split_entry(
        self,
        *,
        issue: DFTAuditIssue,
        sources: list[ExternalAnalysisCandidate],
        identities: list[Any],
        target: DFTResult | None,
        result_identities: dict[UUID, Any],
        paper_results: list[DFTResult],
        source_info: dict[str, Any],
    ) -> dict[str, Any]:
        candidates = []
        for row, identity in sorted(zip(sources, identities), key=lambda pair: str(pair[0].id)):
            matches = sorted(
                str(result.id)
                for result in paper_results
                if identity.observation_key
                and result_identities[result.id].observation_key == identity.observation_key
            )
            candidates.append(
                {
                    "candidate_id": str(row.id),
                    "materialized_target_id": row.materialized_target_id,
                    "identity": self._identity_payload(identity),
                    "matching_result_ids": matches,
                    "evidence_anchors": self._evidence_anchors(
                        row.normalized_payload if isinstance(row.normalized_payload, dict) else {},
                        row.evidence_payload if isinstance(row.evidence_payload, dict) else {},
                    ),
                }
            )
        li2 = next(
            (row for row in candidates if row["identity"]["canonical_atom_pair"] == "li2-s"),
            None,
        )
        return {
            "issue_id": str(issue.id),
            "old_result_id": str(target.id) if target else None,
            "old_result_identity": (
                self._identity_payload(result_identities[target.id]) if target else None
            ),
            "source_resolution": source_info,
            "candidates": candidates,
            "distinct_subject_keys": sorted(
                {row["identity"]["subject_key"] for row in candidates}
            ),
            "li2_result_missing": bool(li2 is not None and not li2["matching_result_ids"]),
        }

    def _assert_b0102_splits(self, split: list[dict[str, Any]]) -> None:
        actual_by_issue = {row["issue_id"]: row for row in split}
        if set(actual_by_issue) != {row["issue_id"] for row in B0102_SPLIT_EXPECTATIONS}:
            raise DFTIdentityDryRunError("B0102_split_issue_ids_mismatch")
        for expected in B0102_SPLIT_EXPECTATIONS:
            row = actual_by_issue[expected["issue_id"]]
            candidates = {item["candidate_id"]: item for item in row["candidates"]}
            if row["old_result_id"] != expected["old_result_id"]:
                raise DFTIdentityDryRunError(f"B0102_split_old_result_mismatch:{expected['issue_id']}")
            if set(candidates) != {expected["li1_candidate_id"], expected["li2_candidate_id"]}:
                raise DFTIdentityDryRunError(f"B0102_split_candidate_ids_mismatch:{expected['issue_id']}")
            li1 = candidates[expected["li1_candidate_id"]]
            li2 = candidates[expected["li2_candidate_id"]]
            if li1["identity"]["canonical_atom_pair"] != "li1-s":
                raise DFTIdentityDryRunError(f"B0102_split_li1_atom_pair_mismatch:{expected['issue_id']}")
            if li2["identity"]["canonical_atom_pair"] != "li2-s":
                raise DFTIdentityDryRunError(f"B0102_split_li2_atom_pair_mismatch:{expected['issue_id']}")
            if li1["identity"]["subject_key"] == li2["identity"]["subject_key"]:
                raise DFTIdentityDryRunError(f"B0102_split_subject_not_distinct:{expected['issue_id']}")
            if li1["matching_result_ids"] != [expected["old_result_id"]]:
                raise DFTIdentityDryRunError(f"B0102_split_li1_result_mismatch:{expected['issue_id']}")
            if li2["matching_result_ids"]:
                raise DFTIdentityDryRunError(f"B0102_split_li2_result_exists:{expected['issue_id']}")
            subject = li1["identity"]["identity_payload"]["subject"]
            observation = li1["identity"]["identity_payload"]["observation"]
            if (
                subject.get("material_key") != expected["material"].casefold()
                or subject.get("property_type") != expected["property_type"]
                or observation.get("value") != expected["value"]
                or observation.get("unit") != expected["unit"]
            ):
                raise DFTIdentityDryRunError(f"B0102_split_scientific_values_mismatch:{expected['issue_id']}")

    @staticmethod
    def _is_new_candidate(candidate: ExternalAnalysisCandidate) -> bool:
        payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
        evidence = candidate.evidence_payload if isinstance(candidate.evidence_payload, dict) else {}
        return str(payload.get("decision") or evidence.get("decision") or "").casefold() == "new_candidate"

    @staticmethod
    def _source_resolution(issue: DFTAuditIssue, relation_ids: Iterable[UUID]) -> dict[str, Any]:
        relation = sorted({str(value) for value in relation_ids})
        legacy: list[str] = []
        invalid: list[str] = []
        for raw in issue.source_candidate_ids or []:
            try:
                legacy.append(str(UUID(str(raw))))
            except ValueError:
                invalid.append(str(raw))
        legacy = sorted(set(legacy))
        if relation:
            effective = relation
            consistency = "match" if relation == legacy and not invalid else "mismatch"
            authority = "source_relation"
        else:
            effective = legacy
            consistency = "legacy_fallback" if legacy else "empty"
            authority = "legacy_source_candidate_ids"
        return {
            "authority": authority,
            "relation_candidate_ids": relation,
            "legacy_candidate_ids": legacy,
            "invalid_legacy_candidate_ids": sorted(invalid),
            "effective_candidate_ids": effective,
            "consistency": consistency,
        }

    @staticmethod
    def _resolved_result_uuid(issue: DFTAuditIssue) -> UUID | None:
        if issue.result_id is not None:
            return issue.result_id
        if issue.target_type != "dft_results":
            return None
        target = str(issue.target_id or "").strip()
        if not target or target.casefold() == "new":
            return None
        try:
            return UUID(target)
        except ValueError:
            return None

    @classmethod
    def _result_resolution(cls, issue: DFTAuditIssue) -> dict[str, Any]:
        legacy = str(issue.target_id or "").strip() or None
        effective = cls._resolved_result_uuid(issue)
        authority = "result_id" if issue.result_id is not None else "legacy_target_id"
        if issue.result_id is not None and legacy and legacy.casefold() != "new":
            consistency = "match" if legacy == str(issue.result_id) else "mismatch"
        elif issue.result_id is not None:
            consistency = "result_id_authoritative"
        else:
            consistency = "legacy_fallback"
        return {
            "authority": authority,
            "result_id": str(issue.result_id) if issue.result_id else None,
            "legacy_target_id": legacy,
            "effective_result_id": str(effective) if effective else None,
            "consistency": consistency,
        }

    @staticmethod
    def _identity_payload(identity: Any) -> dict[str, Any]:
        return {
            "identity_version": identity.identity_version,
            "subject_key": identity.subject_key,
            "observation_key": identity.observation_key,
            "identity_payload": identity.identity_payload,
            "canonical_atom_pair": identity.atom_pair.canonical,
            "error_codes": list(identity.error_codes),
            "dedupe_allowed": identity.dedupe_allowed,
        }

    @staticmethod
    def _evidence_anchors(*values: Any) -> list[dict[str, Any]]:
        keys = (
            "source_document_type",
            "page",
            "source_page",
            "table",
            "source_table",
            "row",
            "source_row",
            "row_index",
            "source_row_index",
            "figure",
            "source_figure",
            "evidence_id",
            "evidence_ids",
            "locator_id",
            "quoted_text",
        )
        anchors: dict[str, dict[str, Any]] = {}

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                anchor = {
                    key: value[key]
                    for key in keys
                    if key in value and value[key] not in (None, "", [])
                }
                if any(key in anchor for key in keys[1:]):
                    anchors[canonical_json(anchor)] = anchor
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        for value in values:
            visit(value)
        return [anchors[key] for key in sorted(anchors)]
