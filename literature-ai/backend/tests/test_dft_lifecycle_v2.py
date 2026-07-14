from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTAuditIssue,
    DFTAuditIssueSource,
    DFTResult,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperRelationship,
    utcnow,
)
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_audit_issue_repair_service import DFTAuditIssueRepairService
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_identity_service import build_dft_identity_v2
from app.services.dft_import_batch_context import DFTImportBatchContext
from app.services.dft_material_binding_service import DFTMaterialBindingService
from app.services.dft_review_service import DFTResultReviewService
from app.services.verification_session_service import VerificationSessionService


def _paper(session: Session, title: str = "DFT lifecycle v2") -> Paper:
    row = Paper(title=title, pdf_path=f"{uuid4()}.pdf", authors=["A"])
    session.add(row)
    session.flush()
    return row


def _candidate(
    session: Session,
    paper: Paper,
    *,
    corrected: dict,
    page: int = 4,
) -> ExternalAnalysisCandidate:
    run = session.scalar(
        sa.select(ExternalAnalysisRun).where(ExternalAnalysisRun.paper_id == paper.id)
    )
    if run is None:
        run = ExternalAnalysisRun(paper_id=paper.id, source="ide_ai", source_label="p1b1-test")
        session.add(run)
        session.flush()
    payload = {
        "target_type": "dft_results",
        "target_id": "new",
        "field_name": "dft_results",
        "decision": "new_candidate",
        "corrected_value": corrected,
        "evidence_location": {
            "source_document_type": "main_text",
            "page": page,
            "quoted_text": f"DFT evidence on page {page}",
        },
    }
    candidate = ExternalAnalysisCandidate(
        run_id=run.id,
        paper_id=paper.id,
        candidate_type="object_review_audit",
        normalized_payload=payload,
        status="candidate",
    )
    session.add(candidate)
    session.flush()
    return candidate


def _identity_payload(paper_id, *, value: float = -1.2, material: str = "Fe-GDY") -> dict:
    return {
        "paper_id": str(paper_id),
        "corrected_value": {
            "material": material,
            "adsorbate": "Li2S4",
            "property_type": "adsorption_energy",
            "reaction_step": "Li2S4 adsorption",
            "value": value,
            "unit": "eV",
        },
    }


def _v2_row(session: Session, paper: Paper, *, value: float = -1.2, material: str = "Fe-GDY") -> DFTResult:
    sample = CatalystSample(paper_id=paper.id, name=material)
    session.add(sample)
    session.flush()
    row = DFTResult(
        paper_id=paper.id,
        catalyst_sample_id=sample.id,
        property_type="adsorption_energy",
        adsorbate="Li2S4",
        reaction_step="Li2S4 adsorption",
        value=value,
        unit="eV",
        evidence_text="PDF evidence",
        evidence_payload={"material_identity": material, "page": 4},
        candidate_status="new_candidate",
        candidate_identity=uuid4().hex,
    )
    identity = build_dft_identity_v2(_identity_payload(paper.id, value=value, material=material))
    DFTAuditIssueLifecycleService.apply_result_identity(row, identity)
    session.add(row)
    session.flush()
    return row


def test_legacy_result_transient_identity_uses_only_explicit_reaction_pathway(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, title="Legacy reaction identity")
        sample = CatalystSample(paper_id=paper.id, name="FeN4-G")
        session.add(sample)
        session.flush()
        common = {
            "paper_id": paper.id,
            "catalyst_sample_id": sample.id,
            "adsorbate": "Li2S",
            "property_type": "reaction_barrier",
            "reaction_step": "Li2S dissociation",
            "value": 1.7,
            "unit": "eV",
            "candidate_status": "ai_verified_ml_ready",
        }
        explicit = DFTResult(**common, reaction_type="SRR_LiS", evidence_payload={"page": 5})
        missing = DFTResult(**common, reaction_type=None, evidence_payload={"page": 5})
        session.add_all([explicit, missing])
        session.flush()

        lifecycle = DFTAuditIssueLifecycleService(session)
        explicit_payload = lifecycle.authoritative_payload_for_result(explicit)
        explicit_identity = lifecycle.identity_for_result(explicit)
        missing_identity = lifecycle.identity_for_result(missing)

        assert explicit_payload["corrected_value"]["reaction_type"] == "SRR_LiS"
        assert explicit_identity.observation_key
        assert explicit_identity.identity_payload["subject"]["property_context"]["pathway"] == "srr_lis"
        assert missing_identity.observation_key is None
        assert "missing_state_context_identity" in missing_identity.error_codes


