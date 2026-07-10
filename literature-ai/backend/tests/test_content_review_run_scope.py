from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from app.db.models import (
    ContentEvidenceItem,
    ContentReviewBundle,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    WorkflowJob,
)
from app.main import app


def _seed_run_scope(engine):
    with Session(engine) as session:
        paper = Paper(
            library_name="内容审核 run 范围测试库",
            paper_code="RS001",
            title="Run scoped content review",
            pdf_path="rs001.pdf",
            authors=[],
        )
        other_paper = Paper(
            library_name="内容审核 run 范围测试库",
            paper_code="RS002",
            title="Other paper",
            pdf_path="rs002.pdf",
            authors=[],
        )
        session.add_all([paper, other_paper])
        session.flush()
        run_a = ExternalAnalysisRun(
            paper_id=paper.id,
            source="web_ai",
            source_label="Run A",
            source_identity="untrusted:http_external_analysis",
            source_identity_verified=False,
            mapping_status="normalized",
        )
        run_b = ExternalAnalysisRun(
            paper_id=paper.id,
            source="ide_ai",
            source_label="Run B",
            source_identity="mcp:local_ide",
            source_identity_verified=True,
            mapping_status="normalized",
        )
        empty_run = ExternalAnalysisRun(
            paper_id=paper.id,
            source="web_ai",
            source_label="Empty Run",
            source_identity="untrusted:http_external_analysis",
            source_identity_verified=False,
            mapping_status="normalized",
        )
        session.add_all([run_a, run_b, empty_run])
        session.flush()
        session.add_all(
            [
                ExternalAnalysisCandidate(run_id=run_a.id, paper_id=paper.id, candidate_type="note", status="pending"),
                ExternalAnalysisCandidate(run_id=run_b.id, paper_id=paper.id, candidate_type="note", status="pending"),
            ]
        )
        session.add_all(
            [
                ContentEvidenceItem(
                    paper_id=paper.id,
                    run_id=run_a.id,
                    category="draft_evidence_check",
                    source_type="external_analysis_candidate",
                    source_id="run-a-1",
                    content="Run A item 1",
                    evidence_text="Run A evidence 1",
                    evidence_locator={"page": 1},
                    page_start=1,
                    review_status="needs_review",
                    citation_status="needs_review",
                ),
                ContentEvidenceItem(
                    paper_id=paper.id,
                    run_id=run_a.id,
                    category="draft_evidence_check",
                    source_type="external_analysis_candidate",
                    source_id="run-a-2",
                    content="Run A item 2",
                    evidence_text="Run A evidence 2",
                    evidence_locator={"page": 2},
                    page_start=2,
                    review_status="needs_review",
                    citation_status="needs_review",
                ),
                ContentEvidenceItem(
                    paper_id=paper.id,
                    run_id=run_b.id,
                    category="draft_evidence_check",
                    source_type="external_analysis_candidate",
                    source_id="run-b-1",
                    content="Run B item 1",
                    evidence_text="Run B evidence 1",
                    evidence_locator={"page": 3},
                    page_start=3,
                    review_status="needs_review",
                    citation_status="needs_review",
                ),
                ContentEvidenceItem(
                    paper_id=paper.id,
                    run_id=run_b.id,
                    category="draft_evidence_check",
                    source_type="external_analysis_candidate",
                    source_id="run-b-2",
                    content="Run B item 2",
                    evidence_text="Run B evidence 2",
                    evidence_locator={"page": 4},
                    page_start=4,
                    review_status="needs_review",
                    citation_status="needs_review",
                ),
                ContentEvidenceItem(
                    paper_id=paper.id,
                    category="writing_material",
                    source_type="paper_note",
                    source_id="paper-level",
                    content="Paper-level content",
                    evidence_text="Paper-level evidence",
                    evidence_locator={"page": 5},
                    page_start=5,
                    review_status="needs_review",
                    citation_status="needs_review",
                ),
            ]
        )
        session.add_all(
            [
                WorkflowJob(
                    job_id="run-a-task",
                    type="agent_activity",
                    status="completed",
                    library_name=paper.library_name,
                    payload={"external_analysis_run_id": str(run_a.id), "paper_id": str(paper.id)},
                    progress={},
                    result={"last_action": "seed", "metrics": {}},
                    runtime_context={},
                ),
                WorkflowJob(
                    job_id="run-b-task",
                    type="agent_activity",
                    status="completed",
                    library_name=paper.library_name,
                    payload={"external_analysis_run_id": str(run_b.id), "paper_id": str(paper.id)},
                    progress={},
                    result={"last_action": "seed", "metrics": {}},
                    runtime_context={},
                ),
            ]
        )
        session.commit()
        return paper.id, other_paper.id, run_a.id, run_b.id, empty_run.id


