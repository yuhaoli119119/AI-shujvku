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
    ContentWebReviewBundleV2,
    ContentWebReviewLocalVerificationResult,
    EvidenceLocator,
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
            # Model a restored historical dump, not a new catalogue intake: the
            # current ORM intake hooks intentionally assign fresh paper codes.
            session.execute(
                sa.insert(Paper.__table__),
                [
                    dict(id=paper_id, paper_code="B0102", title="B0102", pdf_path="B0102.pdf"),
                    dict(id=other_paper_id, paper_code="B9999", title="Other", pdf_path="other.pdf"),
                ],
            )
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


@pytest.mark.parametrize("mutation", ["restore_row", "delete_dependency", "delete_sample", "schema_version"])
def test_rehashed_manifest_cannot_change_the_recovery_plan(recovery_databases, mutation):
    service = recovery_databases.service()
    manifest = service.build_dry_run_manifest()
    if mutation == "restore_row":
        manifest["items"][0]["backup_issue"]["resolution_note"] = "invented restoration"
    elif mutation == "delete_dependency":
        manifest["items"][0]["dependencies"]["extraction_field_reviews"]["delete_rows"] = []
    elif mutation == "delete_sample":
        for sample in manifest["candidate_sample_analysis"]:
            sample["delete_allowed"] = True
    else:
        manifest["schema_version"] = "unknown"
    manifest["manifest_sha256"] = recovery_module.manifest_sha256(manifest)
    with pytest.raises(RecoveryRefused, match="manifest_plan_mismatch"):
        service.apply_manifest(
            manifest,
            confirm_paper_code="B0102",
            expected_backup_sha256=BACKUP_SHA256,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )
    assert service.build_dry_run_manifest()["live"]["fingerprint"] == manifest["live"]["fingerprint"]


def test_apply_refuses_changed_backup_even_when_dump_label_matches(recovery_databases):
    service = recovery_databases.service()
    manifest = service.build_dry_run_manifest()
    with Session(recovery_databases.backup_engine) as session:
        session.get(DFTAuditIssue, recovery_databases.issue_ids[0]).resolution_note = "changed backup"
        session.commit()
    with pytest.raises(RecoveryRefused, match="backup_fingerprint_drift"):
        service.apply_manifest(
            manifest,
            confirm_paper_code="B0102",
            expected_backup_sha256=BACKUP_SHA256,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )
    assert service.build_dry_run_manifest()["live"]["fingerprint"] == manifest["live"]["fingerprint"]


def test_dry_run_uses_read_only_transactions_and_preserves_full_snapshots(recovery_databases):
    service = recovery_databases.service()
    paper_code = "B0102"
    before = []
    for engine in (service.backup_engine, service.live_engine):
        with recovery_module._transactional_session(engine, read_only=True) as session:
            assert session.scalar(sa.text("SHOW transaction_read_only")) == "on"
            assert session.scalar(sa.text("SHOW transaction_isolation")) == "repeatable read"
            before.append(recovery_module._snapshot(session, paper_code).fingerprint)
    manifest = service.build_dry_run_manifest()
    assert before == [manifest["backup"]["fingerprint"], manifest["live"]["fingerprint"]]
    for index, engine in enumerate((service.backup_engine, service.live_engine)):
        with Session(engine) as session:
            assert recovery_module._snapshot(session, paper_code).fingerprint == before[index]


@pytest.mark.parametrize("database", ["backup_engine", "live_engine"])
def test_dry_run_refuses_ambiguous_paper_codes_across_libraries(recovery_databases, database):
    with getattr(recovery_databases, database).begin() as connection:
        connection.execute(sa.insert(Paper.__table__).values(
            id=uuid4(), library_name="another library", paper_code="B0102",
            title="Different paper with same code", pdf_path="different.pdf",
        ))
    with pytest.raises(RecoveryRefused, match="paper_code_ambiguous:B0102"):
        recovery_databases.service().build_dry_run_manifest()


@pytest.mark.parametrize("drift", ["manual_review", "business_reference", "wrong_protocol"])
def test_dry_run_refuses_unsafe_batch_rows(recovery_databases, drift):
    with Session(recovery_databases.live_engine) as session:
        result = session.get(DFTResult, recovery_databases.bad_result_ids[0])
        if drift == "manual_review":
            review = session.scalars(sa.select(ExtractionFieldReview).where(
                ExtractionFieldReview.target_id == str(result.id))).first()
            review.reviewer = "human"
            reason = "review_has_non_batch_or_human_edit"
        elif drift == "business_reference":
            session.add(DFTAuditIssue(
                paper_id=result.paper_id, target_type="dft_results", target_id=str(result.id),
                issue_type="value_conflict", severity="medium", status="open", fingerprint="new-reference",
            ))
            reason = "new_business_reference:dft_audit_issues"
        else:
            result.extraction_protocol_version = "another-protocol"
            reason = "repair_protocol_matches"
        session.commit()
    with pytest.raises(RecoveryRefused, match=reason):
        recovery_databases.service().build_dry_run_manifest()


