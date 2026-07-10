from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ContentEvidenceItem, ContentReviewBundle, EvidenceClaim, ExternalAnalysisCandidate, ExternalAnalysisRun, Paper, PaperSection, WorkflowJob
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
    session.add_all([
        ContentEvidenceItem(
            paper_id=paper.id, category="mechanism_evidence", source_type="seed", source_id="en",
            content="Fe-N4 accelerates LiPS conversion", evidence_text="Fe-N4 sites accelerate LiPS conversion.",
            evidence_locator={"page": 4}, page_start=4, review_status="validated", citation_status="citable",
        ),
        ContentEvidenceItem(
            paper_id=paper.id, category="performance_evidence", source_type="seed", source_id="zh",
            content="Fe-N4 位点促进多硫化物转化", evidence_text="结果表明 Fe-N4 位点促进多硫化物转化。",
            evidence_locator={"page": 5}, page_start=5, review_status="validated", citation_status="citable",
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
    for query in ("LiPS conversion", "多硫化物转化", "Fe-N4 2.5 mg cm-2"):
        response = client.post("/api/retrieval/search", json={"query": query, "paper_ids": [paper_id], "limit": 12, "rerank": False})
        assert response.status_code == 200, response.text
        assert any(item["source"] == "content_knowledge" for item in response.json()["items"])
    full = client.post("/api/retrieval/search", json={"query": "LiPS", "paper_ids": [paper_id], "mode": "full_context", "limit": 12, "rerank": False})
    assert full.status_code == 200
    assert {item["source"] for item in full.json()["items"]} >= {"full_context", "content_knowledge"}


def test_review_bundle_rejects_bad_identity_snapshot_ids_and_allows_citable_plan(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    generated = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id})
    assert generated.status_code == 200, generated.text
    bundle = generated.json(); template = bundle["return_template"]
    item = next(row for row in bundle["manifest"]["items"] if row["content"].startswith("Fe-N4 accelerates"))
    valid = {**template, "review_source": {"review_source_type": "ide_ai", "source_identity_verified": False}, "items": [{"item_id": item["item_id"], "decision": "approve_citable", "evidence_id": item["evidence_id"], "evidence_text": "Fe-N4 sites accelerate LiPS conversion."}]}
    for changed in (
        {**valid, "bundle_fingerprint": "0" * 64},
        {**valid, "paper_code": "WRONG"},
        {**valid, "items": [{**valid["items"][0], "evidence_id": "evidence:unknown"}]},
        {**valid, "review_source": {"review_source_type": "web_ai", "source_identity_verified": True}},
    ):
        response = client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/validate", json=changed)
        assert response.status_code == 409
    response = client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/validate", json=valid)
    assert response.status_code == 200, response.text
    assert client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/apply", json={"reviewer": "human"}).status_code == 200
    plan = client.post("/api/content-knowledge/writing-plan", json={"query": "LiPS conversion", "paper_ids": [paper_id]}).json()
    assert plan["citation_plan"]
    assert all(row["citation_status"] == "citable" for row in plan["citation_plan"])
    assert all("candidate" not in row["source_fragment"] for row in plan["citation_plan"])


def test_web_and_ide_use_the_same_bundle_contract(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    for source in ("web_ai", "ide_ai"):
        bundle = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id}).json()
        item = bundle["manifest"]["items"][0]
        result = {**bundle["return_template"], "review_source": {"review_source_type": source, "source_identity_verified": False}, "items": [{"item_id": item["item_id"], "decision": "needs_human", "evidence_id": item["evidence_id"]}]}
        response = client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/validate", json=result)
        assert response.status_code == 200, response.text


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
        item = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.source_id == "en"))
        item.review_status, item.citation_status = "needs_review", "needs_review"
        session.commit()
    client = TestClient(app)
    bundle = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id}).json()
    item = next(row for row in bundle["manifest"]["items"] if row["content"].startswith("Fe-N4 accelerates"))
    result = {**bundle["return_template"], "review_source": {"review_source_type": "ide_ai", "source_identity_verified": False}, "items": [{"item_id": item["item_id"], "decision": "approve_citable", "evidence_id": item["evidence_id"], "evidence_text": "Fe-N4 sites accelerate LiPS conversion."}]}
    with Session(setup_test_db) as session:
        current = session.get(ContentEvidenceItem, UUID(item["item_id"]))
        setattr(current, field_name, "changed after export" if field_name == "content" else {"page": 99})
        session.commit()
    response = client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/validate", json=result)
    assert response.status_code == 409
    assert response.json()["detail"] == "content_review_validation_failed:stale_snapshot"
    with Session(setup_test_db) as session:
        current = session.get(ContentEvidenceItem, UUID(item["item_id"]))
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
    bundle = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id}).json()
    item = bundle["manifest"]["items"][0]
    result = {**bundle["return_template"], "review_source": {"review_source_type": "ide_ai", "source_identity_verified": False}, "items": [{"item_id": item["item_id"], "decision": "reject", "evidence_id": item["evidence_id"]}]}
    assert client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/validate", json=result).status_code == 200
    with Session(setup_test_db) as session:
        current = session.get(ContentEvidenceItem, UUID(item["item_id"]))
        current.evidence_locator = {"page": 77}
        session.commit()
    response = client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/apply", json={"reviewer": "human"})
    assert response.status_code == 409
    assert response.json()["detail"] == "content_review_validation_failed:stale_snapshot"


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
    bundle = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id, "run_id": run_id}).json()
    item = bundle["manifest"]["items"][0]
    result = {**bundle["return_template"], "review_source": {"review_source_type": "web_ai", "reviewer_label": "declared web", "source_identity_verified": False}, "items": [{"item_id": item["item_id"], "decision": "reject", "evidence_id": item["evidence_id"]}]}
    assert client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/validate", json=result).status_code == 200
    assert client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/apply", json={"reviewer": "human"}).status_code == 200
    assert client.post(f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}/finalize", json={"reviewer": "human"}).status_code == 200
    with Session(setup_test_db) as session:
        jobs = session.scalars(select(WorkflowJob).where(WorkflowJob.type == "agent_activity")).all()
        assert len(jobs) == 1
        assert jobs[0].payload["external_analysis_run_id"] == run_id
        assert jobs[0].result["lifecycle"] == "finalized"
        assert jobs[0].result["last_action"] == "finalized"
        assert jobs[0].result["metrics"]["problem_count"] == 0
        bundle_row = session.get(ContentReviewBundle, UUID(bundle["bundle_id"]))
        assert bundle_row.manifest["review_identity"]["identity_verified"] is False


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