def test_materialization_double_writes_v1_v2_issue_and_source(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session)
        first = _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.2,
                "unit": "eV",
            },
            page=4,
        )
        second = _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "adsorption of Li2S4",
                "value": -1.2,
                "unit": "eV",
            },
            page=99,
        )

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest-ai",
        )

        assert [item["action"] for item in result["materialized_items"]] == ["created", "deduplicated"]
        row = session.scalar(sa.select(DFTResult).where(DFTResult.paper_id == paper.id))
        issue = session.scalar(sa.select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper.id))
        assert row is not None and issue is not None
        assert row.identity_version == 2
        assert row.subject_key and row.observation_key and row.identity_payload
        assert row.candidate_identity
        assert issue.result_id == row.id
        assert issue.target_id == str(row.id)
        assert issue.issue_key_version == 2 and issue.issue_key
        assert issue.lifecycle_version == 2
        assert issue.lifecycle_stage == "pending_verification"
        assert issue.status == "fixed_by_primary_ai"
        assert set(issue.source_candidate_ids) == {str(first.id), str(second.id)}
        sources = session.scalars(
            sa.select(DFTAuditIssueSource).where(DFTAuditIssueSource.issue_id == issue.id)
        ).all()
        assert {source.candidate_id for source in sources} == {first.id, second.id}


def test_materialize_then_ai_verify_closes_only_after_export_gate(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "AI verify lifecycle")
        _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.2,
                "unit": "eV",
            },
        )
        materialized = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest-ai",
        )
        row_id = materialized["materialized_items"][0]["dft_result_id"]
        issue_id = materialized["materialized_items"][0]["issue_id"]

        verified = DFTResultReviewService(session).verify_result(
            paper_id=paper.id,
            result_id=UUID(row_id),
            confirm_reviewed_against_pdf=True,
            reviewer="pytest-ai",
            field_names=["value"],
            verification_actor_type="ai",
            source_label="local_ai",
            evidence_payload={"page": 4, "quoted_text": "DFT evidence on page 4"},
            commit=False,
        )
        issue = session.get(DFTAuditIssue, UUID(issue_id))
        assert verified["actor_type"] == "ai"
        assert verified["export_safety"]["eligible"] is True
        assert issue.status == "closed"
        assert issue.resolution_code == "verified"
        assert issue.retry_count == 0 and issue.next_retry_at is None


def test_export_gate_failure_keeps_issue_open_then_success_clears_retry(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Gate retry lifecycle")
        row = _v2_row(session, paper)
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(row.id),
            result_id=row.id,
            issue_type="missing_dft_result",
            severity="high",
            status="fixed_by_primary_ai",
            fingerprint=uuid4().hex,
            retry_count=2,
            next_retry_at=utcnow() + timedelta(hours=1),
        )
        session.add(issue)
        session.flush()
        lifecycle = DFTAuditIssueLifecycleService(session)

        assert lifecycle.apply_verify(
            paper_id=paper.id,
            result_id=row.id,
            reviewer="pytest-ai",
            actor_type="ai",
            export_gate_passed=False,
        ) == []
        assert issue.status == "fixed_by_primary_ai"
        assert issue.lifecycle_stage == "verification_failed"
        assert issue.last_error_code == "export_gate_failed"
        assert issue.retry_count == 3

        closed = lifecycle.apply_verify(
            paper_id=paper.id,
            result_id=row.id,
            reviewer="pytest-ai",
            actor_type="ai",
            export_gate_passed=True,
        )
        assert [row.id for row in closed] == [issue.id]
        assert issue.status == "closed"
        assert issue.retry_count == 0
        assert issue.next_retry_at is None


