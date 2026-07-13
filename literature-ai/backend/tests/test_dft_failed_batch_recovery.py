from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    Base,
    CatalystSample,
    DFTAuditIssue,
    DFTResult,
    EvidenceSpan,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
)
from app.services import dft_failed_batch_recovery_service as recovery_module
from app.services.dft_failed_batch_recovery_service import (
    DFTFailedBatchRecoveryService,
    EXPECTED_BACKUP_COUNTS,
    FAILED_CANDIDATE_STATUS,
    RECOVERY_ACTION,
    RecoveryRefused,
    _model_row,
    load_manifest,
)


BACKUP_SHA256 = "4F41DBDCC6ABE78771075CA6A7A733DC224541776AE3420EAD76CC8ECEBFA2CF"
NOW = datetime(2026, 7, 13, 12, 59, 0)


def stable_uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"litai-r0-recovery-test:{label}")


@dataclass
class RecoveryDatabases:
    backup_engine: Engine
    live_engine: Engine
    issue_ids: list[UUID]
    bad_result_ids: list[UUID]
    historical_sample_id: UUID
    referenced_live_only_sample_id: UUID
    exclusive_live_only_sample_id: UUID
    other_paper_result_id: UUID

    def service(self) -> DFTFailedBatchRecoveryService:
        return DFTFailedBatchRecoveryService(
            backup_engine=self.backup_engine,
            live_engine=self.live_engine,
            backup_sha256=BACKUP_SHA256,
        )


@pytest.fixture
def recovery_databases(shared_test_database):
    root_url = make_url(shared_test_database.url)
    root_query = dict(root_url.query)
    root_query.pop("options", None)
    root_url = root_url.set(query=root_query)
    admin = sa.create_engine(root_url, future=True)
    backup_schema = f"r0_backup_{uuid4().hex}"
    live_schema = f"r0_live_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(sa.text(f'CREATE SCHEMA "{backup_schema}"'))
        connection.execute(sa.text(f'CREATE SCHEMA "{live_schema}"'))

    def schema_engine(schema: str) -> Engine:
        query = dict(root_url.query)
        query["options"] = f"-csearch_path={schema},public"
        return sa.create_engine(root_url.set(query=query), future=True)

    backup_engine = schema_engine(backup_schema)
    live_engine = schema_engine(live_schema)
    Base.metadata.create_all(backup_engine, checkfirst=False)
    Base.metadata.create_all(live_engine, checkfirst=False)
    dataset = _populate_pair(backup_engine, live_engine)
    pair = RecoveryDatabases(backup_engine=backup_engine, live_engine=live_engine, **dataset)
    try:
        yield pair
    finally:
        backup_engine.dispose()
        live_engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{backup_schema}" CASCADE'))
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{live_schema}" CASCADE'))
        admin.dispose()