@pytest.mark.parametrize("reference_type", ["support_writeback", "audit_result_id"])
def test_apply_never_nulls_foreign_keys_in_other_papers(recovery_databases, reference_type):
    service = recovery_databases.service()
    manifest = service.build_dry_run_manifest()
    result_id = recovery_databases.bad_result_ids[0]
    with Session(recovery_databases.live_engine) as session:
        other = session.get(DFTResult, recovery_databases.other_paper_result_id)
        if reference_type == "support_writeback":
            other.support_writeback_dft_result_id = result_id
            referenced_id = other.id
        else:
            issue = DFTAuditIssue(
                paper_id=other.paper_id, target_type="dft_results", target_id="unrelated",
                result_id=result_id, issue_type="value_conflict", severity="medium",
                status="open", fingerprint="other-paper-reference",
            )
            session.add(issue)
            session.flush()
            referenced_id = issue.id
        session.commit()
    with pytest.raises(RecoveryRefused, match="external_result_reference"):
        service.apply_manifest(
            manifest, confirm_paper_code="B0102", expected_backup_sha256=BACKUP_SHA256,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )
    with Session(recovery_databases.live_engine) as session:
        assert session.get(DFTResult, result_id) is not None
        if reference_type == "support_writeback":
            assert session.get(DFTResult, referenced_id).support_writeback_dft_result_id == result_id
        else:
            assert session.get(DFTAuditIssue, referenced_id).result_id == result_id


def _add_verification_reference(pair, reference_field, scope):
    with Session(pair.live_engine) as session:
        result = session.get(DFTResult, pair.bad_result_ids[0])
        if reference_field == "review_id":
            reference_id = session.scalar(sa.select(ExtractionFieldReview.id).where(
                ExtractionFieldReview.target_id == str(result.id)).order_by(ExtractionFieldReview.id))
        else:
            reference_id = uuid4()
            session.add(EvidenceLocator(
                id=reference_id, paper_id=result.paper_id, target_type="dft_results",
                target_id=str(result.id), evidence_text="batch evidence",
            ))
            session.flush()
        paper_id = result.paper_id if scope == "same_paper" else session.get(
            DFTResult, pair.other_paper_result_id).paper_id
        bundle_id = uuid4()
        session.add(ContentWebReviewBundleV2(
            id=bundle_id, paper_id=paper_id, policy_version="reference-test",
            snapshot_fingerprint="f" * 64, manifest={},
        ))
        session.flush()
        verification = ContentWebReviewLocalVerificationResult(
            id=uuid4(), bundle_id=bundle_id, plan_item_id=uuid4(), payload_hash="a" * 64,
            target_type="dft_results", target_id=str(pair.other_paper_result_id),
            field_name="value", object_snapshot_hash="b" * 64, outcome="accepted",
            verification_note="preserve full verification provenance", verified_value=1.23,
            status="applied", applied_by="reviewer", **{reference_field: reference_id},
        )
        session.add(verification)
        session.commit()
        return verification.id, reference_id


def _full_database_rows(engine):
    # All tables, including unrelated-paper rows and compensation audits. This
    # catches partial restoration/deletion as well as silent FK nulling.
    with engine.connect() as connection:
        return {
            table.name: sorted(
                [recovery_module._jsonable(dict(row)) for row in connection.execute(sa.select(table)).mappings()],
                key=lambda row: str(row.get("id")),
            ) for table in Base.metadata.sorted_tables
        }