@pytest.mark.parametrize("terminal_status", ["closed", "false_positive"])
def test_terminal_bind_is_true_noop_for_issue_and_source_relation(setup_test_db, terminal_status):
    with Session(setup_test_db) as session:
        paper = _paper(session, f"Terminal {terminal_status}")
        candidate = _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "value": -1.2,
                "unit": "eV",
            },
        )
        row = _v2_row(session, paper)
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            severity="high",
            status=terminal_status,
            source_candidate_ids=[str(candidate.id)],
            fingerprint=uuid4().hex,
            resolution_note=terminal_status,
        )
        session.add(issue)
        session.flush()

        lifecycle = DFTAuditIssueLifecycleService(session)
        lifecycle.reconcile_candidate_binding(
            candidate=candidate,
            issue=issue,
            row=row,
            identity=lifecycle.identity_for_result(row),
            repaired_by="pytest",
            resolution_note="ordinary_reimport",
        )

        assert issue.result_id is None
        assert issue.target_id == "new"
        assert issue.issue_key is None and issue.lifecycle_version is None
        assert candidate.status == "candidate"
        assert candidate.materialized_target_type is None
        assert candidate.materialized_target_id is None
        assert session.scalar(sa.select(sa.func.count()).select_from(DFTAuditIssueSource)) == 0


def test_materialization_terminal_issue_skips_all_entrypoint_side_effects(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Terminal materialize no-op")
        support_paper = _paper(session, "Terminal support source")
        support_row = _v2_row(session, support_paper, material="Support-Fe-GDY")
        support_row.support_lifecycle_status = "pending"
        support_row.support_writeback_paper_id = paper.id
        support_row.support_lifecycle_reason = "linked_supplementary_candidate"
        support_row.support_lifecycle_actor = "pytest"
        session.add(
            PaperRelationship(
                source_paper_id=paper.id,
                target_paper_id=support_paper.id,
                relationship_type="supplementary",
                created_by="pytest",
            )
        )
        candidate = _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.2,
                "unit": "eV",
                "source_dft_result_id": str(support_row.id),
                "source_paper_id": str(support_paper.id),
            },
        )
        payload = deepcopy(candidate.normalized_payload)
        issue_service = DFTAuditIssueService(session)
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            severity="high",
            status="closed",
            fingerprint=issue_service.fingerprint_missing_issue(
                paper_id=paper.id,
                payload=payload,
                candidate_id=str(candidate.id),
            ),
            resolution_note="already_terminal",
        )
        session.add(issue)
        session.flush()

        candidate_before = {
            attr.key: deepcopy(getattr(candidate, attr.key))
            for attr in sa.inspect(candidate).mapper.column_attrs
        }
        issue_before = {
            attr.key: deepcopy(getattr(issue, attr.key))
            for attr in sa.inspect(issue).mapper.column_attrs
        }
        support_before = {
            field: deepcopy(getattr(support_row, field))
            for field in (
                "support_lifecycle_status",
                "support_writeback_paper_id",
                "support_writeback_dft_result_id",
                "support_lifecycle_reason",
                "support_lifecycle_actor",
                "support_lifecycle_updated_at",
            )
        }
        main_result_count = session.scalar(
            sa.select(sa.func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)
        )
        main_sample_count = session.scalar(
            sa.select(sa.func.count(CatalystSample.id)).where(CatalystSample.paper_id == paper.id)
        )
        source_count = session.scalar(sa.select(sa.func.count()).select_from(DFTAuditIssueSource))
        support_log_count = session.scalar(
            sa.select(sa.func.count(AuditLog.id)).where(
                AuditLog.paper_id == paper.id,
                AuditLog.action == "resolve_supplementary_dft_candidate",
            )
        )

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest-ai",
        )

        assert result["materialized_count"] == 0
        assert result["skipped_items"] == [
            {"candidate_id": str(candidate.id), "reason": "terminal_dft_audit_issue"}
        ]
        assert {
            attr.key: deepcopy(getattr(candidate, attr.key))
            for attr in sa.inspect(candidate).mapper.column_attrs
        } == candidate_before
        assert {
            attr.key: deepcopy(getattr(issue, attr.key))
            for attr in sa.inspect(issue).mapper.column_attrs
        } == issue_before
        assert session.scalar(
            sa.select(sa.func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)
        ) == main_result_count
        assert session.scalar(
            sa.select(sa.func.count(CatalystSample.id)).where(CatalystSample.paper_id == paper.id)
        ) == main_sample_count
        assert session.scalar(sa.select(sa.func.count()).select_from(DFTAuditIssueSource)) == source_count
        assert {
            field: deepcopy(getattr(support_row, field)) for field in support_before
        } == support_before
        assert session.scalar(
            sa.select(sa.func.count(AuditLog.id)).where(
                AuditLog.paper_id == paper.id,
                AuditLog.action == "resolve_supplementary_dft_candidate",
            )
        ) == support_log_count


