from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    AuditLog,
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
)
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_b0102_reconciliation_service import (
    B0102_INPUT_BACKUP_SHA256,
    B0102_MANIFEST_CANONICAL_SHA256,
    B0102_PDF_SNAPSHOT_FINGERPRINT,
    B0102_PRE_APPLY_DATABASE_FINGERPRINT,
    B0102_SPLIT_FIXED,
    B0102ReconciliationError,
    DFTB0102ReconciliationService,
)
from app.services.dft_identity_dry_run_service import (
    B0102_EXPECTED,
    DFTIdentityDryRunError,
    DFTIdentityDryRunService,
    canonical_sha256,
    file_sha256,
)
from app.services.dft_review_service import DFTResultReviewService
from app.services.verification_session_service import VerificationSessionService
from scripts.dft_b0102_reconciliation import build_parser


def _payload(atom_pair: str) -> dict:
    return {
        "target_type": "dft_results",
        "target_id": "new",
        "decision": "new_candidate",
        "corrected_value": {
            "material_identity": "FeN4-G@Li2S",
            "property_type": "bond_length",
            "adsorbate": "Li2S",
            "bond_pair": atom_pair,
            "value": 2.18,
            "unit": "Å",
        },
        "evidence_location": {
            "source_document_type": "supplementary_information",
            "page": 12,
            "table": "Table S4",
            "row": atom_pair,
            "quoted_text": f"{atom_pair} = 2.18 Å",
        },
    }


def _child(
    session: Session,
    lifecycle: DFTAuditIssueLifecycleService,
    *,
    parent: DFTAuditIssue,
    candidate: ExternalAnalysisCandidate,
    row: DFTResult | None,
    status: str,
    resolution_code: str | None,
) -> DFTAuditIssue:
    payload = candidate.normalized_payload
    identity = lifecycle.build_identity(paper_id=candidate.paper_id, payload=payload)
    child = DFTAuditIssue(
        paper_id=candidate.paper_id,
        target_type="dft_results",
        target_id=str(row.id) if row else "new",
        result_id=row.id if row else None,
        issue_type="missing_dft_result",
        severity="high",
        status=status,
        source_candidate_ids=[str(candidate.id)],
        fingerprint=DFTAuditIssueService(session).fingerprint_missing_issue(
            paper_id=candidate.paper_id,
            payload=payload,
            candidate_id=str(candidate.id),
        ),
        parent_issue_id=parent.id,
        resolution_code=resolution_code,
    )
    lifecycle.initialize_issue_identity(child, identity=identity, row=row)
    child.status = status
    child.resolution_code = resolution_code
    session.add(child)
    session.flush()
    lifecycle.bind_source_candidate(child, candidate)
    return child


