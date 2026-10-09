from __future__ import annotations

import base64
from datetime import datetime, timedelta
import json
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AuditLog, DFTAuditIssue, DFTResult, EvidenceSpan, ExtractionFieldReview, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper
from app.main import app
from app.services.dft_audit_issue_service import DFTAuditIssueCursorError, DFTAuditIssueService
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.dft_identity_service import build_dft_scientific_identity, resolve_atom_pair_identity
from app.services.dft_review_service import DFTResultReviewService
from app.services.verification_session_service import VerificationSessionService


def _paper(session: Session, title: str = "DFT audit issue paper") -> Paper:
    pdf_path = f"{title}.pdf"
    resolved_pdf = Path(get_settings().storage_root) / pdf_path
    resolved_pdf.parent.mkdir(parents=True, exist_ok=True)
    resolved_pdf.write_bytes(b"%PDF-1.4\nDFT audit test\n%%EOF\n")
    paper = Paper(title=title, pdf_path=pdf_path)
    session.add(paper)
    session.flush()
    return paper


def _run(session: Session, paper: Paper, source_label: str) -> ExternalAnalysisRun:
    run = ExternalAnalysisRun(
        paper_id=paper.id,
        source="ide_ai",
        source_label=source_label,
        raw_payload={},
        normalized_payload={},
        mapping_status="mapped",
    )
    session.add(run)
    session.flush()
    return run


def _candidate(session: Session, paper: Paper, run: ExternalAnalysisRun, payload: dict, status: str = "pending") -> ExternalAnalysisCandidate:
    candidate = ExternalAnalysisCandidate(
        run_id=run.id,
        paper_id=paper.id,
        candidate_type="object_review_audit",
        normalized_payload=payload,
        status=status,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _dft_row(session: Session, paper: Paper, *, status: str = "system_candidate") -> DFTResult:
    row = DFTResult(
        paper_id=paper.id,
        property_type="adsorption_energy",
        adsorbate="Li2S4",
        value=-1.20,
        unit="eV",
        evidence_text="Table 1 reports -1.20 eV.",
        candidate_status=status,
    )
    session.add(row)
    session.flush()
    session.add(
        EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id=str(row.id),
            text=row.evidence_text,
            page=4,
        )
    )
    session.flush()
    return row


@pytest.mark.parametrize(
    ("container", "alias"),
    [
        ("payload", "atom_pair"),
        ("corrected_value", "bond_pair"),
        ("evidence_payload", "bond"),
        ("evidence_location", "interaction_pair"),
    ],
)
def test_atom_pair_identity_reads_all_aliases_from_all_supported_sources(container, alias):
    payload = {"corrected_value": {"property_type": "bond_length"}}
    target = payload if container == "payload" else payload.setdefault(container, {})
    target[alias] = " Li1 – S "

    identity = resolve_atom_pair_identity(payload)

    assert identity.canonical == "li1-s"
    assert identity.error_code is None


def test_atom_pair_identity_preserves_site_number_and_is_symmetric_only_for_bond_properties():
    li1 = build_dft_scientific_identity(
        {"corrected_value": {"material": "Fe-GDY", "property_type": "bond_length", "value": 2.1, "unit": "Å", "atom_pair": "Li1-S"}}
    )
    reversed_li1 = build_dft_scientific_identity(
        {"corrected_value": {"material": "Fe-GDY", "property_type": "bond_length", "value": 2.1, "unit": "Å", "bond": "S - Li1"}}
    )
    li2 = build_dft_scientific_identity(
        {"corrected_value": {"material": "Fe-GDY", "property_type": "bond_length", "value": 2.1, "unit": "Å", "interaction_pair": "Li2-S"}}
    )
    directional_forward = resolve_atom_pair_identity(
        {"corrected_value": {"property_type": "directional_charge_transfer", "atom_pair": "Li1-S"}}
    )
    directional_reverse = resolve_atom_pair_identity(
        {"corrected_value": {"property_type": "directional_charge_transfer", "atom_pair": "S-Li1"}}
    )

    assert li1.observation_signature == reversed_li1.observation_signature
    assert li1.subject_signature != li2.subject_signature
    assert directional_forward.canonical != directional_reverse.canonical