def test_materialization_terminal_issue_still_reports_binding_conflict(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Terminal materialize conflict")
        conflicting_row = _v2_row(session, paper, value=-2.0)
        candidate = _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.2,
                "unit": "eV",
            },
        )
        payload = deepcopy(candidate.normalized_payload)
        issue_service = DFTAuditIssueService(session)
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(conflicting_row.id),
            result_id=conflicting_row.id,
            issue_type="missing_dft_result",
            severity="high",
            status="closed",
            fingerprint=issue_service.fingerprint_missing_issue(
                paper_id=paper.id,
                payload=payload,
                candidate_id=str(candidate.id),
            ),
            resolution_note="already_terminal",
        )
        session.add(issue)
        session.flush()
        before_result_count = session.scalar(
            sa.select(sa.func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)
        )

        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest-ai",
        )

        assert result["materialized_count"] == 0
        assert result["skipped_items"] == [
            {
                "candidate_id": str(candidate.id),
                "reason": "dft_audit_issue_bound_to_different_result",
            }
        ]
        assert session.scalar(
            sa.select(sa.func.count(DFTResult.id)).where(DFTResult.paper_id == paper.id)
        ) == before_result_count
        assert candidate.materialized_target_type is None
        assert candidate.materialized_target_id is None
        assert issue.status == "closed" and issue.result_id == conflicting_row.id


def test_result_id_is_authoritative_over_conflicting_legacy_target_for_repair_and_verify(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "V2 relationship authority")
        authoritative = _v2_row(session, paper, value=-1.2, material="Fe-GDY")
        legacy_target = _v2_row(session, paper, value=-2.3, material="Co-GDY")
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id=str(legacy_target.id),
            result_id=authoritative.id,
            issue_type="wrong_value",
            severity="medium",
            status="needs_primary_ai",
            current_snapshot=DFTAuditIssueLifecycleService.snapshot_dft_result(authoritative),
            evidence_payload={"page": 4, "quoted_text": "corrected value"},
            fingerprint=uuid4().hex,
        )
        session.add(issue)
        session.flush()

        repaired = DFTAuditIssueRepairService(session).repair_issue(
            issue_id=issue.id,
            action="update_dft_fields",
            repair_payload={"fields": {"value": -1.25}},
            reason="PDF correction",
            evidence_payload={"page": 4, "quoted_text": "corrected value"},
            repaired_by="pytest-ai",
        )
        assert repaired["dft_result_id"] == str(authoritative.id)
        assert authoritative.value == -1.25
        assert legacy_target.value == -2.3

        closed = DFTAuditIssueLifecycleService(session).apply_verify(
            paper_id=paper.id,
            result_id=authoritative.id,
            reviewer="pytest-ai",
            actor_type="ai",
            export_gate_passed=True,
        )
        assert [row.id for row in closed] == [issue.id]


def test_historical_null_v2_is_transiently_comparable_without_backfill(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Historical null v2")
        sample = CatalystSample(paper_id=paper.id, name="Fe-GDY")
        session.add(sample)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=sample.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            reaction_step="Li2S4 adsorption",
            value=-1.2,
            unit="eV",
            evidence_payload={"material_identity": "Fe-GDY"},
            candidate_identity=uuid4().hex,
        )
        session.add(row)
        session.flush()

        lifecycle = DFTAuditIssueLifecycleService(session)
        identity = lifecycle.identity_for_result(row)
        exact, conflicts = lifecycle.classify_result_identity(
            paper_id=paper.id,
            identity=identity,
            rows=[row],
        )
        assert exact == row and conflicts == []
        session.flush()
        assert row.identity_version is None
        assert row.subject_key is None and row.observation_key is None and row.identity_payload is None