def test_terminal_parent_materializes_and_ai_verifies_only_li2_child(setup_test_db):
    with Session(setup_test_db) as session:
        paper = Paper(title="split lineage", paper_code="B9002", pdf_path="paper.pdf", authors=["A"])
        session.add(paper)
        session.flush()
        run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="split-test")
        session.add(run)
        session.flush()
        li1 = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=_payload("Li1-S"),
            status="materialized",
        )
        li2 = ExternalAnalysisCandidate(
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=_payload("Li2-S"),
            status="materialized",
        )
        session.add_all([li1, li2])
        session.flush()
        lifecycle = DFTAuditIssueLifecycleService(session)
        old_identity = lifecycle.build_identity(paper_id=paper.id, payload=li1.normalized_payload)
        old = DFTResult(
            paper_id=paper.id,
            property_type="bond_length",
            adsorbate="Li2S",
            value=2.18,
            value_kind="point",
            unit="Å",
            evidence_text="Li1-S = 2.18 Å",
            evidence_payload={
                "material_identity": "FeN4-G@Li2S",
                "atom_pair": "li1-s",
                "page": 12,
            },
            candidate_status="ai_verified_ml_ready",
            candidate_identity=uuid4().hex,
        )
        lifecycle.apply_result_identity(old, old_identity)
        session.add(old)
        session.flush()
        for candidate in (li1, li2):
            candidate.materialized_target_type = "dft_results"
            candidate.materialized_target_id = str(old.id)
            session.add(candidate)
        parent = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            severity="high",
            status="closed",
            source_candidate_ids=[str(li1.id), str(li2.id)],
            fingerprint=uuid4().hex,
            resolution_code="identity_split",
            lifecycle_stage="closed",
        )
        session.add(parent)
        session.flush()
        li1_child = _child(
            session,
            lifecycle,
            parent=parent,
            candidate=li1,
            row=old,
            status="closed",
            resolution_code="exact_duplicate",
        )
        li2_child = _child(
            session,
            lifecycle,
            parent=parent,
            candidate=li2,
            row=None,
            status="needs_primary_ai",
            resolution_code=None,
        )
        session.flush()

        lifecycle.release_candidate_for_identity_split(
            candidate=li2,
            old_result=old,
            parent_issue=parent,
            child_issue=li2_child,
            candidate_identity=lifecycle.build_identity(paper_id=paper.id, payload=li2.normalized_payload),
        )
        materialized = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest-ai",
            candidate_ids={li2.id},
        )
        assert materialized["materialized_count"] == 1
        assert materialized["materialized_items"][0]["issue_id"] == str(li2_child.id)
        new_result_id = UUID(materialized["materialized_items"][0]["dft_result_id"])
        li2_child = session.get(DFTAuditIssue, li2_child.id)
        assert li2_child.result_id == new_result_id
        assert [issue.id for issue in lifecycle.active_issues_for_target(
            paper_id=paper.id,
            target_id=new_result_id,
        )] == [li2_child.id]
        verified = DFTResultReviewService(session).verify_result(
            paper_id=paper.id,
            result_id=new_result_id,
            confirm_reviewed_against_pdf=True,
            reviewer="pytest-ai",
            verification_actor_type="ai",
            source_label="existing-pdf-evidence",
            evidence_payload=_payload("Li2-S")["evidence_location"],
            commit=False,
            compact_result=True,
        )
        session.flush()

        assert verified["actor_type"] == "ai"
        assert verified["export_safety"]["is_exportable"] is True
        assert session.get(DFTResult, new_result_id).candidate_status == "ai_verified_ml_ready"
        assert session.get(DFTAuditIssue, parent.id).resolution_code == "identity_split"
        assert session.get(DFTAuditIssue, parent.id).status == "closed"
        assert session.get(DFTAuditIssue, li1_child.id).resolution_code == "exact_duplicate"
        assert session.get(DFTAuditIssue, li1_child.id).status == "closed"
        li2_child = session.get(DFTAuditIssue, li2_child.id)
        assert li2_child.status == "closed"
        assert li2_child.result_id == new_result_id
        assert li2_child.resolution_code == "verified"
        assert session.scalar(
            select(DFTAuditIssueSource).where(
                DFTAuditIssueSource.issue_id == li2_child.id,
                DFTAuditIssueSource.candidate_id == li2.id,
            )
        ) is not None


@pytest.mark.no_test_database
def test_b0102_cli_is_strict_and_has_no_force():
    parser = build_parser()
    destinations = {action.dest for action in parser._actions}
    assert "force" not in destinations
    assert {"dry_run", "apply", "database_url", "manifest", "output_report"} <= destinations
    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.no_test_database
def test_wrong_fixed_confirmation_stops_before_database_access():
    service = DFTB0102ReconciliationService.__new__(DFTB0102ReconciliationService)
    with pytest.raises(B0102ReconciliationError, match="invalid_database_fingerprint_sha256_confirmation"):
        service.reconcile(
            manifest={"canonical_payload": {}},
            expected_manifest_sha256=B0102_MANIFEST_CANONICAL_SHA256,
            expected_database_fingerprint="wrong",
            expected_pdf_fingerprint="wrong",
            pdf_preflight_fingerprint="wrong",
        )


