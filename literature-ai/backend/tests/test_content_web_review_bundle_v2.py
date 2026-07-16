from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import ContentEvidenceItem, ExtractionFieldReview, MechanismClaim, Paper, PaperSection, WritingCard
from app.main import app
from app.services.content_web_review_bundle_v2_service import ContentWebReviewBundleV2Service


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _seed(engine, tmp_path):
    pdf = tmp_path / "v2.pdf"
    pdf.write_bytes(b"%PDF-1.4\nv2 source\n%%EOF")
    with _factory(engine).begin() as session:
        paper = Paper(paper_code="WV201", title="Web proposal V2", abstract="Abstract evidence", pdf_path=str(pdf), authors=[])
        session.add(paper); session.flush()
        first = PaperSection(paper_id=paper.id, section_title="Results", text="First page evidence", page_start=4, page_end=4)
        second = PaperSection(paper_id=paper.id, section_title="Discussion", text="Second same page evidence", page_start=4, page_end=4)
        claim = MechanismClaim(paper_id=paper.id, claim_type="mechanism", claim_text="Claim evidence", evidence_text="Claim evidence")
        card = WritingCard(paper_id=paper.id, research_gap="Writing-card evidence")
        session.add_all([first, second, claim, card]); session.flush()
        # A REJECT of this source is consequential, so it must enter the local plan.
        session.add(ContentEvidenceItem(paper_id=paper.id, category="mechanism_evidence", source_type="mechanism_claim", source_id=str(claim.id), content="Claim evidence", review_status="validated", citation_status="citable", risk_flags=[]))
        return str(paper.id), str(first.id)


def _proposal(manifest, decisions=None):
    decisions = decisions or {}
    actions = []
    for target in manifest["targets"]:
        evidence = target["evidence"]
        actions.append({
            "plan_item_id": target["plan_item_id"], "target_type": target["target_type"], "target_id": target["target_id"],
            "field_name": target["field_name"], "object_snapshot_hash": target["object_snapshot_hash"],
            "decision": decisions.get(target["target_type"], "PASS"), "evidence_ref_ids": [evidence["evidence_ref_id"]],
            "evidence_quote": evidence["evidence_excerpt"], "evidence_asset_sha256": evidence["evidence_asset_sha256"], "page": evidence["page"],
        })
    return {"schema_version": "content_web_review_proposal_v2", "bundle_fingerprint": manifest["bundle_fingerprint"], "paper_id": manifest["paper_id"], "paper_code": manifest["paper_code"], "proposal_status": "web_ai_proposal", "source_identity_verified": False, "writes_final_truth": False, "local_ai_verification": None, "actions": actions, "discovery_proposals": [{"summary": "possible future target", "target_id": None}]}


def test_v2_zip_validation_is_proposal_only_and_plan_dedupes_pages(setup_test_db, tmp_path):
    paper_id, _ = _seed(setup_test_db, tmp_path)
    client = TestClient(app)
    created = client.post("/api/content-knowledge/review-bundles/v2", json={"paper_id": paper_id})
    assert created.status_code == 200
    body = created.json(); manifest = body["manifest"]
    assert body["proposal_only"] is True and body["writes_final_truth"] is False
    archive = client.get(body["download_url"])
    assert archive.status_code == 200
    with ZipFile(BytesIO(archive.content)) as zip_file:
        names = set(zip_file.namelist())
        assert {"manifest.json", "return_schema.json", "return_template.json", "instructions_for_web_ai.md", "required_target_ids.json", "required_field_coverage.json", "allowed_evidence_refs.json", "allowed_pages.json", "local_verification_requirements.json", "format_examples.json"} <= names
        assert any(name.startswith("evidence/") for name in names)
    proposal = _proposal(manifest, {"mechanism_claim": "REJECT", "writing_card": "NEEDS_HUMAN"})
    validated = client.post(f"/api/content-knowledge/review-bundles/{body['bundle_id']}/web-proposal/validate", json=proposal)
    assert validated.status_code == 200 and validated.json()["valid"] is True
    with _factory(setup_test_db)() as session:
        # Web validation itself does not create reviews or alter formal qualification.
        assert session.scalar(select(func.count()).select_from(ExtractionFieldReview)) == 0
        item = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.source_type == "mechanism_claim"))
        assert item.review_status == "validated" and item.citation_status == "citable"
    plan = client.get(f"/api/content-knowledge/review-bundles/{body['bundle_id']}/local-verification-plan")
    assert plan.status_code == 200
    payload = plan.json()
    assert payload["local_required_target_count"] <= payload["web_reviewed_target_count"]
    assert payload["local_skipped_target_count_by_reason"] == {"ordinary_reject": 0, "needs_human": 1, "discovery_proposals": 1}
    assert payload["metrics"]["physical_page_read_attempt_count"] == 0
    assert len([row for row in payload["required_page_checks"] if row["page"] == 4]) == 1
    assert any(row["decision"] == "REJECT" for row in payload["required_object_checks"])


def test_v2_rejects_incomplete_forged_and_wrong_page_payloads(setup_test_db, tmp_path):
    paper_id, _ = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id)); manifest = bundle["manifest"]
        payload = _proposal(manifest)
        payload["source_identity_verified"] = True
        payload["actions"] = payload["actions"][:-1]
        payload["actions"][0]["page"] = 999
        payload["actions"][0]["evidence_quote"] = "invented quote"
        result = service.validate_web_proposal(UUID(bundle["bundle_id"]), payload)
        assert result["valid"] is False
        assert result["status"] == "proposal_invalid"
        assert {"forged_source_identity", "incomplete_required_field_coverage"} <= set(result["errors"])
        assert any(error.startswith("wrong_page:") for error in result["errors"])
        assert any(error.startswith("forged_quote:") for error in result["errors"])


def test_v2_stale_dependency_report_is_target_scoped(setup_test_db, tmp_path):
    paper_id, section_id = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id)); proposal = _proposal(bundle["manifest"])
        assert service.validate_web_proposal(UUID(bundle["bundle_id"]), proposal)["valid"] is True
    with _factory(setup_test_db).begin() as session:
        section = session.get(PaperSection, UUID(section_id)); section.text = "Changed snapshot text"
    with _factory(setup_test_db).begin() as session:
        result = ContentWebReviewBundleV2Service(session).local_verification_plan(UUID(bundle["bundle_id"]))
        assert result["status"] == "stale"
        assert result["stale"]["changed_dependencies"] == ["target"]
        assert len(result["stale"]["affected_plan_item_ids"]) == 1


def test_v2_source_pdf_change_stales_all_dependent_targets(setup_test_db, tmp_path):
    paper_id, _ = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id)); proposal = _proposal(bundle["manifest"])
        assert service.validate_web_proposal(UUID(bundle["bundle_id"]), proposal)["valid"] is True
        target_count = len(bundle["manifest"]["targets"])
        pdf = Path(session.get(Paper, UUID(paper_id)).pdf_path)
    pdf.write_bytes(b"%PDF-1.4\nchanged source\n%%EOF")
    with _factory(setup_test_db).begin() as session:
        result = ContentWebReviewBundleV2Service(session).local_verification_plan(UUID(bundle["bundle_id"]))
        assert result["status"] == "stale"
        assert result["stale"]["changed_dependencies"] == ["source_pdf"]
        assert len(result["stale"]["affected_plan_item_ids"]) == target_count