def test_invalid_identity_candidates_remain_distinct_and_open(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Invalid identity isolation")
        for page in (4, 5):
            _candidate(
                session,
                paper,
                corrected={
                    "material": "Li-S host",
                    "property_type": "bond_length",
                    "value": 2.4,
                    "unit": "Å",
                },
                page=page,
            )
        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest-ai",
        )
        rows = session.scalars(sa.select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        issues = session.scalars(sa.select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper.id)).all()
        assert [item["action"] for item in result["materialized_items"]] == ["created", "created"]
        assert len(rows) == 2 and all(row.observation_key is None for row in rows)
        assert len({row.candidate_identity for row in rows}) == 2
        assert len(issues) == 2
        assert all(issue.status == "fixed_by_primary_ai" for issue in issues)
        assert all(issue.issue_key is None for issue in issues)
        assert all(issue.lifecycle_stage == "verification_failed" for issue in issues)
        assert all(issue.resolution_code == "invalid_identity" for issue in issues)
        assert all(issue.last_error_code == "missing_atom_pair_identity" for issue in issues)


def test_dimensionless_candidate_without_unit_uses_central_policy(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Dimensionless identity")
        _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "property_type": "coordination_number",
                "value": 4,
            },
        )
        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest-ai",
        )
        row = session.scalar(sa.select(DFTResult).where(DFTResult.paper_id == paper.id))
        assert result["materialized_count"] == 1
        assert row.unit is None
        assert row.identity_version == 2 and row.observation_key


def test_two_invalid_repair_issues_do_not_reuse_legacy_candidate_identity(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Invalid repair isolation")
        issues = []
        for index in range(2):
            issue = DFTAuditIssue(
                paper_id=paper.id,
                target_type="dft_results",
                target_id="new",
                issue_type="missing_dft_result",
                severity="high",
                status="needs_primary_ai",
                suggested_dft={
                    "material_identity": "Fe-GDY",
                    "adsorbate": "Li2S4",
                    "property_type": "adsorption_energy",
                    "value": -1.2,
                    "unit": "unsupported-unit",
                },
                evidence_payload={"page": index + 4, "quoted_text": "DFT evidence"},
                fingerprint=uuid4().hex,
            )
            session.add(issue)
            issues.append(issue)
        session.flush()

        created = [
            DFTAuditIssueRepairService(session).repair_issue(
                issue_id=issue.id,
                action="create_missing_dft",
                repair_payload={},
                reason="isolated invalid identity",
                evidence_payload=issue.evidence_payload,
                repaired_by="pytest-ai",
            )
            for issue in issues
        ]
        rows = session.scalars(sa.select(DFTResult).where(DFTResult.paper_id == paper.id)).all()
        assert len({item["dft_result_id"] for item in created}) == 2
        assert len(rows) == 2
        assert all(row.observation_key is None for row in rows)
        assert len({row.candidate_identity for row in rows}) == 2
        assert all(issue.lifecycle_stage == "verification_failed" for issue in issues)
        assert all(issue.resolution_code == "invalid_identity" for issue in issues)


def test_dimensionless_repair_without_unit_can_create_v2_result(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Dimensionless repair")
        issue = DFTAuditIssue(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="new",
            issue_type="missing_dft_result",
            severity="high",
            status="needs_primary_ai",
            suggested_dft={
                "material_identity": "Fe-GDY",
                "property_type": "coordination_number",
                "value": 4,
            },
            evidence_payload={"page": 4, "quoted_text": "coordination number is four"},
            fingerprint=uuid4().hex,
        )
        session.add(issue)
        session.flush()

        created = DFTAuditIssueRepairService(session).repair_issue(
            issue_id=issue.id,
            action="create_missing_dft",
            repair_payload={},
            reason="dimensionless DFT result",
            evidence_payload=issue.evidence_payload,
            repaired_by="pytest-ai",
        )
        row = session.get(DFTResult, UUID(created["dft_result_id"]))
        assert row.unit is None
        assert row.identity_version == 2 and row.observation_key
        assert issue.lifecycle_stage == "pending_verification"


def test_rejected_result_cannot_be_revived_by_verify(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Rejected is terminal")
        row = _v2_row(session, paper)
        row.candidate_status = "Rejected"
        session.flush()
        with pytest.raises(ValueError, match="rejected_dft_result_cannot_be_verified"):
            DFTResultReviewService(session).verify_result(
                paper_id=paper.id,
                result_id=row.id,
                confirm_reviewed_against_pdf=True,
                reviewer="pytest",
            )


def test_entrypoints_delegate_v2_writes_to_unified_lifecycle_service():
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    candidate_source = (root / "verification_session_candidates.py").read_text(encoding="utf-8")
    repair_source = (root / "dft_audit_issue_repair_service.py").read_text(encoding="utf-8")
    issue_source = (root / "dft_audit_issue_service.py").read_text(encoding="utf-8")
    review_source = (root / "dft_review_service.py").read_text(encoding="utf-8")
    assert "reconcile_candidate_binding(" in candidate_source
    assert "apply_result_identity(" in repair_source
    assert "initialize_issue_identity(" in issue_source
    assert "issue_lifecycle.apply_verify(" in review_source
    for source in (candidate_source, repair_source, issue_source, review_source):
        assert ".identity_version = 2" not in source
        assert "row.subject_key =" not in source
        assert "row.observation_key =" not in source


def test_import_batch_overlay_discards_rolled_back_issue_and_source_cache(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Batch overlay rollback")
        candidate = _candidate(
            session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.2,
                "unit": "eV",
            },
        )
        run = session.get(ExternalAnalysisRun, candidate.run_id)
        payload = dict(candidate.normalized_payload)
        issue_service = DFTAuditIssueService(session)
        issue_type, fingerprint, *_ = issue_service.missing_issue_batch_key(
            paper_id=paper.id,
            candidate=candidate,
            payload=payload,
        )
        context = issue_service.begin_import_batch(
            paper_id=paper.id,
            issue_fingerprints={(issue_type, fingerprint)},
            candidates_by_id={candidate.id: candidate},
        )
        context.locked_candidate_ids.add(candidate.id)
        materializer = VerificationSessionService(session, get_settings())

        relation_key = None
        with pytest.raises(RuntimeError, match="rollback_overlay"):
            with materializer._dft_import_savepoint(context):
                issue = issue_service.create_or_update_missing_issue(
                    paper_id=paper.id,
                    candidate=candidate,
                    run=run,
                    payload=payload,
                )
                relation_key = (issue.id, candidate.id)
                assert context.issue_by_fingerprint((issue_type, fingerprint)) is issue
                assert context.source_relation_exists(relation_key)
                raise RuntimeError("rollback_overlay")

        assert context.overlay is None
        assert context.issue_by_fingerprint((issue_type, fingerprint)) is None
        assert relation_key is not None and not context.source_relation_exists(relation_key)
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper.id)
        ) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(DFTAuditIssueSource)) == 0

        with materializer._dft_import_savepoint(context):
            issue = issue_service.create_or_update_missing_issue(
                paper_id=paper.id,
                candidate=candidate,
                run=run,
                payload=payload,
            )
            lifecycle = DFTAuditIssueLifecycleService(session, batch_context=context)
            assert lifecycle.bind_source_candidate(issue, candidate) is False

        assert context.issue_by_fingerprint((issue_type, fingerprint)) is issue
        assert context.source_relation_exists((issue.id, candidate.id))
        assert session.scalar(sa.select(sa.func.count()).select_from(DFTAuditIssueSource)) == 1
        issue_service.end_import_batch()


