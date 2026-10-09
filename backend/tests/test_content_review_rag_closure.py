from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    ContentEvidenceItem,
    ContentReviewBundle,
    EvidenceClaim,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperSection,
    WorkflowJob,
)
from app.main import app


def _seed(session: Session, tmp_path) -> Paper:
    pdf_path = tmp_path / "CR001.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\nacceptance\n%%EOF\n")
    paper = Paper(
        library_name="内容闭环测试库", paper_code="CR001", title="Fe-N4 Li-S mixed material evidence",
        pdf_path=str(pdf_path), authors=[],
    )
    session.add(paper); session.flush()
    session.add(PaperSection(paper_id=paper.id, section_title="Results", text="Fe-N4 improves LiPS conversion at 2.5 mg cm-2.", page_start=4, page_end=4))
    english = MechanismClaim(
        paper_id=paper.id,
        claim_type="conversion",
        claim_text="Fe-N4 accelerates LiPS conversion",
        evidence_text="Fe-N4 sites accelerate LiPS conversion.",
    )
    chinese = MechanismClaim(
        paper_id=paper.id,
        claim_type="conversion",
        claim_text="Fe-N4 位点促进多硫化物转化",
        evidence_text="结果表明 Fe-N4 位点促进多硫化物转化。",
    )
    session.add_all([english, chinese]); session.flush()
    for claim, page in ((english, 4), (chinese, 5)):
        session.add(ExtractionFieldReview(
            paper_id=paper.id,
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            reviewer_status="verified",
            target_resolution_status="active",
            evidence_text=claim.evidence_text,
        ))
        session.add(EvidenceLocator(
            paper_id=paper.id,
            source_type="pdf",
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            page=page,
            evidence_text=claim.evidence_text,
            locator_status="exact_page",
            locator_confidence=1.0,
            parser_source="test",
        ))
    session.add_all([
        ContentEvidenceItem(
            paper_id=paper.id, category="mechanism_evidence", source_type="mechanism_claim", source_id=str(english.id),
            content="Fe-N4 accelerates LiPS conversion", evidence_text="Fe-N4 sites accelerate LiPS conversion.",
            evidence_locator={"page": 4, "locator_status": "exact_page"}, page_start=4, review_status="validated", citation_status="citable",
        ),
        ContentEvidenceItem(
            paper_id=paper.id, category="performance_evidence", source_type="mechanism_claim", source_id=str(chinese.id),
            content="Fe-N4 位点促进多硫化物转化", evidence_text="结果表明 Fe-N4 位点促进多硫化物转化。",
            evidence_locator={"page": 5, "locator_status": "exact_page"}, page_start=5, review_status="validated", citation_status="citable",
        ),
        ContentEvidenceItem(
            paper_id=paper.id, category="writing_material", source_type="seed", source_id="candidate",
            content="未审核候选：3.2 mAh cm-2", evidence_text="candidate only", evidence_locator={"page": 6}, page_start=6,
            review_status="needs_review", citation_status="needs_review", risk_flags=["candidate_requires_review"],
        ),
    ])
    session.commit(); return paper


def test_hybrid_rag_handles_chinese_english_mixed_and_full_context(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    for query, expects_content_knowledge in (
        ("LiPS conversion", True),
        ("多硫化物转化", True),
        ("Fe-N4 2.5 mg cm-2", False),
    ):
        response = client.post("/api/retrieval/search", json={"query": query, "paper_ids": [paper_id], "limit": 12, "rerank": False})
        assert response.status_code == 200, response.text
        if expects_content_knowledge:
            assert any(item["source"] in {"mechanism_claims", "content_knowledge"} for item in response.json()["items"])
    full = client.post("/api/retrieval/search", json={"query": "LiPS", "paper_ids": [paper_id], "mode": "full_context", "limit": 12, "rerank": False})
    assert full.status_code == 200
    assert "full_context" in {item["source"] for item in full.json()["items"]}
    assert any(item["source"] != "full_context" for item in full.json()["items"])


def test_review_bundle_v1_is_gone_while_object_gated_plan_remains_available(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    generated = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id})
    assert generated.status_code == 410
    assert generated.json()["detail"]["code"] == "content_review_bundle_v1_deprecated"
    plan = client.post("/api/content-knowledge/writing-plan", json={"query": "LiPS conversion", "paper_ids": [paper_id]}).json()
    assert plan["citation_plan"]
    assert all(row["citation_status"] == "citable" for row in plan["citation_plan"])
    assert all("candidate" not in row["source_fragment"] for row in plan["citation_plan"])