@pytest.mark.parametrize(
    ("argument", "error"),
    [
        ("expected_manifest_sha256", "invalid_manifest_sha256_confirmation"),
        ("expected_backup_sha256", "invalid_backup_sha256_confirmation"),
        ("expected_database_fingerprint", "invalid_database_fingerprint_sha256_confirmation"),
        ("expected_pdf_fingerprint", "invalid_pdf_fingerprint_sha256_confirmation"),
    ],
)
def test_wrong_preflight_confirmation_is_zero_write(
    setup_test_db,
    tmp_path,
    argument,
    error,
):
    with Session(setup_test_db) as session:
        paper = Paper(title="preflight sentinel", paper_code="B9998", pdf_path="sentinel.pdf")
        session.add(paper)
        session.commit()
    manifest_path = tmp_path / "manifest.json"
    backup_path = tmp_path / "backup.dump"
    kwargs = {
        "expected_manifest_sha256": B0102_MANIFEST_CANONICAL_SHA256,
        "expected_backup_sha256": B0102_INPUT_BACKUP_SHA256,
        "expected_database_fingerprint": B0102_PRE_APPLY_DATABASE_FINGERPRINT,
        "expected_pdf_fingerprint": B0102_PDF_SNAPSHOT_FINGERPRINT,
    }
    kwargs[argument] = "wrong"
    with Session(setup_test_db) as session:
        before = (
            session.query(Paper).count(),
            session.query(DFTResult).count(),
            session.query(DFTAuditIssue).count(),
            session.query(AuditLog).count(),
        )
    with pytest.raises(B0102ReconciliationError, match=error):
        DFTB0102ReconciliationService.load_and_validate_manifest(
            manifest_path,
            backup_path=backup_path,
            paper_code="B0102",
            **kwargs,
        )
    with Session(setup_test_db) as session:
        after = (
            session.query(Paper).count(),
            session.query(DFTResult).count(),
            session.query(DFTAuditIssue).count(),
            session.query(AuditLog).count(),
        )
    assert after == before


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("manifest_sha", "manifest_canonical_sha256_mismatch"),
        ("backup_sha", "backup_sha256_mismatch"),
    ],
)
def test_tampered_manifest_or_backup_file_is_zero_write(
    setup_test_db,
    tmp_path,
    monkeypatch,
    failure,
    error,
):
    manifest_path = tmp_path / "manifest.json"
    backup_path = tmp_path / "backup.dump"
    backup_path.write_bytes(b"temporary-test-backup")
    payload = {
        "backup": {"sha256": B0102_INPUT_BACKUP_SHA256},
        "preconditions_for_apply": {
            "paper_code": "B0102",
            "database_data_fingerprint": B0102_PRE_APPLY_DATABASE_FINGERPRINT,
            "pdf_snapshot_fingerprint": B0102_PDF_SNAPSHOT_FINGERPRINT,
        },
        "paper_reconciliation": {
            "paper_code": "B0102",
            "actual": {},
            "safe_single_targets": [],
            "identity_split_parent_issues": [],
        },
    }
    manifest_path.write_text(
        json.dumps(
            {
                "canonical_payload": payload,
                "canonical_sha256": B0102_MANIFEST_CANONICAL_SHA256,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.services.dft_b0102_reconciliation_service.canonical_sha256",
        lambda _value: "wrong" if failure == "manifest_sha" else B0102_MANIFEST_CANONICAL_SHA256,
    )
    monkeypatch.setattr(
        "app.services.dft_b0102_reconciliation_service.file_sha256",
        lambda _path: "wrong" if failure == "backup_sha" else B0102_INPUT_BACKUP_SHA256,
    )
    with Session(setup_test_db) as session:
        before = (session.query(DFTResult).count(), session.query(DFTAuditIssue).count(), session.query(AuditLog).count())
    with pytest.raises(B0102ReconciliationError, match=error):
        DFTB0102ReconciliationService.load_and_validate_manifest(
            manifest_path,
            expected_manifest_sha256=B0102_MANIFEST_CANONICAL_SHA256,
            backup_path=backup_path,
            expected_backup_sha256=B0102_INPUT_BACKUP_SHA256,
            expected_database_fingerprint=B0102_PRE_APPLY_DATABASE_FINGERPRINT,
            expected_pdf_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
            paper_code="B0102",
        )
    with Session(setup_test_db) as session:
        after = (session.query(DFTResult).count(), session.query(DFTAuditIssue).count(), session.query(AuditLog).count())
    assert after == before


@pytest.mark.no_test_database
def test_alternative_authorized_recovery_hashes_pass_when_all_inputs_match(tmp_path):
    backup_path = tmp_path / "authorized-recovery.dump"
    backup_path.write_bytes(b"authorized recovery with retained audit history")
    backup_sha = file_sha256(backup_path)
    database_fingerprint = "a" * 64
    safe_mapping = [
        {
            "issue_id": str(UUID(int=index + 1)),
            "candidate_id": str(UUID(int=10_000 + index)),
            "candidate_ids": [str(UUID(int=10_000 + index))],
            "result_id": str(UUID(int=20_000 + index)),
            "observation_key": f"dft-observation-v2:test-{index:03d}",
            "conditions": {"all_scientific_preconditions": True},
        }
        for index in range(366)
    ]
    split_mapping = [
        {
            "issue_id": fixed["issue_id"],
            "old_result_id": fixed["old_result_id"],
            "candidates": [
                {
                    "candidate_id": fixed["li1_candidate_id"],
                    "identity": {"observation_key": fixed["li1_observation_key"]},
                },
                {
                    "candidate_id": fixed["li2_candidate_id"],
                    "identity": {"observation_key": fixed["li2_observation_key"]},
                },
            ],
            "li2_result_missing": True,
        }
        for fixed in B0102_SPLIT_FIXED
    ]
    payload = {
        "backup": {"path": str(backup_path), "sha256": backup_sha.upper()},
        "preconditions_for_apply": {
            "paper_code": "B0102",
            "database_data_fingerprint": database_fingerprint,
            "pdf_snapshot_fingerprint": B0102_PDF_SNAPSHOT_FINGERPRINT,
        },
        "paper_reconciliation": {
            "paper_code": "B0102",
            "actual": B0102_EXPECTED,
            "safe_single_targets": safe_mapping,
            "identity_split_parent_issues": split_mapping,
            "unmapped": [],
            "unknown_multi_target": [],
            "unsafe_single_targets": [],
        },
    }
    manifest_sha = canonical_sha256(payload)
    assert manifest_sha != B0102_MANIFEST_CANONICAL_SHA256
    manifest_path = tmp_path / "authorized-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "canonical_payload": payload,
                "canonical_sha256": manifest_sha,
            }
        ),
        encoding="utf-8",
    )

    loaded = DFTB0102ReconciliationService.load_and_validate_manifest(
        manifest_path,
        expected_manifest_sha256=manifest_sha,
        backup_path=backup_path,
        expected_backup_sha256=backup_sha,
        expected_database_fingerprint=database_fingerprint,
        expected_pdf_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
        paper_code="B0102",
    )

    assert loaded["canonical_sha256"] == manifest_sha
    assert loaded["canonical_payload"]["backup"]["sha256"] == backup_sha.upper()