def test_atom_pair_identity_rejects_conflicting_aliases():
    identity = resolve_atom_pair_identity(
        {
            "corrected_value": {"property_type": "ICOHP", "atom_pair": "Li1-S"},
            "evidence_payload": {"bond_pair": "Li2-S"},
        }
    )

    assert identity.canonical is None
    assert identity.error_code == "conflicting_atom_pair_aliases"


def test_interval_observation_identity_normalizes_bounds_and_value_kind():
    base = {
        "corrected_value": {
            "material": "FePc@WS2",
            "adsorbate": "Li2S4",
            "property_type": "pdos_overlap_energy_window",
            "value": "-2.5000",
            "value_upper": "-0.500",
            "value_kind": "Energy Window",
            "unit": "eV",
        }
    }
    same = {
        "corrected_value": {
            **base["corrected_value"],
            "value": -2.5,
            "value_upper": -0.5,
            "value_kind": "energy-window",
        }
    }
    different_upper = {
        "corrected_value": {**base["corrected_value"], "value_upper": -0.4}
    }

    first = build_dft_scientific_identity(base)
    assert first.observation_signature == build_dft_scientific_identity(same).observation_signature
    assert first.subject_signature == build_dft_scientific_identity(different_upper).subject_signature
    assert first.observation_signature != build_dft_scientific_identity(different_upper).observation_signature


def test_point_range_and_energy_window_are_distinct_observations_with_same_bounds():
    def identity(value_kind: str):
        return build_dft_scientific_identity(
            {
                "corrected_value": {
                    "material": "FePc@WS2",
                    "property_type": "pdos_overlap",
                    "value": -2.5,
                    "value_upper": -0.5,
                    "value_kind": value_kind,
                    "unit": "eV",
                }
            }
        )

    signatures = {identity(kind).observation_signature for kind in ("point", "range", "energy_window")}
    assert len(signatures) == 3