def test_isolated_api_generate_validate_apply_finalize_and_writing_plan_closure(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper = _seed(session, tmp_path); paper_id = str(paper.id)
    client = TestClient(app)
    bundle = client.post("/api/content-knowledge/review-bundles", json={"paper_id": paper_id}).json()
    actions = []
    for item in bundle["manifest"]["items"]:
        if item["content"].startswith("Fe-N4 accelerates"):
            actions.append({"item_id": item["item_id"], "decision": "approve_citable", "evidence_id": item["evidence_id"], "evidence_text": "Fe-N4 sites accelerate LiPS conversion."})
        elif "多硫化物" in item["content"]:
            actions.append({"item_id": item["item_id"], "decision": "approve_citable", "evidence_id": item["evidence_id"], "evidence_text": "结果表明 Fe-N4 位点促进多硫化物转化。"})
        else:
            actions.append({"item_id": item["item_id"], "decision": "reject", "evidence_id": item["evidence_id"]})
    result = {**bundle["return_template"], "review_source": {"review_source_type": "ide_ai", "reviewer_label": "isolated-test", "source_identity_verified": False}, "items": actions}
    base = f"/api/content-knowledge/review-bundles/{bundle['bundle_id']}"
    assert client.post(base + "/validate", json=result).status_code == 200
    applied = client.post(base + "/apply", json={"reviewer": "isolated-human"})
    assert applied.status_code == 200 and applied.json()["needs_human"] == 0
    assert client.post(base + "/finalize", json={"reviewer": "isolated-human"}).json()["finalized"] is True
    plan = client.post("/api/content-knowledge/writing-plan", json={"query": "LiPS conversion", "paper_ids": [paper_id]})
    assert plan.status_code == 200
    assert plan.json()["citation_plan"]
    assert plan.json()["persistence"] == {"writes_db": False, "saved_plan": False}