def _safe_payload(material: str, value: float) -> dict:
    return {
        "target_type": "dft_results",
        "target_id": "new",
        "decision": "new_candidate",
        "corrected_value": {
            "material_identity": material,
            "property_type": "adsorption_energy",
            "adsorbate": "Li2S4",
            "value": value,
            "unit": "eV",
        },
        "evidence_location": {
            "source_document_type": "main_text",
            "page": 5,
            "quoted_text": f"{material} {value} eV",
        },
    }


def _seed_b0102_fixture(session: Session) -> None:
    paper = Paper(
        id=UUID("0ed01979-08b6-4fa2-9d24-81ef54c71aef"),
        title="B0102 reconciliation fixture",
        paper_code="B0102",
        pdf_path="main.pdf",
        authors=["A"],
    )
    session.add(paper)
    session.flush()
    run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="b0102-test")
    session.add(run)
    session.flush()
    lifecycle = DFTAuditIssueLifecycleService(session)

    def result(payload: dict, *, row_id: UUID | None = None, status: str = "ai_verified_ml_ready") -> DFTResult:
        corrected = payload["corrected_value"]
        row = DFTResult(
            id=row_id or uuid4(),
            paper_id=paper.id,
            property_type=corrected["property_type"],
            adsorbate=corrected.get("adsorbate"),
            value=corrected["value"],
            value_kind="point",
            unit=corrected["unit"],
            evidence_text=payload["evidence_location"]["quoted_text"],
            evidence_payload={
                "material_identity": corrected["material_identity"],
                "atom_pair": corrected.get("bond_pair"),
                **payload["evidence_location"],
            },
            candidate_status=status,
            candidate_identity=uuid4().hex,
        )
        lifecycle.apply_result_identity(
            row,
            lifecycle.build_identity(paper_id=paper.id, payload=payload),
        )
        session.add(row)
        session.flush()
        return row

    def candidate(payload: dict, row: DFTResult, *, candidate_id: UUID | None = None) -> ExternalAnalysisCandidate:
        item = ExternalAnalysisCandidate(
            id=candidate_id or uuid4(),
            run_id=run.id,
            paper_id=paper.id,
            candidate_type="object_review_audit",
            normalized_payload=payload,
            evidence_payload=payload["evidence_location"],
            status="materialized",
            materialized_target_type="dft_results",
            materialized_target_id=str(row.id),
        )
        session.add(item)
        session.flush()
        return item

    def issue(candidates: list[ExternalAnalysisCandidate], *, issue_id: UUID | None = None) -> DFTAuditIssue:
        item = DFTAuditIssue(
            id=issue_id or uuid4(),
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            severity="high",
            status="needs_primary_ai",
            source_candidate_ids=[str(row.id) for row in candidates],
            fingerprint=uuid4().hex,
        )
        session.add(item)
        session.flush()
        return item

    for index in range(366):
        payload = _safe_payload(f"safe-{index:03d}", float(index + 1))
        row = result(payload)
        issue([candidate(payload, row)])

    for fixed in B0102_SPLIT_FIXED:
        li1_payload = _payload("Li1-S")
        li1_payload["corrected_value"]["material_identity"] = fixed["material"]
        li1_payload["corrected_value"]["value"] = float(fixed["value"])
        li2_payload = deepcopy(li1_payload)
        li2_payload["corrected_value"]["bond_pair"] = "Li2-S"
        li2_payload["evidence_location"]["row"] = "Li2-S"
        old = result(li1_payload, row_id=UUID(fixed["old_result_id"]))
        li1 = candidate(li1_payload, old, candidate_id=UUID(fixed["li1_candidate_id"]))
        li2 = candidate(li2_payload, old, candidate_id=UUID(fixed["li2_candidate_id"]))
        issue([li1, li2], issue_id=UUID(fixed["issue_id"]))

    for index in range(5):
        result(_safe_payload(f"extra-{index}", -100.0 - index))
    for index in range(2):
        result(_safe_payload(f"rejected-{index}", -200.0 - index), status="Rejected")
    session.commit()