@pytest.mark.parametrize("reference_field", ["review_id", "locator_id"])
@pytest.mark.parametrize("scope", ["same_paper", "other_paper"])
@pytest.mark.parametrize("phase", ["dry_run", "apply", "locked_guard"])
def test_local_verification_references_refuse_recovery_without_any_mutation(
    recovery_databases, monkeypatch, reference_field, scope, phase,
):
    pair = recovery_databases
    service = pair.service()
    # A locator is added before the clean plan so subsequent apply probes
    # isolate verification drift, rather than dependency drift.
    verification_id, reference_id = _add_verification_reference(pair, reference_field, scope)
    with Session(pair.live_engine) as session:
        verification_row = _model_row(session.get(ContentWebReviewLocalVerificationResult, verification_id))
    if phase != "dry_run":
        # Temporarily detach only the synthetic FK to build a valid plan, then
        # restore it before apply. All writes are confined to the test schema.
        with Session(pair.live_engine) as session:
            setattr(session.get(ContentWebReviewLocalVerificationResult, verification_id), reference_field, None)
            session.commit()
        manifest = service.build_dry_run_manifest()
        with Session(pair.live_engine) as session:
            row = session.get(ContentWebReviewLocalVerificationResult, verification_id)
            recovery_module._restore_model_from_row(row, verification_row, preserve=("id",))
            session.commit()
    before = _full_database_rows(pair.live_engine)
    with Session(pair.live_engine) as session:
        snapshot = recovery_module._snapshot(session, "B0102")
        assert snapshot.fingerprint == recovery_module._snapshot(session, "B0102").fingerprint
        refs = snapshot.tables[ContentWebReviewLocalVerificationResult.__tablename__]
        assert refs == [verification_row]
    if phase == "dry_run":
        for _ in range(2):
            with pytest.raises(RecoveryRefused, match=f"local_verification_reference:{reference_field}"):
                service.build_dry_run_manifest()
    else:
        assert snapshot.fingerprint != manifest["live"]["fingerprint"]
        if phase == "locked_guard":
            # Even a stale snapshot must not bypass the independent global
            # query. Observe actual PostgreSQL locks when that query runs.
            original = recovery_module._verification_references
            def assert_locked(session, review_ids, locator_ids):
                if session.bind.engine is pair.backup_engine:
                    return original(session, review_ids, locator_ids)
                locks = session.execute(sa.text(
                    "SELECT relation::regclass::text FROM pg_locks "
                    "WHERE pid=pg_backend_pid() AND mode='RowShareLock'"
                )).scalars().all()
                assert any("extraction_field_reviews" in name for name in locks)
                assert any("evidence_locators" in name for name in locks)
                assert session.scalar(sa.text("SHOW transaction_isolation")) == "serializable"
                return original(session, review_ids, locator_ids)
            snapshot_fn = recovery_module._snapshot
            def stale_live_snapshot(session, code):
                if session.bind.engine is pair.backup_engine:
                    return snapshot_fn(session, code)
                pytest.fail("locked guard did not reject before live snapshot")
            monkeypatch.setattr(recovery_module, "_snapshot", stale_live_snapshot)
            monkeypatch.setattr(recovery_module, "_verification_references", assert_locked)
        with pytest.raises(RecoveryRefused, match=f"local_verification_reference:{reference_field}"):
            service.apply_manifest(
                manifest, confirm_paper_code="B0102", expected_backup_sha256=BACKUP_SHA256,
                expected_live_fingerprint=manifest["live"]["fingerprint"],
            )
    assert _full_database_rows(pair.live_engine) == before
    with Session(pair.live_engine) as session:
        actual = session.get(ContentWebReviewLocalVerificationResult, verification_id)
        assert _model_row(actual) == verification_row
        assert getattr(actual, reference_field) == reference_id
        assert getattr(actual, reference_field) is not None


@pytest.mark.parametrize("reference_field", ["review_id", "locator_id"])
@pytest.mark.parametrize("force_rollback", [False, True])
def test_unselected_verification_dependencies_are_preserved_on_success_and_rollback(
    recovery_databases, monkeypatch, reference_field, force_rollback,
):
    pair = recovery_databases
    verification_id, dependency_id = _add_verification_reference(pair, reference_field, "other_paper")
    model = ExtractionFieldReview if reference_field == "review_id" else EvidenceLocator
    with Session(pair.live_engine) as session:
        # This dependency belongs to a retained DFT result, so its verification
        # must not block the narrow recovery or lose either provenance FK.
        session.get(model, dependency_id).target_id = str(stable_uuid("baseline-result-0"))
        session.commit()
        verification_before = _model_row(session.get(ContentWebReviewLocalVerificationResult, verification_id))
    service = pair.service()
    manifest = service.build_dry_run_manifest()
    before = _full_database_rows(pair.live_engine)
    if force_rollback:
        def fail_after_all_mutations(*args):
            raise RuntimeError("forced failure after deletions and compensation audit")
        monkeypatch.setattr(recovery_module, "_counts", fail_after_all_mutations)
        with pytest.raises(RuntimeError, match="forced failure after deletions"):
            service.apply_manifest(
                manifest, confirm_paper_code="B0102", expected_backup_sha256=BACKUP_SHA256,
                expected_live_fingerprint=manifest["live"]["fingerprint"],
            )
        assert _full_database_rows(pair.live_engine) == before
    else:
        result = service.apply_manifest(
            manifest, confirm_paper_code="B0102", expected_backup_sha256=BACKUP_SHA256,
            expected_live_fingerprint=manifest["live"]["fingerprint"],
        )
        assert result["status"] == "recovered"
    with Session(pair.live_engine) as session:
        assert _model_row(session.get(ContentWebReviewLocalVerificationResult, verification_id)) == verification_before
        assert getattr(session.get(ContentWebReviewLocalVerificationResult, verification_id), reference_field) == dependency_id
        assert session.get(model, dependency_id) is not None