def test_missing_issue_fingerprint_ignores_locator_provenance(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Missing issue locator-independent identity")
        base = {
            "target_type": "dft_results",
            "target_id": "new",
            "decision": "new_candidate",
            "corrected_value": {
                "material": "Fe-GDY",
                "property_type": "bond_length",
                "value": 2.1,
                "unit": "Å",
                "atom_pair": "Li1-S",
            },
        }
        service = DFTAuditIssueService(session)

        first = service.fingerprint_missing_issue(
            paper_id=paper.id,
            payload={**base, "evidence_location": {"page": 4, "table": "T1", "source_document_type": "main_text"}},
        )
        second = service.fingerprint_missing_issue(
            paper_id=paper.id,
            payload={**base, "evidence_location": {"page": 9, "table": "T7", "source_document_type": "supplementary_information"}},
        )

        assert first == second


def test_issue_and_candidate_binding_is_idempotent_and_rejects_different_result(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Binding conflict")
        run = _run(session, paper, "binding")
        candidate = _candidate(session, paper, run, {}, status="requires_resolution")
        first = _dft_row(session, paper)
        second = _dft_row(session, paper)
        issue = DFTAuditIssueService(session).upsert_issue(
            paper_id=paper.id,
            target_id="new",
            issue_type="missing_dft_result",
            status="needs_primary_ai",
            fingerprint="binding-conflict",
        )
        lifecycle = DFTAuditIssueLifecycleService(session)

        assert lifecycle.bind_candidate_to_result(candidate, first) is True
        lifecycle.bind_missing_issue_to_result(issue, first, repaired_by="pytest")
        assert lifecycle.bind_candidate_to_result(candidate, first) is False
        lifecycle.bind_missing_issue_to_result(issue, first, repaired_by="pytest")

        with pytest.raises(ValueError, match="dft_candidate_bound_to_different_result"):
            lifecycle.bind_candidate_to_result(candidate, second)
        with pytest.raises(ValueError, match="dft_audit_issue_bound_to_different_result"):
            lifecycle.bind_missing_issue_to_result(issue, second, repaired_by="pytest")


def test_missing_dft_result_issue_is_idempotent_and_merges_sources(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Missing issue dedupe")
        payload = {
            "target_type": "dft_results",
            "target_id": "new",
            "field_name": "dft_results",
            "decision": "new_candidate",
            "corrected_value": {
                "material": "Fe-GDY",
                "adsorbate": "Li2S4",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S4 adsorption",
                "value": -1.1,
                "unit": "eV",
            },
            "evidence_location": {"source_document_type": "main_text", "page": 5, "quoted_text": "Fe-GDY -1.10 eV"},
        }
        first = _candidate(session, paper, _run(session, paper, "ai-1"), payload, status="candidate")
        second = _candidate(session, paper, _run(session, paper, "ai-2"), payload, status="candidate")
        service = DFTAuditIssueService(session)
        for candidate in (first, second):
            run = session.get(ExternalAnalysisRun, candidate.run_id)
            service.create_or_update_missing_issue(
                paper_id=paper.id,
                candidate=candidate,
                run=run,
                payload=candidate.normalized_payload,
            )
        session.flush()

        issues = session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper.id)).all()
        assert len(issues) == 1
        assert issues[0].issue_type == "missing_dft_result"
        assert issues[0].status == "needs_primary_ai"
        assert issues[0].suggested_dft["material_identity"] == "Fe-GDY"
        assert issues[0].source_identities == ["untrusted:external_analysis"]
        assert issues[0].source_candidate_ids == [str(first.id), str(second.id)]


def test_supporting_reference_missing_dft_result_becomes_closed_source_scope_issue(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Supporting scope issue")
        run = _run(session, paper, "ai-1")
        candidate = _candidate(
            session,
            paper,
            run,
            {
                "target_type": "dft_results",
                "target_id": "new",
                "field_name": "dft_results",
                "decision": "new_candidate",
                "corrected_value": {
                    "material": "Fe-GDY",
                    "adsorbate": "Li2S4",
                    "property_type": "adsorption_energy",
                    "reaction_step": "Li2S4 adsorption",
                    "value": -1.1,
                    "unit": "eV",
                },
                "evidence_location": {
                    "source_document_type": "supporting_reference",
                    "page": 8,
                    "quoted_text": "Cited reference reports -1.10 eV.",
                },
            },
            status="candidate",
        )
        result = VerificationSessionService(session, get_settings())._materialize_new_dft_candidates(
            paper_id=paper.id,
            reviewer="pytest",
        )
        issues = session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == paper.id)).all()
        dft_rows = session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all()

        assert result["materialized_count"] == 0
        assert dft_rows == []
        assert len(issues) == 1
        assert issues[0].issue_type == "source_scope_error"
        assert issues[0].status == "closed"
        assert issues[0].source_candidate_ids == [str(candidate.id)]