def _seed_one_fixed_split(session: Session, *, missing_evidence: bool) -> tuple[dict, dict]:
    fixed = B0102_SPLIT_FIXED[0]
    paper = Paper(
        id=UUID("0ed01979-08b6-4fa2-9d24-81ef54c71aef"),
        title="single split fixture",
        paper_code="B0102",
        pdf_path="main.pdf",
        authors=["A"],
    )
    session.add(paper)
    session.flush()
    run = ExternalAnalysisRun(paper_id=paper.id, source="local_ai", source_label="split-failure")
    session.add(run)
    session.flush()
    li1_payload = _payload("Li1-S")
    li1_payload["corrected_value"]["material_identity"] = fixed["material"]
    li1_payload["corrected_value"]["value"] = float(fixed["value"])
    li2_payload = deepcopy(li1_payload)
    li2_payload["corrected_value"]["bond_pair"] = "Li2-S"
    li2_payload["evidence_location"]["row"] = "Li2-S"
    if missing_evidence:
        li2_payload.pop("evidence_location")
    lifecycle = DFTAuditIssueLifecycleService(session)
    old = DFTResult(
        id=UUID(fixed["old_result_id"]),
        paper_id=paper.id,
        property_type="bond_length",
        adsorbate="Li2S",
        value=float(fixed["value"]),
        value_kind="point",
        unit="Å",
        evidence_text="Li1-S PDF evidence",
        evidence_payload={"material_identity": fixed["material"], "atom_pair": "li1-s", "page": 12},
        candidate_status="ai_verified_ml_ready",
        candidate_identity=uuid4().hex,
    )
    lifecycle.apply_result_identity(
        old,
        lifecycle.build_identity(paper_id=paper.id, payload=li1_payload),
    )
    session.add(old)
    session.flush()
    li1 = ExternalAnalysisCandidate(
        id=UUID(fixed["li1_candidate_id"]),
        run_id=run.id,
        paper_id=paper.id,
        candidate_type="object_review_audit",
        normalized_payload=li1_payload,
        status="materialized",
        materialized_target_type="dft_results",
        materialized_target_id=str(old.id),
    )
    li2 = ExternalAnalysisCandidate(
        id=UUID(fixed["li2_candidate_id"]),
        run_id=run.id,
        paper_id=paper.id,
        candidate_type="object_review_audit",
        normalized_payload=li2_payload,
        status="materialized",
        materialized_target_type="dft_results",
        materialized_target_id=str(old.id),
    )
    session.add_all([li1, li2])
    session.flush()
    parent = DFTAuditIssue(
        id=UUID(fixed["issue_id"]),
        paper_id=paper.id,
        target_type="dft_results",
        target_id="new",
        issue_type="missing_dft_result",
        severity="high",
        status="needs_primary_ai",
        source_candidate_ids=[str(li1.id), str(li2.id)],
        fingerprint=uuid4().hex,
    )
    session.add(parent)
    session.commit()
    return (
        {
            "issue_id": str(parent.id),
            "old_result_id": str(old.id),
            "candidates": [
                {"candidate_id": str(li1.id)},
                {"candidate_id": str(li2.id)},
            ],
        },
        fixed,
    )


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("missing_evidence", "li2_standard_materialization_failed"),
        ("non_ai_actor", "li2_ai_verification_export_gate_failed"),
        ("export_gate", "li2_ai_verification_export_gate_failed"),
    ],
)
def test_split_failure_rolls_back_children_result_sources_and_audit(
    setup_test_db,
    monkeypatch,
    failure,
    error,
):
    with Session(setup_test_db) as session:
        entry, fixed = _seed_one_fixed_split(
            session,
            missing_evidence=failure == "missing_evidence",
        )
    if failure in {"non_ai_actor", "export_gate"}:
        monkeypatch.setattr(
            DFTResultReviewService,
            "verify_result",
            lambda _self, **_kwargs: {
                "actor_type": "human" if failure == "non_ai_actor" else "ai",
                "export_safety": {"is_exportable": failure != "export_gate"},
            },
        )
    with Session(setup_test_db) as session:
        with pytest.raises(B0102ReconciliationError, match=error):
            DFTB0102ReconciliationService(session)._reconcile_split(entry, fixed)
        session.rollback()
    with Session(setup_test_db) as session:
        parent = session.get(DFTAuditIssue, UUID(fixed["issue_id"]))
        li2 = session.get(ExternalAnalysisCandidate, UUID(fixed["li2_candidate_id"]))
        assert parent.status == "needs_primary_ai"
        assert parent.resolution_code is None
        assert li2.status == "materialized"
        assert li2.materialized_target_id == fixed["old_result_id"]
        assert session.query(DFTResult).count() == 1
        assert session.query(DFTAuditIssue).count() == 1
        assert session.query(DFTAuditIssueSource).count() == 0
        assert session.query(AuditLog).count() == 0