def _populate_pair(backup_engine: Engine, live_engine: Engine) -> dict:
    paper_id = stable_uuid("paper-b0102")
    other_paper_id = stable_uuid("paper-other")
    run_id = stable_uuid("run")
    issue_ids = [stable_uuid(f"issue-{index}") for index in range(368)]
    bad_result_ids = [stable_uuid(f"bad-result-{index}") for index in range(25)]
    candidate_ids = [stable_uuid(f"candidate-{index}") for index in range(370)]
    historical_sample_id = stable_uuid("sample-historical")
    referenced_live_only_sample_id = stable_uuid("sample-live-referenced")
    exclusive_live_only_sample_id = stable_uuid("sample-live-exclusive")
    regular_sample_ids = [stable_uuid(f"sample-{index}") for index in range(8)]
    baseline_result_ids = [stable_uuid(f"baseline-result-{index}") for index in range(375)]
    other_paper_result_id = stable_uuid("other-paper-result")

    def populate(engine: Engine, *, live: bool) -> None:
        with Session(engine) as session:
            session.add_all(
                [
                    Paper(id=paper_id, paper_code="B0102", title="B0102", pdf_path="B0102.pdf"),
                    Paper(id=other_paper_id, paper_code="B9999", title="Other", pdf_path="other.pdf"),
                ]
            )
            session.flush()
            session.add(
                ExternalAnalysisRun(
                    id=run_id,
                    paper_id=paper_id,
                    source="local_ai",
                    source_identity="mcp:local_ide",
                    source_identity_verified=True,
                    mapping_status="applied",
                    created_at=NOW,
                )
            )
            session.flush()
            for sample_id in [historical_sample_id, *regular_sample_ids]:
                session.add(CatalystSample(id=sample_id, paper_id=paper_id, name=str(sample_id)))
            if live:
                session.add_all(
                    [
                        CatalystSample(id=referenced_live_only_sample_id, paper_id=paper_id, name="referenced"),
                        CatalystSample(id=exclusive_live_only_sample_id, paper_id=paper_id, name="exclusive"),
                    ]
                )
            session.flush()
            if live:
                session.add(
                    MechanismClaim(
                        id=stable_uuid("sample-reference-claim"),
                        paper_id=paper_id,
                        catalyst_sample_id=referenced_live_only_sample_id,
                        claim_type="test",
                        claim_text="keeps sample alive",
                    )
                )

            for index, result_id in enumerate(baseline_result_ids):
                session.add(
                    DFTResult(
                        id=result_id,
                        paper_id=paper_id,
                        catalyst_sample_id=historical_sample_id,
                        adsorbate=f"A{index}",
                        property_type="adsorption_energy",
                        value=float(index),
                        unit="eV",
                        candidate_status="ai_verified_ml_ready" if index < 373 else "Rejected",
                        candidate_identity=f"baseline-{index}",
                    )
                )
            session.add(
                DFTResult(
                    id=other_paper_result_id,
                    paper_id=other_paper_id,
                    property_type="adsorption_energy",
                    value=1.23,
                    unit="eV",
                    candidate_status="ai_verified_ml_ready",
                    candidate_identity="other-paper",
                )
            )

            candidate_index = 0
            for index, issue_id in enumerate(issue_ids):
                per_issue = 2 if index < 2 else 1
                source_ids = candidate_ids[candidate_index : candidate_index + per_issue]
                candidate_index += per_issue
                bound_target = stable_uuid(f"bound-target-{index}")
                for candidate_id in source_ids:
                    session.add(
                        ExternalAnalysisCandidate(
                            id=candidate_id,
                            run_id=run_id,
                            paper_id=paper_id,
                            candidate_type="object_review_audit",
                            normalized_payload={"decision": "new_candidate", "target_id": "new"},
                            status="ai_applied",
                            materialized_target_type="dft_results",
                            materialized_target_id=str(bound_target),
                            created_at=NOW,
                        )
                    )
                issue_kwargs = {
                    "id": issue_id,
                    "paper_id": paper_id,
                    "target_type": "dft_results",
                    "target_id": "new",
                    "issue_type": "missing_dft_result",
                    "severity": "medium",
                    "status": "needs_primary_ai",
                    "current_snapshot": None,
                    "suggested_dft": {
                        "property_type": "bond_length",
                        "value": 2.0 + index / 100,
                        "unit": "Å",
                        "raw_corrected_value": {"bond_pair": "Li1-S"},
                    },
                    "evidence_payload": {"page": 18, "evidence_ids": ["si:table:003"]},
                    "source_identities": ["mcp:local_ide"],
                    "source_candidate_ids": [str(value) for value in source_ids],
                    "fingerprint": f"issue-fingerprint-{index}",
                    "resolution_note": "Missing DFT result draft queued for authorized AI or user-controlled follow-up.",
                    "resolved_by": None,
                    "resolved_at": None,
                    "created_at": NOW + timedelta(seconds=index),
                    "updated_at": NOW + timedelta(seconds=index),
                }
                if live and index < 25:
                    result_id = bad_result_ids[index]
                    issue_kwargs.update(
                        target_id=str(result_id),
                        status="closed",
                        current_snapshot={"id": str(result_id), "property_type": "bond_length"},
                        resolution_note="ai_verified",
                        resolved_by="local_ide",
                        resolved_at=NOW + timedelta(hours=4, seconds=index),
                        updated_at=NOW + timedelta(hours=4, seconds=index),
                    )
                session.add(DFTAuditIssue(**issue_kwargs))

            if live:
                sample_cycle = [
                    referenced_live_only_sample_id,
                    exclusive_live_only_sample_id,
                    *regular_sample_ids,
                ]
                for index, (issue_id, result_id) in enumerate(zip(issue_ids[:25], bad_result_ids)):
                    value = 2.0 + index / 100
                    result = DFTResult(
                        id=result_id,
                        paper_id=paper_id,
                        catalyst_sample_id=sample_cycle[index % len(sample_cycle)],
                        adsorbate="Li2S",
                        property_type="bond_length",
                        value=value,
                        unit="Å",
                        reaction_step=None,
                        candidate_status=FAILED_CANDIDATE_STATUS,
                        extraction_protocol_version="dft_audit_issue_primary_repair_v1",
                        candidate_identity=f"failed-{index}",
                        evidence_payload={
                            "issue_id": str(issue_id),
                            "page": 18,
                            "material_identity": f"sample-{index}",
                        },
                        local_ai_verification_payload={
                            "source_label": "mcp_fast_batch",
                            "final_decision": "repair_failed_not_exportable",
                            "blocked_reasons": ["missing_atom_pair_identity"],
                        },
                    )
                    session.add(result)
                    for field_name in ("value", "adsorbate", "energy_type"):
                        session.add(
                            ExtractionFieldReview(
                                id=stable_uuid(f"review-{index}-{field_name}"),
                                paper_id=paper_id,
                                target_type="dft_results",
                                target_id=str(result_id),
                                field_name=field_name,
                                original_value=value if field_name == "value" else "Li2S",
                                reviewed_value=value if field_name == "value" else "Li2S",
                                reviewer_status="verified",
                                reviewer="local_ide",
                                reviewer_note="Fast-mode DFT processing completed from stored evidence.",
                                review_payload={
                                    "ai_verification": {
                                        "source_label": "mcp_fast_batch",
                                        "decision": "verified",
                                    }
                                },
                                created_at=NOW + timedelta(hours=4, seconds=index),
                                updated_at=NOW + timedelta(hours=4, seconds=index),
                                write_version=2,
                            )
                        )
                    session.add(
                        EvidenceSpan(
                            id=stable_uuid(f"span-{index}"),
                            paper_id=paper_id,
                            object_type="dft_results",
                            object_id=str(result_id),
                            text="Table S3",
                            page=18,
                            confidence=0.75,
                        )
                    )
                    session.add_all(
                        [
                            AuditLog(
                                id=stable_uuid(f"audit-repair-{index}"),
                                paper_id=paper_id,
                                action="repair_dft_audit_issue",
                                source="local_ide",
                                target_type="dft_audit_issue",
                                target_id=str(issue_id),
                                payload={"result_id": str(result_id)},
                                created_at=NOW + timedelta(hours=4, seconds=index),
                            ),
                            AuditLog(
                                id=stable_uuid(f"audit-verify-{index}"),
                                paper_id=paper_id,
                                action="verify_dft_result",
                                source="local_ide",
                                target_type="dft_results",
                                target_id=str(result_id),
                                payload={"issue_id": str(issue_id)},
                                created_at=NOW + timedelta(hours=4, seconds=index, milliseconds=1),
                            ),
                        ]
                    )
            session.commit()

    populate(backup_engine, live=False)
    populate(live_engine, live=True)
    return {
        "issue_ids": issue_ids,
        "bad_result_ids": bad_result_ids,
        "historical_sample_id": historical_sample_id,
        "referenced_live_only_sample_id": referenced_live_only_sample_id,
        "exclusive_live_only_sample_id": exclusive_live_only_sample_id,
        "other_paper_result_id": other_paper_result_id,
    }


