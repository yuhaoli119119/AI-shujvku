from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import ContentEvidenceItem, EvidenceLocator, ExtractionFieldReview, MechanismClaim, Paper, PaperCorrection, PaperSection, WritingCard
from app.main import app
from app.services.content_web_review_bundle_v2_service import ContentWebReviewBundleV2Service


ALL_MODULES = ["abstract", "sections", "mechanism_knowledge", "writing_cards"]


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
            "proposed_value": None, "verification_note": None,
        })
    return {"schema_version": "content_web_review_proposal_v2", "bundle_fingerprint": manifest["bundle_fingerprint"], "paper_id": manifest["paper_id"], "paper_code": manifest["paper_code"], "proposal_status": "web_ai_proposal", "source_identity_verified": False, "writes_final_truth": False, "local_ai_verification": None, "actions": actions, "discovery_proposals": [{"summary": "possible future target", "target_id": None}]}


def test_v2_zip_validation_is_proposal_only_and_plan_dedupes_pages(setup_test_db, tmp_path):
    paper_id, _ = _seed(setup_test_db, tmp_path)
    client = TestClient(app)
    created = client.post("/api/content-knowledge/review-bundles/v2", json={"paper_id": paper_id, "modules": ALL_MODULES})
    assert created.status_code == 200
    body = created.json(); manifest = body["manifest"]
    assert body["proposal_only"] is True and body["writes_final_truth"] is False
    assert body["object_count"] == len(manifest["targets"])
    assert body["web_ai_instruction"]
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
        assert session.scalar(select(func.count()).select_from(EvidenceLocator)) == 0
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0
        item = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.source_type == "mechanism_claim"))
        assert item.review_status == "validated" and item.citation_status == "citable"
    plan = client.get(f"/api/content-knowledge/review-bundles/{body['bundle_id']}/local-verification-plan")
    assert plan.status_code == 200
    payload = plan.json()
    assert payload["local_required_target_count"] <= payload["web_reviewed_target_count"]
    assert payload["local_skipped_target_count_by_reason"] == {"ordinary_reject": 0, "needs_human": 1, "discovery_proposals": 1}
    assert payload["metrics"]["physical_page_read_attempt_count"] == 0
    assert len([row for row in payload["required_page_checks"] if row["page"] == 4]) == 1
    assert len(payload["required_evidence_checks"]) == payload["local_required_target_count"]
    assert any(row["decision"] == "REJECT" for row in payload["required_object_checks"])
    assert payload["unique_page_count"] == payload["metrics"]["logical_page_read_count"]
    assert "apply_content_web_review_local_verification" in payload["local_ai_instruction"]
    assert payload["metrics"]["unresolved_page_target_count"] == payload["unresolved_page_target_count"]


def test_v2_rejects_incomplete_forged_and_wrong_page_payloads(setup_test_db, tmp_path):
    paper_id, _ = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id), modules=ALL_MODULES); manifest = bundle["manifest"]
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
        bundle = service.generate(paper_id=UUID(paper_id), modules=ALL_MODULES); proposal = _proposal(bundle["manifest"])
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
        bundle = service.generate(paper_id=UUID(paper_id), modules=ALL_MODULES); proposal = _proposal(bundle["manifest"])
        assert service.validate_web_proposal(UUID(bundle["bundle_id"]), proposal)["valid"] is True
        target_count = len(bundle["manifest"]["targets"])
        pdf = Path(session.get(Paper, UUID(paper_id)).pdf_path)
    pdf.write_bytes(b"%PDF-1.4\nchanged source\n%%EOF")
    with _factory(setup_test_db).begin() as session:
        result = ContentWebReviewBundleV2Service(session).local_verification_plan(UUID(bundle["bundle_id"]))
        assert result["status"] == "stale"
        assert result["stale"]["changed_dependencies"] == ["source_pdf"]
        assert len(result["stale"]["affected_plan_item_ids"]) == target_count


def test_v2_modules_reject_empty_invalid_and_persist_selected_scope(setup_test_db, tmp_path):
    paper_id, _ = _seed(setup_test_db, tmp_path)
    client = TestClient(app)
    assert client.post("/api/content-knowledge/review-bundles/v2", json={"paper_id": paper_id}).status_code == 400
    assert client.post("/api/content-knowledge/review-bundles/v2", json={"paper_id": paper_id, "module": "all"}).status_code == 400
    response = client.post("/api/content-knowledge/review-bundles/v2", json={"paper_id": paper_id, "module": "sections"})
    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["selected_modules"] == ["sections"]
    assert {item["target_type"] for item in manifest["targets"]} == {"paper_section"}
    with _factory(setup_test_db).begin() as session:
        empty = Paper(paper_code="WV202", title="empty", pdf_path=str(tmp_path / "empty.pdf"), authors=[])
        session.add(empty); session.flush()
        try:
            ContentWebReviewBundleV2Service(session).generate(paper_id=empty.id, module="sections")
        except ValueError as exc:
            assert str(exc) == "content_web_review_v2_no_targets_for_selected_modules"
        else:
            raise AssertionError("empty module scope must not create a review package")