def test_run_scoped_bundle_isolated_from_other_runs_and_paper_scope(setup_test_db):
    paper_id, other_paper_id, run_a_id, run_b_id, empty_run_id = _seed_run_scope(setup_test_db)
    client = TestClient(app)

    with Session(setup_test_db) as session:
        bundle_before = session.query(ContentReviewBundle).count()

    scoped_knowledge = client.get(
        "/api/content-knowledge",
        params={"paper_id": str(paper_id), "run_id": str(run_a_id), "limit": 50},
    )
    assert scoped_knowledge.status_code == 200
    assert scoped_knowledge.json()["filters"]["run_id"] == str(run_a_id)
    assert {item["content"] for item in scoped_knowledge.json()["items"]} == {"Run A item 1", "Run A item 2"}

    generated = client.post(
        "/api/content-knowledge/review-bundles",
        json={"paper_id": str(paper_id), "run_id": str(run_a_id)},
    )
    assert generated.status_code == 200, generated.text
    payload = generated.json()
    manifest = payload["manifest"]
    assert manifest["scope_type"] == "external_analysis_run"
    assert manifest["run_id"] == str(run_a_id)
    assert manifest["paper_id"] == str(paper_id)
    assert manifest["paper_code"] == "RS001"
    assert manifest["item_count"] == 2
    assert {item["content"] for item in manifest["items"]} == {"Run A item 1", "Run A item 2"}
    assert all("Run B" not in item["content"] for item in manifest["items"])
    assert all(item["content"] != "Paper-level content" for item in manifest["items"])
    assert payload["return_template"]["scope_type"] == "external_analysis_run"
    assert payload["return_template"]["run_id"] == str(run_a_id)
    assert str(run_a_id) in manifest["instructions"]

    mismatch = client.post(
        "/api/content-knowledge/review-bundles",
        json={"paper_id": str(other_paper_id), "run_id": str(run_a_id)},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "content_review_run_not_found_for_paper"
    empty = client.post(
        "/api/content-knowledge/review-bundles",
        json={"paper_id": str(paper_id), "run_id": str(empty_run_id)},
    )
    assert empty.status_code == 400
    assert empty.json()["detail"] == "content_review_no_items_for_run"
    with Session(setup_test_db) as session:
        assert session.query(ContentReviewBundle).count() == bundle_before + 1
        other_run_item = session.scalar(
            select(ContentEvidenceItem).where(ContentEvidenceItem.source_id == "run-b-1")
        )
        other_run_item.content = "Run B changed after Run A bundle generation"
        session.commit()

    result = {
        **payload["return_template"],
        "items": [],
    }
    result["items"] = [
        {"item_id": item["item_id"], "decision": "reject", "evidence_id": item["evidence_id"]}
        for item in manifest["items"]
    ]
    validated = client.post(
        f"/api/content-knowledge/review-bundles/{payload['bundle_id']}/validate", json=result
    )
    assert validated.status_code == 200, validated.text
    applied = client.post(
        f"/api/content-knowledge/review-bundles/{payload['bundle_id']}/apply", json={"reviewer": "test"}
    )
    assert applied.status_code == 200, applied.text
    finalized = client.post(
        f"/api/content-knowledge/review-bundles/{payload['bundle_id']}/finalize", json={"reviewer": "test"}
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["finalized"] is True

    with Session(setup_test_db) as session:
        run_a_items = session.scalars(
            select(ContentEvidenceItem).where(ContentEvidenceItem.run_id == run_a_id)
        ).all()
        run_b_items = session.scalars(
            select(ContentEvidenceItem).where(ContentEvidenceItem.run_id == run_b_id)
        ).all()
        paper_level = session.scalar(
            select(ContentEvidenceItem).where(ContentEvidenceItem.source_id == "paper-level")
        )
        assert all(item.citation_status == "blocked" for item in run_a_items)
        assert all(item.review_status == "needs_review" for item in run_b_items)
        assert paper_level.citation_status == "needs_review"
        task_a = session.get(WorkflowJob, "run-a-task")
        task_b = session.get(WorkflowJob, "run-b-task")
        assert task_a.result["last_action"] == "finalized"
        assert task_b.result["last_action"] == "seed"