def test_dry_run_identifies_exact_failed_rows_and_writes_nothing(recovery_databases, tmp_path):
    service = recovery_databases.service()
    with recovery_databases.live_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
            for table in ("dft_results", "dft_audit_issues", "extraction_field_reviews", "evidence_spans", "audit_logs")
        }
    manifest = service.build_dry_run_manifest(output_path=tmp_path / "manifest.json")
    with recovery_databases.live_engine.connect() as connection:
        after = {
            table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
            for table in before
        }

    assert before == after
    assert manifest["status"] == "dry_run_ready"
    assert manifest["identified_issue_count"] == 25
    assert manifest["identified_result_count"] == 25
    assert set(manifest["issue_ids"]) == {str(value) for value in recovery_databases.issue_ids[:25]}
    assert set(manifest["result_ids"]) == {str(value) for value in recovery_databases.bad_result_ids}
    assert manifest["dependent_record_counts"] == {
        "extraction_field_reviews": 75,
        "evidence_spans": 25,
        "evidence_locators": 0,
    }
    assert manifest["audit_logs"]["preserved_count"] == 50
    assert manifest["current_live_counts"]["dft_total"] == 400


def test_apply_is_exact_audited_scoped_and_idempotent(recovery_databases, tmp_path):
    service = recovery_databases.service()
    manifest = service.build_dry_run_manifest(output_path=tmp_path / "manifest.json")
    backup_issue_rows = {item["issue_id"]: item["backup_issue"] for item in manifest["items"]}
    preserved_audit_ids = set(manifest["audit_logs"]["preserve_ids"])

    result = service.apply_manifest(
        manifest,
        confirm_paper_code="B0102",
        expected_backup_sha256=BACKUP_SHA256,
        expected_live_fingerprint=manifest["live"]["fingerprint"],
    )
    assert result["status"] == "recovered"
    assert result["after_counts"] == EXPECTED_BACKUP_COUNTS
    assert result["deleted"] == {
        "dft_results": 25,
        "extraction_field_reviews": 75,
        "evidence_spans": 25,
        "evidence_locators": 0,
        "catalyst_samples": 1,
    }

    with Session(recovery_databases.live_engine) as session:
        restored = session.scalars(
            sa.select(DFTAuditIssue).where(DFTAuditIssue.id.in_(recovery_databases.issue_ids[:25]))
        ).all()
        assert {_model_row(row)["id"]: _model_row(row) for row in restored} == backup_issue_rows
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DFTResult).where(DFTResult.id.in_(recovery_databases.bad_result_ids))
        ) == 0
        assert session.scalar(
            sa.select(sa.func.count()).select_from(ExtractionFieldReview).where(
                ExtractionFieldReview.target_id.in_([str(value) for value in recovery_databases.bad_result_ids])
            )
        ) == 0
        assert session.scalar(
            sa.select(sa.func.count()).select_from(EvidenceSpan).where(
                EvidenceSpan.object_id.in_([str(value) for value in recovery_databases.bad_result_ids])
            )
        ) == 0
        assert set(
            session.scalars(sa.select(sa.cast(AuditLog.id, sa.String)).where(AuditLog.id.in_([UUID(value) for value in preserved_audit_ids])))
        ) == preserved_audit_ids
        compensation = session.scalar(sa.select(AuditLog).where(AuditLog.action == RECOVERY_ACTION))
        assert compensation is not None
        assert compensation.payload["manifest_sha256"] == manifest["manifest_sha256"]
        assert session.get(CatalystSample, recovery_databases.referenced_live_only_sample_id) is not None
        assert session.get(CatalystSample, recovery_databases.exclusive_live_only_sample_id) is None
        other = session.get(DFTResult, recovery_databases.other_paper_result_id)
        assert other is not None and other.value == 1.23

    second = service.build_dry_run_manifest(output_path=tmp_path / "second.json")
    assert second["status"] == "already_recovered"
    assert second["identified_issue_count"] == 0
    assert second["identified_result_count"] == 0