def test_dft_audit_issues_api_filters_open_paper_issues(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Issue API")
        other = _paper(session, "Other Issue API")
        service = DFTAuditIssueService(session)
        service.upsert_issue(
            paper_id=paper.id,
            target_id="new",
            issue_type="missing_dft_result",
            status="needs_primary_ai",
            fingerprint="api-open",
            suggested_dft={"material_identity": "Fe-GDY"},
        )
        service.upsert_issue(
            paper_id=paper.id,
            target_id="new",
            issue_type="source_scope_error",
            status="closed",
            fingerprint="api-closed",
        )
        service.upsert_issue(
            paper_id=other.id,
            target_id="new",
            issue_type="missing_dft_result",
            status="needs_primary_ai",
            fingerprint="api-other",
        )
        session.commit()
        paper_id = str(paper.id)

    response = TestClient(app).get(f"/api/dft/audit-issues?paper_id={paper_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["issue_type"] == "missing_dft_result"
    assert payload["items"][0]["status"] == "needs_primary_ai"


def test_audit_issue_query_filters_before_limit_and_total_count_matches_pages(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "SQL filtered issue page")
        created = datetime(2026, 7, 14, 10, 0, 0)
        for index in range(8):
            session.add(
                DFTAuditIssue(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id="new",
                    issue_type="wrong_value" if index < 5 else "missing_dft_result",
                    severity="medium",
                    status="needs_primary_ai",
                    fingerprint=f"sql-filter-{index}",
                    created_at=created + timedelta(seconds=index),
                    updated_at=created + timedelta(seconds=index),
                )
            )
        session.flush()
        service = DFTAuditIssueService(session)
        first = service.query_issues(
            paper_id=paper.id,
            statuses={"needs_primary_ai"},
            issue_types={"missing_dft_result"},
            limit=2,
            sort_direction="asc",
        )
        second = service.query_issues(
            paper_id=paper.id,
            statuses={"needs_primary_ai"},
            issue_types={"missing_dft_result"},
            limit=2,
            cursor=first["next_cursor"],
            sort_direction="asc",
        )

        assert first["total_count"] == second["total_count"] == 3
        assert first["returned_count"] == first["count"] == 2
        assert second["returned_count"] == 1
        assert first["has_more"] is True
        assert second["has_more"] is False
        assert all(row.issue_type == "missing_dft_result" for row in [*first["items"], *second["items"]])


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_audit_issue_keyset_tie_breaker_has_no_duplicates_or_omissions(setup_test_db, direction):
    with Session(setup_test_db) as session:
        paper = _paper(session, f"Keyset {direction}")
        created = datetime(2026, 7, 14, 11, 0, 0)
        expected_ids = []
        for index in range(7):
            issue = DFTAuditIssue(
                paper_id=paper.id,
                target_type="dft_results",
                target_id="new",
                issue_type="missing_dft_result",
                severity="high",
                status="needs_primary_ai",
                fingerprint=f"tie-{direction}-{index}",
                created_at=created,
                updated_at=created,
            )
            session.add(issue)
            session.flush()
            expected_ids.append(str(issue.id))
        service = DFTAuditIssueService(session)
        cursor = None
        seen = []
        while True:
            page = service.query_issues(
                paper_id=paper.id,
                issue_types={"missing_dft_result"},
                limit=3,
                cursor=cursor,
                sort_direction=direction,
            )
            seen.extend(str(row.id) for row in page["items"])
            if not page["has_more"]:
                break
            assert page["next_cursor"] not in {None, cursor}
            cursor = page["next_cursor"]

        assert len(seen) == len(set(seen)) == 7
        assert set(seen) == set(expected_ids)
        assert seen == sorted(expected_ids, reverse=direction == "desc")


def test_audit_issue_cursor_rejects_tamper_version_filter_and_sort_mismatch(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Cursor contract")
        for index in range(2):
            session.add(
                DFTAuditIssue(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id="new",
                    issue_type="missing_dft_result",
                    severity="high",
                    status="needs_primary_ai",
                    fingerprint=f"cursor-{index}",
                )
            )
        session.flush()
        service = DFTAuditIssueService(session)
        first = service.query_issues(
            paper_id=paper.id,
            statuses={"needs_primary_ai"},
            issue_types={"missing_dft_result"},
            limit=1,
        )
        cursor = first["next_cursor"]
        assert cursor

        padded = cursor + "=" * (-len(cursor) % 4)
        tampered_envelope = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        tampered_envelope["position"]["id"] = str(UUID(int=0))
        tampered = base64.urlsafe_b64encode(
            service._canonical_json(tampered_envelope).encode("utf-8")
        ).decode("ascii").rstrip("=")
        with pytest.raises(DFTAuditIssueCursorError, match="tampered_dft_audit_issue_cursor"):
            service.query_issues(
                paper_id=paper.id,
                statuses={"needs_primary_ai"},
                issue_types={"missing_dft_result"},
                limit=1,
                cursor=tampered,
            )
        with pytest.raises(DFTAuditIssueCursorError, match="invalid_dft_audit_issue_cursor"):
            service.query_issues(
                paper_id=paper.id,
                statuses={"needs_primary_ai"},
                issue_types={"missing_dft_result"},
                limit=1,
                cursor="not-a-valid-cursor",
            )
        with pytest.raises(DFTAuditIssueCursorError, match="dft_audit_issue_cursor_filter_mismatch"):
            service.query_issues(
                paper_id=paper.id,
                statuses={"needs_user_decision"},
                issue_types={"missing_dft_result"},
                limit=1,
                cursor=cursor,
            )
        with pytest.raises(DFTAuditIssueCursorError, match="dft_audit_issue_cursor_sort_mismatch"):
            service.query_issues(
                paper_id=paper.id,
                statuses={"needs_primary_ai"},
                issue_types={"missing_dft_result"},
                limit=1,
                cursor=cursor,
                sort_direction="asc",
            )

        envelope = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        envelope["v"] = 999
        payload = {key: value for key, value in envelope.items() if key != "checksum"}
        envelope["checksum"] = service._cursor_checksum(payload)
        wrong_version = base64.urlsafe_b64encode(
            service._canonical_json(envelope).encode("utf-8")
        ).decode("ascii").rstrip("=")
        with pytest.raises(DFTAuditIssueCursorError, match="unsupported_dft_audit_issue_cursor_version"):
            service.query_issues(
                paper_id=paper.id,
                statuses={"needs_primary_ai"},
                issue_types={"missing_dft_result"},
                limit=1,
                cursor=wrong_version,
            )


@pytest.mark.parametrize("limit", [0, 201])
def test_audit_issue_query_rejects_limit_outside_contract(setup_test_db, limit):
    with Session(setup_test_db) as session:
        service = DFTAuditIssueService(session)
        with pytest.raises(ValueError, match="between 1 and 200"):
            service.query_issues(limit=limit)


def test_human_verify_closes_eligible_issue_but_not_duplicate_suspected(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Human verify issue lifecycle")
        row = _dft_row(session, paper)
        service = DFTAuditIssueService(session)
        wrong_value = service.upsert_issue(
            paper_id=paper.id,
            target_id=str(row.id),
            issue_type="wrong_value",
            status="needs_primary_ai",
            fingerprint="verify-wrong-value",
            current_snapshot=service.snapshot_dft_result(row),
        )
        duplicate = service.upsert_issue(
            paper_id=paper.id,
            target_id=str(row.id),
            issue_type="duplicate_suspected",
            status="needs_primary_ai",
            fingerprint="verify-duplicate",
            current_snapshot=service.snapshot_dft_result(row),
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id
        wrong_value_id = wrong_value.id
        duplicate_id = duplicate.id

    with Session(setup_test_db) as session:
        result = DFTResultReviewService(session).verify_result(
            paper_id=paper_id,
            result_id=row_id,
            confirm_reviewed_against_pdf=True,
            reviewer="human_reviewer",
            verification_actor_type="human",
            actor_name="owner",
            source_label="owner_api_token",
            field_names=["value"],
        )

    assert str(wrong_value_id) in result["closed_audit_issue_ids"]
    assert str(duplicate_id) not in result["closed_audit_issue_ids"]
    with Session(setup_test_db) as session:
        wrong_value = session.get(DFTAuditIssue, wrong_value_id)
        duplicate = session.get(DFTAuditIssue, duplicate_id)
        assert wrong_value.status == "closed"
        assert wrong_value.resolution_note == "human_verified"
        assert wrong_value.resolved_by == "owner"
        assert duplicate.status == "needs_primary_ai"
        assert duplicate.resolved_at is None


def test_human_reject_closes_target_issues_as_rejected(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Human reject issue lifecycle")
        row = _dft_row(session, paper)
        service = DFTAuditIssueService(session)
        wrong_unit = service.upsert_issue(
            paper_id=paper.id,
            target_id=str(row.id),
            issue_type="wrong_unit",
            status="needs_primary_ai",
            fingerprint="reject-wrong-unit",
            current_snapshot=service.snapshot_dft_result(row),
        )
        uncertain = service.upsert_issue(
            paper_id=paper.id,
            target_id=str(row.id),
            issue_type="uncertain",
            status="needs_user_decision",
            fingerprint="reject-uncertain",
            current_snapshot=service.snapshot_dft_result(row),
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id
        issue_ids = {wrong_unit.id, uncertain.id}

    with Session(setup_test_db) as session:
        result = DFTResultReviewService(session).reject_result(
            paper_id=paper_id,
            result_id=row_id,
            confirm_reject_candidate=True,
            reviewer="human_reviewer",
            verification_actor_type="human",
            actor_name="owner",
            source_label="owner_api_token",
            field_names=["value"],
        )

    assert set(result["closed_audit_issue_ids"]) == {str(issue_id) for issue_id in issue_ids}
    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_id)
        assert row.candidate_status == "Rejected"
        for issue_id in issue_ids:
            issue = session.get(DFTAuditIssue, issue_id)
            assert issue.status == "closed"
            assert issue.resolution_note == "target_rejected"
            assert issue.resolved_by == "owner"


def test_audit_issue_list_returns_live_stale_snapshot(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Stale issue list")
        row = _dft_row(session, paper)
        service = DFTAuditIssueService(session)
        issue = service.upsert_issue(
            paper_id=paper.id,
            target_id=str(row.id),
            issue_type="wrong_value",
            status="needs_primary_ai",
            fingerprint="stale-live",
            current_snapshot=service.snapshot_dft_result(row),
        )
        row.value = -0.95
        session.add(row)
        session.commit()
        paper_id = str(paper.id)
        issue_id = str(issue.id)

    response = TestClient(app).get(f"/api/dft/audit-issues?paper_id={paper_id}")

    assert response.status_code == 200
    item = next(item for item in response.json()["items"] if item["id"] == issue_id)
    assert item["is_stale"] is True
    assert "value" in item["stale_fields"]
    assert item["live_snapshot"]["value"] == -0.95


def test_dft_review_transaction_rolls_back_review_status_audit_and_issue(monkeypatch, setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Rollback review lifecycle")
        row = _dft_row(session, paper)
        service = DFTAuditIssueService(session)
        issue = service.upsert_issue(
            paper_id=paper.id,
            target_id=str(row.id),
            issue_type="wrong_value",
            status="needs_primary_ai",
            fingerprint="rollback-wrong-value",
            current_snapshot=service.snapshot_dft_result(row),
        )
        session.commit()
        paper_id = paper.id
        row_id = row.id
        issue_id = issue.id

    def fail_workflow_job(self, *, paper_id, action, payload):
        raise RuntimeError("workflow job write failed")

    monkeypatch.setattr(DFTResultReviewService, "_add_workflow_job", fail_workflow_job)
    with Session(setup_test_db) as session:
        try:
            DFTResultReviewService(session).verify_result(
                paper_id=paper_id,
                result_id=row_id,
                confirm_reviewed_against_pdf=True,
                reviewer="human_reviewer",
                verification_actor_type="human",
                actor_name="owner",
                source_label="owner_api_token",
                field_names=["value"],
            )
        except RuntimeError:
            session.rollback()
        else:
            raise AssertionError("verify_result should fail")

    with Session(setup_test_db) as session:
        row = session.get(DFTResult, row_id)
        issue = session.get(DFTAuditIssue, issue_id)
        reviews = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.target_id == str(row_id))).all()
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "verify_dft_result"))
        assert row.candidate_status == "system_candidate"
        assert issue.status == "needs_primary_ai"
        assert issue.resolved_at is None
        assert reviews == []
        assert audit is None


def test_dft_audit_issue_can_be_marked_false_positive(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Issue close")
        service = DFTAuditIssueService(session)
        issue = service.upsert_issue(
            paper_id=paper.id,
            target_id="new",
            issue_type="missing_dft_result",
            status="needs_primary_ai",
            fingerprint="close-fp",
            suggested_dft={"material_identity": "Fe-GDY"},
        )

        closed = service.close_issue(
            issue.id,
            status="false_positive",
            resolved_by="pytest",
            resolution_note="AI read a cited reference as main-paper data.",
        )

        assert closed.status == "false_positive"
        assert closed.resolved_by == "pytest"
        assert closed.resolved_at is not None