def test_v2_stable_uuid_schema_revise_shape_and_locator_page_batch(setup_test_db, tmp_path):
    paper_id, first_section_id = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        session.add(EvidenceLocator(
            paper_id=UUID(paper_id), target_type="PaperSection", target_id=first_section_id, field_name="text",
            page=4, bbox={"x0": 1, "y0": 2}, evidence_text="Locator-backed section evidence", locator_status="located",
        ))
        session.flush()
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id), module="sections")
        manifest = bundle["manifest"]
        first = next(item for item in manifest["targets"] if item["target_id"] == first_section_id)
        assert UUID(first["plan_item_id"]).version == 5
        assert first["evidence"]["evidence_source"] == "evidence_locator"
        assert first["evidence"]["locator_status"] == "located"
        assert first["evidence"]["page_asset_status"] == "not_materialized"
        schema = manifest["return_schema"]
        assert schema["$schema"].endswith("draft/2020-12/schema")
        assert schema["additionalProperties"] is False
        proposal = _proposal(manifest)
        proposal["actions"][0]["decision"] = "REVISE"
        invalid = service.validate_web_proposal(UUID(bundle["bundle_id"]), proposal)
        assert any(error.startswith("revise_requires_proposed_value:") for error in invalid["errors"])
        proposal["actions"][0]["proposed_value"] = "corrected section"
        proposal["actions"][0]["verification_note"] = "compare wording"
        valid = service.validate_web_proposal(UUID(bundle["bundle_id"]), proposal)
        assert valid["valid"] is True
        plan = service.local_verification_plan(UUID(bundle["bundle_id"]))
        revised = next(item for item in plan["required_object_checks"] if item["decision"] == "REVISE")
        assert revised["proposed_value"] == "corrected section"
        assert revised["verification_note"] == "compare wording"
        assert plan["unique_page_count"] == 1
        assert plan["page_batches"][0]["page"] == 4
        assert plan["page_batches"][0]["target_count"] == 2


def test_v2_unlocated_mechanism_still_requires_page_render_and_blocks(setup_test_db, tmp_path):
    paper_id, _ = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id), module="mechanism_knowledge")
        proposal = _proposal(bundle["manifest"], {"mechanism_claim": "REJECT"})
        assert service.validate_web_proposal(UUID(bundle["bundle_id"]), proposal)["valid"] is True
        plan = service.local_verification_plan(UUID(bundle["bundle_id"]))
        row = plan["required_object_checks"][0]
        assert row["page"] is None and row["requires_page_render"] is True
        assert row["layout_consistency_status"] == "page_unlocated"
        assert plan["required_page_checks"] == []
        assert plan["unresolved_page_target_count"] == 1


def test_v2_uuidv5_identity_does_not_shift_when_a_target_is_added(setup_test_db, tmp_path):
    paper_id, first_section_id = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id), module="sections")
        original = next(item for item in bundle["manifest"]["targets"] if item["target_id"] == first_section_id)
        original_id = original["plan_item_id"]
        session.add(PaperSection(paper_id=UUID(paper_id), section_title="New", text="new target", page_start=8))
        session.flush()
        stale = service._stale_report(service._bundle(UUID(bundle["bundle_id"])))
        current = service._build_manifest(session.get(Paper, UUID(paper_id)), selected_modules=["sections"])
        unchanged = next(item for item in current["targets"] if item["target_id"] == first_section_id)
        assert unchanged["plan_item_id"] == original_id
        assert stale["affected_plan_item_ids"] != [original_id]


def test_v2_zip_embeds_materialized_page_asset_without_local_path(setup_test_db, tmp_path):
    paper_id, first_section_id = _seed(setup_test_db, tmp_path)
    preview = tmp_path / "page_004.png"
    preview_bytes = b"\x89PNG\r\n\x1a\nmaterialized-page"
    preview.write_bytes(preview_bytes)
    with _factory(setup_test_db).begin() as session:
        session.add(EvidenceLocator(
            paper_id=UUID(paper_id), target_type="paper_section", target_id=first_section_id, field_name="text",
            page=4, bbox={"full_page_image_path": str(preview), "x0": 0}, evidence_text="preview-backed", locator_status="located",
        ))
        session.flush()
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id), module="sections")
        evidence = next(item["evidence"] for item in bundle["manifest"]["targets"] if item["target_id"] == first_section_id)
        assert evidence["page_asset_status"] == "materialized"
        assert evidence["page_asset_ref"].startswith("evidence/pages/")
        assert str(tmp_path) not in evidence["page_asset_ref"]
        assert "full_page_image_path" not in evidence["bbox"]
        archive = service.download(UUID(bundle["bundle_id"]))
        with ZipFile(BytesIO(archive["content"])) as zip_file:
            assert zip_file.read(evidence["page_asset_ref"]) == preview_bytes
            manifest = __import__("json").loads(zip_file.read("manifest.json"))
            assert str(tmp_path) not in __import__("json").dumps(manifest)


def test_v2_renders_only_selected_pdf_page_when_preview_is_absent(setup_test_db, tmp_path):
    import fitz

    paper_id, first_section_id = _seed(setup_test_db, tmp_path)
    pdf = tmp_path / "renderable.pdf"
    document = fitz.open()
    for number in range(4):
        page = document.new_page()
        page.insert_text((72, 72), f"page {number + 1}")
    document.save(pdf)
    document.close()
    with _factory(setup_test_db).begin() as session:
        session.get(Paper, UUID(paper_id)).pdf_path = str(pdf)
        service = ContentWebReviewBundleV2Service(session)
        bundle = service.generate(paper_id=UUID(paper_id), module="sections")
        evidence = next(item["evidence"] for item in bundle["manifest"]["targets"] if item["target_id"] == first_section_id)
        assert evidence["page_asset_status"] == "rendered_for_bundle"
        assert evidence["page_asset_ref"].endswith(".png")
        archive = service.download(UUID(bundle["bundle_id"]))
        with ZipFile(BytesIO(archive["content"])) as zip_file:
            assert zip_file.read(evidence["page_asset_ref"]).startswith(b"\x89PNG")