def test_apply_refuses_live_fingerprint_drift_without_partial_writes(recovery_databases):
    service = recovery_databases.service()
    manifest = service.build_dry_run_manifest()
    with Session(recovery_databases.live_engine) as session:
        row = session.get(DFTResult, recovery_databases.bad_result_ids[0])
        row.value = 99.0
        session.commit()
    with pytest.raises(RecoveryRefused, match="live_fingerprint_drift"):
        service.apply_manifest(
            manifest,
            confirm_paper_code="B0102",
            expected_backup_sha256=BACKUP_SHA256,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )
    with Session(recovery_databases.live_engine) as session:
        assert session.get(DFTResult, recovery_databases.bad_result_ids[1]) is not None
        assert session.get(DFTAuditIssue, recovery_databases.issue_ids[1]).status == "closed"


def test_dry_run_refuses_post_batch_manual_edit(recovery_databases):
    with Session(recovery_databases.live_engine) as session:
        session.add(
            AuditLog(
                paper_id=stable_uuid("paper-b0102"),
                action="manual_update_dft_result",
                source="human_reviewer",
                target_type="dft_results",
                target_id=str(recovery_databases.bad_result_ids[0]),
                payload={"changed": "value"},
            )
        )
        session.commit()
    with pytest.raises(RecoveryRefused, match="post_batch_audit_indicates_manual_or_unknown_edit"):
        recovery_databases.service().build_dry_run_manifest()


def test_manifest_missing_tampered_and_backup_sha_mismatch_are_refused(recovery_databases, tmp_path):
    service = recovery_databases.service()
    manifest = service.build_dry_run_manifest()
    with pytest.raises(RecoveryRefused, match="manifest_file_missing"):
        load_manifest(tmp_path / "missing.json")

    tampered = deepcopy(manifest)
    tampered["result_ids"][0] = str(uuid4())
    with pytest.raises(RecoveryRefused, match="manifest_missing_or_tampered"):
        service.apply_manifest(
            tampered,
            confirm_paper_code="B0102",
            expected_backup_sha256=BACKUP_SHA256,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )

    with pytest.raises(RecoveryRefused, match="backup_sha256_mismatch"):
        service.apply_manifest(
            manifest,
            confirm_paper_code="B0102",
            expected_backup_sha256="0" * 64,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )


def test_any_apply_exception_rolls_back_every_change(recovery_databases, monkeypatch):
    service = recovery_databases.service()
    manifest = service.build_dry_run_manifest()

    def fail_after_mutations(*args, **kwargs):
        raise RuntimeError("forced post-mutation failure")

    monkeypatch.setattr(recovery_module, "_counts", fail_after_mutations)
    with pytest.raises(RuntimeError, match="forced post-mutation failure"):
        service.apply_manifest(
            manifest,
            confirm_paper_code="B0102",
            expected_backup_sha256=BACKUP_SHA256,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )

    with Session(recovery_databases.live_engine) as session:
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DFTResult).where(DFTResult.id.in_(recovery_databases.bad_result_ids))
        ) == 25
        assert session.get(DFTAuditIssue, recovery_databases.issue_ids[0]).status == "closed"
        assert session.scalar(sa.select(sa.func.count()).select_from(AuditLog).where(AuditLog.action == RECOVERY_ACTION)) == 0