def test_full_reconciliation_rolls_back_fault_applies_and_is_idempotent(
    setup_test_db,
    monkeypatch,
):
    def gates(_session, rows, target_type):
        assert target_type == "dft_results"
        return {
            str(row.id): SimpleNamespace(
                eligible=str(row.candidate_status or "").casefold() != "rejected",
                reasons=() if str(row.candidate_status or "").casefold() != "rejected" else ("rejected",),
            )
            for row in rows
        }

    monkeypatch.setattr(
        "app.services.dft_identity_dry_run_service.bulk_export_gate_results",
        gates,
    )
    monkeypatch.setattr(
        "app.services.dft_b0102_reconciliation_service.bulk_export_gate_results",
        gates,
    )
    monkeypatch.setattr(
        "app.services.dft_b0102_reconciliation_service.canonical_sha256",
        lambda _value: B0102_MANIFEST_CANONICAL_SHA256,
    )
    monkeypatch.setattr(
        DFTIdentityDryRunService,
        "database_data_fingerprint",
        lambda _self: {"sha256": B0102_PRE_APPLY_DATABASE_FINGERPRINT},
    )

    with Session(setup_test_db) as session:
        _seed_b0102_fixture(session)
    with Session(setup_test_db) as session:
        reconciliation = DFTB0102ReconciliationService(session)._live_authoritative_reconciliation()
        manifest = {
            "canonical_payload": {"paper_reconciliation": reconciliation},
            "canonical_sha256": B0102_MANIFEST_CANONICAL_SHA256,
        }

    safe_result_id = UUID(reconciliation["safe_single_targets"][0]["result_id"])
    with Session(setup_test_db) as session:
        session.get(DFTResult, safe_result_id).candidate_status = "system_candidate"
        session.flush()
        with pytest.raises(DFTIdentityDryRunError, match="B0102_unsafe_single_target"):
            DFTB0102ReconciliationService(session).reconcile(
                manifest=manifest,
                expected_manifest_sha256=B0102_MANIFEST_CANONICAL_SHA256,
                expected_database_fingerprint=B0102_PRE_APPLY_DATABASE_FINGERPRINT,
                expected_pdf_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
                pdf_preflight_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
            )
        session.rollback()

    drifted = deepcopy(manifest)
    drifted["canonical_payload"]["paper_reconciliation"]["safe_single_targets"][0]["conditions"][
        "currently_exportable"
    ] = False
    with Session(setup_test_db) as session:
        with pytest.raises(B0102ReconciliationError, match="authoritative_manifest_mapping_drift"):
            DFTB0102ReconciliationService(session).reconcile(
                manifest=drifted,
                expected_manifest_sha256=B0102_MANIFEST_CANONICAL_SHA256,
                expected_database_fingerprint=B0102_PRE_APPLY_DATABASE_FINGERPRINT,
                expected_pdf_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
                pdf_preflight_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
            )
        session.rollback()

    with Session(setup_test_db) as session:
        with pytest.raises(B0102ReconciliationError, match="injected_fault:after_safe_366"):
            DFTB0102ReconciliationService(session).reconcile(
                manifest=manifest,
                expected_manifest_sha256=B0102_MANIFEST_CANONICAL_SHA256,
                expected_database_fingerprint=B0102_PRE_APPLY_DATABASE_FINGERPRINT,
                expected_pdf_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
                pdf_preflight_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
                fault_after="after_safe_366",
            )
        session.rollback()
    with Session(setup_test_db) as session:
        paper_id = session.scalar(select(Paper.id).where(Paper.paper_code == "B0102"))
        assert session.query(DFTResult).filter(DFTResult.paper_id == paper_id).count() == 375
        assert session.query(DFTAuditIssue).filter(DFTAuditIssue.paper_id == paper_id).count() == 368
        assert session.query(DFTAuditIssue).filter(DFTAuditIssue.parent_issue_id.is_not(None)).count() == 0
        assert session.query(AuditLog).filter(
            AuditLog.action.in_([
                "reconciled_materialized_verified_result_v2",
                "reconciled_identity_split_v2",
            ])
        ).count() == 0

    with Session(setup_test_db) as session:
        applied = DFTB0102ReconciliationService(session).reconcile(
            manifest=manifest,
            expected_manifest_sha256=B0102_MANIFEST_CANONICAL_SHA256,
            expected_database_fingerprint=B0102_PRE_APPLY_DATABASE_FINGERPRINT,
            expected_pdf_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
            pdf_preflight_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
        )
        assert applied["status"] == "reconciled"
        readback = applied["final_readback"]
        assert readback["is_exact_final_state"] is True
        assert (
            readback["dft_result_total"],
            readback["ai_verified_ml_ready"],
            readback["rejected"],
            readback["missing_issue_closed"],
            readback["child_issue_count"],
            readback["new_candidate"],
            readback["distinct_bound_targets"],
        ) == (377, 375, 2, 372, 4, 370, 370)
        assert readback["split_lineage"]["valid"] is True
        assert applied["other_papers_unchanged"] is True
        assert applied["audit_counts_delta"] == {
            "reconciled_identity_split_v2": 2,
            "reconciled_materialized_verified_result_v2": 368,
        }
        session.commit()
    with Session(setup_test_db) as session:
        before_counts = (
            session.query(DFTResult).count(),
            session.query(DFTAuditIssue).count(),
            session.query(DFTAuditIssueSource).count(),
            session.query(AuditLog).count(),
        )
        repeated = DFTB0102ReconciliationService(session).reconcile(
            manifest=manifest,
            expected_manifest_sha256=B0102_MANIFEST_CANONICAL_SHA256,
            expected_database_fingerprint=B0102_PRE_APPLY_DATABASE_FINGERPRINT,
            expected_pdf_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
            pdf_preflight_fingerprint=B0102_PDF_SNAPSHOT_FINGERPRINT,
        )
        assert repeated["status"] == "already_reconciled"
        assert repeated["database_writes"] == 0
        after_counts = (
            session.query(DFTResult).count(),
            session.query(DFTAuditIssue).count(),
            session.query(DFTAuditIssueSource).count(),
            session.query(AuditLog).count(),
        )
        assert after_counts == before_counts

        conflict = DFTAuditIssue(
            paper_id=UUID("0ed01979-08b6-4fa2-9d24-81ef54c71aef"),
            target_type="dft_results",
            target_id="new",
            issue_type="duplicate_suspected",
            severity="high",
            status="needs_user_decision",
            lifecycle_stage="binding_conflict",
            last_error_code="legacy_false_dedupe_requires_identity_split",
            fingerprint=uuid4().hex,
        )
        session.add(conflict)
        session.flush()
        conflicted = DFTB0102ReconciliationService(session).readback(require_final=False)
        assert conflicted["identity_conflict"] == 1
        assert conflicted["is_exact_final_state"] is False
        session.rollback()
