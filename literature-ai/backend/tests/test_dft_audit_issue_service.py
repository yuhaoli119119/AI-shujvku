from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import DFTAuditIssue, DFTResult, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper
from app.main import app
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.verification_session_service import VerificationSessionService


def _paper(session: Session, title: str = "DFT audit issue paper") -> Paper:
    paper = Paper(title=title, pdf_path=f"{title}.pdf")
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


def test_dual_ai_dft_positive_consensus_creates_issue_without_verifying_result(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Positive consensus issue")
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text="Table 1 reports -1.20 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        for label in ("ai-1", "ai-2"):
            run = _run(session, paper, label)
            _candidate(
                session,
                paper,
                run,
                {
                    "target_type": "dft_results",
                    "target_id": str(row.id),
                    "field_name": "value",
                    "decision": "PASS",
                    "corrected_value": -1.20,
                    "confidence": 0.91,
                    "normalized_material": "Fe-GDY",
                    "normalized_energy_type": "adsorption_energy",
                    "evidence_location": {"page": 4, "table": "Table 1", "quoted_text": "-1.20 eV"},
                },
            )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    response = TestClient(app).post(f"/api/papers/{paper_id}/settle-ai-dft-reviews")

    assert response.status_code == 200
    assert response.json()["auto_applied_count"] == 0
    with Session(setup_test_db) as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status == "system_candidate"
        issues = session.scalars(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == UUID(paper_id))).all()
        assert len(issues) == 1
        assert issues[0].issue_type == "consensus_ready"
        assert issues[0].status == "needs_primary_ai"
        assert issues[0].source_identities == ["ai-1", "ai-2"]


def test_dft_negative_consensus_creates_issue_without_rejecting_result(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session, "Negative consensus issue")
        row = DFTResult(
            paper_id=paper.id,
            property_type="adsorption_energy",
            adsorbate="Li2S4",
            value=-1.20,
            unit="eV",
            evidence_text="Table 1 reports -1.20 eV.",
            candidate_status="system_candidate",
        )
        session.add(row)
        session.flush()
        for label in ("ai-1", "ai-2"):
            run = _run(session, paper, label)
            _candidate(
                session,
                paper,
                run,
                {
                    "target_type": "dft_results",
                    "target_id": str(row.id),
                    "field_name": "dft_results",
                    "decision": "REJECT",
                    "confidence": 0.9,
                    "evidence_location": {"page": 5, "quoted_text": "No DFT value appears."},
                },
            )
        session.commit()
        paper_id = str(paper.id)
        row_id = str(row.id)

    response = TestClient(app).post(f"/api/papers/{paper_id}/settle-ai-dft-reviews")

    assert response.status_code == 200
    with Session(setup_test_db) as session:
        stored = session.get(DFTResult, UUID(row_id))
        assert stored.candidate_status == "system_candidate"
        issue = session.scalar(select(DFTAuditIssue).where(DFTAuditIssue.paper_id == UUID(paper_id)))
        assert issue.issue_type == "negative_consensus"
        assert issue.status == "needs_user_decision"


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
        assert issues[0].source_identities == ["ai-1", "ai-2"]
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