def test_web_and_ide_use_the_same_bundle_contract(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    for source in ("web_ai", "ide_ai"):
        response = client.post(
            "/api/content-knowledge/review-bundles",
            json={"paper_id": paper_id, "created_by": source},
        )
        assert response.status_code == 410
        assert response.json()["detail"]["read_only"] is True


def test_one_external_run_has_one_task_and_refreshes_after_materialize(setup_test_db):
    client = TestClient(app)
    with Session(setup_test_db) as session:
        paper = Paper(library_name="内容闭环测试库", paper_code="CR002", title="batch", pdf_path="batch.pdf", authors=[])
        session.add(paper); session.commit(); paper_id = str(paper.id)
    payload = {"paper_id": paper_id, "source": "web_ai", "raw_payload": {"review_notes": [{"content": f"image {index}", "page": 1} for index in range(10)]}}
    imported = client.post("/api/external-analysis/import", json=payload)
    assert imported.status_code == 200, imported.text
    run_id = imported.json()["id"]
    with Session(setup_test_db) as session:
        jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "agent_activity")).all()
        assert len(jobs) == 1
        assert jobs[0].payload["external_analysis_run_id"] == run_id
        candidates = session.scalars(select(ExternalAnalysisCandidate).where(ExternalAnalysisCandidate.run_id == UUID(run_id))).all()
    materialized = client.post(f"/api/external-analysis/runs/{run_id}/materialize", json={"explicit_all": True, "created_by": "human"})
    assert materialized.status_code == 200, materialized.text
    with Session(setup_test_db) as session:
        job = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "agent_activity")).one()
        assert job.result["last_action"] == "materialize"
        assert job.result["metrics"]["total"] == len(candidates) == 10


@pytest.mark.parametrize("field_name", ["content", "evidence_locator"])
def test_content_review_rejects_real_stale_snapshot_without_upgrading_item(setup_test_db, tmp_path, field_name):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
        item = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.content == "Fe-N4 accelerates LiPS conversion"))
        item.review_status, item.citation_status = "needs_review", "needs_review"
        session.commit()
    client = TestClient(app)
    with Session(setup_test_db) as session:
        current = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.content == "Fe-N4 accelerates LiPS conversion"))
        item_id = current.id
        setattr(current, field_name, "changed after export" if field_name == "content" else {"page": 99})
        session.commit()
    response = client.post(
        f"/api/content-knowledge/review-bundles/{uuid4()}/validate",
        json={"items": []},
    )
    assert response.status_code == 410
    with Session(setup_test_db) as session:
        current = session.get(ContentEvidenceItem, item_id)
        assert current.citation_status == "needs_review"


def test_full_context_reserves_content_knowledge_when_sections_fill_limit(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path)
        for page in (1, 2, 3, 4):
            session.add(PaperSection(paper_id=paper.id, section_title=f"Section {page}", text=f"Fe-N4 LiPS section {page}", page_start=page, page_end=page))
        session.commit(); paper_id = str(paper.id)
    response = TestClient(app).post("/api/retrieval/search", json={"query": "Fe-N4 LiPS", "paper_ids": [paper_id], "mode": "full_context", "limit": 3, "rerank": False})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {item["source"] for item in items} >= {"full_context", "content_knowledge"}
    sections = [item for item in items if item["source"] == "full_context"]
    assert [item["page_start"] for item in sections] == sorted(item["page_start"] for item in sections)


def test_content_review_apply_rechecks_snapshot_after_validation(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    with Session(setup_test_db) as session:
        current = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.content == "Fe-N4 accelerates LiPS conversion"))
        item_id = current.id
        current.evidence_locator = {"page": 77}
        session.commit()
    response = client.post(f"/api/content-knowledge/review-bundles/{uuid4()}/apply", json={"reviewer": "human"})
    assert response.status_code == 410
    with Session(setup_test_db) as session:
        current = session.get(ContentEvidenceItem, item_id)
        assert current.review_status == "validated"
        assert current.citation_status == "citable"


def test_content_bundle_refreshes_the_same_external_run_task(setup_test_db, tmp_path):
    pdf_path = tmp_path / "run.pdf"; pdf_path.write_bytes(b"%PDF-1.4\nrun\n%%EOF\n")
    with Session(setup_test_db) as session:
        paper = Paper(library_name="内容闭环测试库", paper_code="CR003", title="run-linked", pdf_path=str(pdf_path), authors=[])
        session.add(paper); session.commit(); paper_id = str(paper.id)
    client = TestClient(app)
    imported = client.post("/api/external-analysis/import", json={"paper_id": paper_id, "source": "web_ai", "raw_payload": {"review_notes": [{"content": "External candidate", "page": 1, "quoted_text": "PDF-supported note."}]}})
    assert imported.status_code == 200, imported.text
    run_id = imported.json()["id"]
    synced = client.post(f"/api/content-knowledge/sync?paper_id={paper_id}")
    assert synced.status_code == 200
    with Session(setup_test_db) as session:
        projected = session.scalars(select(ContentEvidenceItem).where(ContentEvidenceItem.run_id == UUID(run_id))).all()
        assert len(projected) == 1
        assert projected[0].source_record["external_analysis_run_id"] == run_id
    deprecated = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id, "run_id": run_id})
    assert deprecated.status_code == 410
    with Session(setup_test_db) as session:
        jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "agent_activity")).all()
        assert len(jobs) == 1
        assert jobs[0].payload["external_analysis_run_id"] == run_id
        assert jobs[0].result["lifecycle"] != "finalized"
        assert session.scalar(select(func.count()).select_from(ContentReviewBundle)) == 0