def test_batch_and_nonbatch_missing_issue_upserts_keep_same_semantics(setup_test_db):
    with Session(setup_test_db) as session:
        rows = []
        for title in ("Nonbatch semantics", "Batch semantics"):
            paper = _paper(session, title)
            candidate = _candidate(
                session,
                paper,
                corrected={
                    "material": "Fe-GDY",
                    "adsorbate": "Li2S4",
                    "property_type": "adsorption_energy",
                    "reaction_step": "Li2S4 adsorption",
                    "value": -1.2,
                    "unit": "eV",
                },
            )
            rows.append((paper, candidate, session.get(ExternalAnalysisRun, candidate.run_id)))

        nonbatch_paper, nonbatch_candidate, nonbatch_run = rows[0]
        nonbatch_issue = DFTAuditIssueService(session).create_or_update_missing_issue(
            paper_id=nonbatch_paper.id,
            candidate=nonbatch_candidate,
            run=nonbatch_run,
            payload=dict(nonbatch_candidate.normalized_payload),
        )

        batch_paper, batch_candidate, batch_run = rows[1]
        batch_service = DFTAuditIssueService(session)
        batch_payload = dict(batch_candidate.normalized_payload)
        issue_type, fingerprint, *_ = batch_service.missing_issue_batch_key(
            paper_id=batch_paper.id,
            candidate=batch_candidate,
            payload=batch_payload,
        )
        context = batch_service.begin_import_batch(
            paper_id=batch_paper.id,
            issue_fingerprints={(issue_type, fingerprint)},
            candidates_by_id={batch_candidate.id: batch_candidate},
        )
        with VerificationSessionService(session, get_settings())._dft_import_savepoint(context):
            batch_issue = batch_service.create_or_update_missing_issue(
                paper_id=batch_paper.id,
                candidate=batch_candidate,
                run=batch_run,
                payload=batch_payload,
            )

        assert (
            batch_issue.issue_type,
            batch_issue.status,
            batch_issue.severity,
            batch_issue.lifecycle_version,
            batch_issue.lifecycle_stage,
            batch_issue.resolution_note,
        ) == (
            nonbatch_issue.issue_type,
            nonbatch_issue.status,
            nonbatch_issue.severity,
            nonbatch_issue.lifecycle_version,
            nonbatch_issue.lifecycle_stage,
            nonbatch_issue.resolution_note,
        )
        assert batch_issue.source_candidate_ids == [str(batch_candidate.id)]
        assert nonbatch_issue.source_candidate_ids == [str(nonbatch_candidate.id)]
        batch_service.end_import_batch()