def test_writing_plan_is_read_only_and_never_projects_itself(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
        candidate = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.source_id == "candidate"))
        candidate.content = "Unreviewed LiPS conversion candidate"
        session.commit()
        evidence_before = session.scalar(select(func.count()).select_from(EvidenceClaim))
        content_before = session.scalar(select(func.count()).select_from(ContentEvidenceItem))
    client = TestClient(app)
    first = client.post("/api/content-knowledge/writing-plan", json={"query": "LiPS conversion", "paper_ids": [paper_id]})
    second = client.post("/api/content-knowledge/writing-plan", json={"query": "LiPS conversion", "paper_ids": [paper_id]})
    assert first.status_code == second.status_code == 200
    assert first.json()["citation_plan"] == second.json()["citation_plan"]
    assert first.json()["excluded_unreviewed"] >= 1
    assert client.post(f"/api/content-knowledge/sync?paper_id={paper_id}").status_code == 200
    with Session(setup_test_db) as session:
        assert session.scalar(select(func.count()).select_from(EvidenceClaim)) == evidence_before
        assert session.scalar(select(func.count()).select_from(ContentEvidenceItem)) == content_before
        assert not session.scalars(select(ContentEvidenceItem).where(ContentEvidenceItem.source_type == "content_writing_plan")).all()


def test_retrieval_is_read_only_and_does_not_sync_projection(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
        before = session.scalar(select(func.count()).select_from(ContentEvidenceItem))
    response = TestClient(app).post("/api/retrieval/search", json={"query": "LiPS conversion", "paper_ids": [paper_id], "limit": 10, "rerank": False})
    assert response.status_code == 200
    with Session(setup_test_db) as session:
        assert session.scalar(select(func.count()).select_from(ContentEvidenceItem)) == before


def test_retrieval_api_without_projection_is_read_only(setup_test_db, monkeypatch):
    with Session(setup_test_db) as session:
        paper = Paper(library_name="检索只读测试库", paper_code="RO001", title="Read-only retrieval", pdf_path="ro.pdf", authors=[])
        session.add(paper); session.flush()
        session.add(PaperSection(paper_id=paper.id, section_title="Results", text="Fe-N4 lexical retrieval evidence", page_start=1, page_end=1))
        session.commit(); paper_id = str(paper.id)

    def fail_if_sync(*args, **kwargs):
        raise AssertionError("retrieval must not call sync_items")

    monkeypatch.setattr("app.services.content_knowledge_service.ContentKnowledgeService.sync_items", fail_if_sync)
    client = TestClient(app)
    with Session(setup_test_db) as session:
        before = {
            "content": session.scalar(select(func.count()).select_from(ContentEvidenceItem)),
            "claims": session.scalar(select(func.count()).select_from(EvidenceClaim)),
            "jobs": session.scalar(select(func.count()).select_from(WorkflowJob)),
            "bundles": session.scalar(select(func.count()).select_from(ContentReviewBundle)),
        }
    response = client.post("/api/retrieval/search", json={"query": "Fe-N4", "paper_ids": [paper_id], "limit": 5, "rerank": False})
    assert response.status_code == 200, response.text
    with Session(setup_test_db) as session:
        after = {
            "content": session.scalar(select(func.count()).select_from(ContentEvidenceItem)),
            "claims": session.scalar(select(func.count()).select_from(EvidenceClaim)),
            "jobs": session.scalar(select(func.count()).select_from(WorkflowJob)),
            "bundles": session.scalar(select(func.count()).select_from(ContentReviewBundle)),
        }
    assert after == before


def test_isolated_api_v1_mutators_return_stable_410_and_writing_plan_is_readonly(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    generated = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id})
    assert generated.status_code == 410
    base = f"/api/content-knowledge/review-bundles/{uuid4()}"
    for suffix, payload in (
        ("/validate", {"items": []}),
        ("/apply", {"reviewer": "isolated-human"}),
        ("/finalize", {"reviewer": "isolated-human"}),
    ):
        response = client.post(base + suffix, json=payload)
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "content_review_bundle_v1_deprecated"
    plan = client.post("/api/content-knowledge/writing-plan", json={"query": "LiPS conversion", "paper_ids": [paper_id]})
    assert plan.status_code == 200
    assert plan.json()["citation_plan"]
    assert plan.json()["persistence"] == {"writes_db": False, "saved_plan": False}