def test_batch_materialization_reuses_concurrent_observation_winner(setup_test_db, monkeypatch):
    with Session(setup_test_db) as setup_session:
        paper = _paper(setup_session, "Concurrent observation winner")
        candidate = _candidate(
            setup_session,
            paper,
            corrected={
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.2,
                "unit": "eV",
            },
        )
        paper_id = paper.id
        candidate_id = candidate.id
        setup_session.commit()

    winner_id = None

    def concurrent_insert_then_conflict(self, *, paper_id, candidate_item, source_label):
        nonlocal winner_id
        with Session(setup_test_db) as concurrent_session:
            winner = DFTResult(
                paper_id=paper_id,
                adsorbate=candidate_item["adsorbate"],
                property_type=candidate_item["property_type"],
                value=candidate_item["value"],
                unit=candidate_item["unit"],
                reaction_step=candidate_item["reaction_step"],
                candidate_status="new_candidate",
                evidence_payload=candidate_item["evidence_payload"],
                candidate_identity=f"concurrent-winner:{uuid4()}",
            )
            DFTAuditIssueLifecycleService.apply_result_identity(winner, candidate_item["identity_v2"])
            concurrent_session.add(winner)
            concurrent_session.commit()
            winner_id = winner.id
        original = RuntimeError("concurrent observation")
        original.diag = SimpleNamespace(constraint_name="uq_dft_results_identity_v2_observation")
        raise IntegrityError("concurrent observation", {}, original)

    monkeypatch.setattr(
        VerificationSessionService,
        "_insert_new_dft_candidate_in_current_savepoint",
        concurrent_insert_then_conflict,
    )

    with Session(setup_test_db) as session:
        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper_id,
            reviewer="pytest-ai",
            candidate_ids={candidate_id},
        )
        assert result["materialized_count"] == 1
        assert result["materialized_items"][0]["action"] == "deduplicated"
        assert result["materialized_items"][0]["dft_result_id"] == str(winner_id)
        assert session.scalar(
            sa.select(sa.func.count()).select_from(DFTResult).where(DFTResult.paper_id == paper_id)
        ) == 1


def test_material_binding_cache_discards_savepoint_rollback(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Material cache rollback")
        context = DFTImportBatchContext(paper_id=paper.id)
        binding = DFTMaterialBindingService(session)
        materializer = VerificationSessionService(session, get_settings())

        with pytest.raises(RuntimeError, match="rollback_material"):
            with materializer._dft_import_savepoint(context, binding):
                sample, created = binding.resolve_or_create_sample(
                    paper_id=paper.id,
                    material_identity="Fe-GDY",
                )
                assert created and sample.id is not None
                raise RuntimeError("rollback_material")

        assert session.scalar(
            sa.select(sa.func.count()).select_from(CatalystSample).where(CatalystSample.paper_id == paper.id)
        ) == 0
        with materializer._dft_import_savepoint(context, binding):
            sample, created = binding.resolve_or_create_sample(
                paper_id=paper.id,
                material_identity="Fe-GDY",
            )
            assert created
        assert session.scalar(
            sa.select(sa.func.count()).select_from(CatalystSample).where(CatalystSample.paper_id == paper.id)
        ) == 1
