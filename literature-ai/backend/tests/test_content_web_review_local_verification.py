from __future__ import annotations

from pathlib import Path
from uuid import UUID

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ContentEvidenceItem,
    ContentWebReviewLocalVerificationResult,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCorrection,
    PaperRelationship,
    PaperSection,
    WritingCard,
)
from app.main import app
from app.services.content_web_review_bundle_v2_service import ContentWebReviewBundleV2Service
from app.services.content_web_review_local_verification_service import (
    ContentWebReviewLocalVerificationError,
    ContentWebReviewLocalVerificationService,
)
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.review_service import ReviewService
from app.utils.review_safety import content_object_gate


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _pdf(path: Path, pages: int = 2) -> None:
    document = fitz.open()
    for number in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"evidence page {number + 1}")
    document.save(path)
    document.close()


def _seed(engine, tmp_path, suffix: str = "") -> dict[str, str]:
    pdf = tmp_path / f"local-verification{suffix}.pdf"
    _pdf(pdf)
    with _factory(engine).begin() as session:
        paper = Paper(
            paper_code=f"WV301{suffix}",
            title="Local verification",
            abstract="Grounded abstract evidence",
            pdf_path=str(pdf),
            authors=[],
        )
        session.add(paper)
        session.flush()
        first = PaperSection(
            paper_id=paper.id,
            section_title="First",
            text="alpha grounded statement",
            page_start=1,
            page_end=1,
        )
        second = PaperSection(
            paper_id=paper.id,
            section_title="Second",
            text="beta grounded statement",
            page_start=1,
            page_end=1,
        )
        card = WritingCard(
            paper_id=paper.id,
            research_gap="missing durable evidence",
            proposed_solution="add durable evidence",
            evidence_chain=[
                {
                    "supports_fields": ["research_gap"],
                    "text": "missing durable evidence",
                    "source": "paper",
                    "page": 1,
                    "locator_status": "exact_page",
                },
                {
                    "supports_fields": ["proposed_solution"],
                    "text": "add durable evidence",
                    "source": "paper",
                    "page": 1,
                    "locator_status": "exact_page",
                },
            ],
        )
        session.add_all([first, second, card])
        session.flush()
        for field_name in ("research_gap", "proposed_solution"):
            session.add(EvidenceLocator(
                paper_id=paper.id,
                target_type="writing_cards",
                target_id=str(card.id),
                field_name=field_name,
                source_type="pdf",
                page=1,
                evidence_text=getattr(card, field_name),
                locator_status="exact_page",
                locator_confidence=1.0,
                parser_source="parser",
            ))
        return {
            "paper_id": str(paper.id),
            "first_section_id": str(first.id),
            "second_section_id": str(second.id),
            "card_id": str(card.id),
        }


def _proposal(manifest, decisions=None):
    decisions = decisions or {}
    actions = []
    for target in manifest["targets"]:
        evidence = target["evidence"]
        actions.append({
            "plan_item_id": target["plan_item_id"],
            "target_type": target["target_type"],
            "target_id": target["target_id"],
            "field_name": target["field_name"],
            "object_snapshot_hash": target["object_snapshot_hash"],
            "decision": decisions.get(target["plan_item_id"], "PASS"),
            "evidence_ref_ids": [evidence["evidence_ref_id"]],
            "evidence_quote": evidence["evidence_excerpt"],
            "evidence_asset_sha256": evidence["evidence_asset_sha256"],
            "page": evidence["page"],
            "proposed_value": None,
            "verification_note": None,
        })
    return {
        "schema_version": "content_web_review_proposal_v2",
        "bundle_fingerprint": manifest["bundle_fingerprint"],
        "paper_id": manifest["paper_id"],
        "paper_code": manifest["paper_code"],
        "proposal_status": "web_ai_proposal",
        "source_identity_verified": False,
        "writes_final_truth": False,
        "local_ai_verification": None,
        "actions": actions,
        "discovery_proposals": [],
    }


def _bundle(session, paper_id: str, module: str):
    service = ContentWebReviewBundleV2Service(session)
    created = service.generate(paper_id=UUID(paper_id), module=module)
    proposal = _proposal(created["manifest"])
    assert service.validate_web_proposal(UUID(created["bundle_id"]), proposal)["valid"] is True
    return created, service.local_verification_plan(UUID(created["bundle_id"]))


def _result(check, outcome="CONFIRMED", verified_value=None):
    pages = []
    if check["requires_page_render"]:
        pages = [{
            "source_paper_id": check["source_paper_id"],
            "source_pdf_sha256": check["source_pdf_sha256"],
            "page": check["page"],
            "page_asset_sha256": check["page_asset_sha256"],
        }]
    payload = {
        "plan_item_id": check["plan_item_id"],
        "object_snapshot_hash": check["object_snapshot_hash"],
        "outcome": outcome,
        "checked_evidence_ids": [check["evidence_ref_id"]],
        "checked_pages": pages,
        "verification_note": f"local {outcome.lower()} conclusion",
    }
    if outcome == "REVISED":
        payload["verified_value"] = verified_value
    return payload


def test_validate_and_plan_never_change_formal_eligibility(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, _ = _bundle(session, seeded["paper_id"], "sections")
        status = ContentWebReviewLocalVerificationService(session).status(UUID(created["bundle_id"]))
        assert status["formal_eligibility_delta"] == {"writing": 0, "citation": 0, "rag": 0}
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0
        assert session.scalar(select(func.count()).select_from(ExtractionFieldReview)) == 0


def test_confirmed_creates_review_and_locator_without_correction_and_dedupes_page(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        results = [_result(check) for check in plan["required_object_checks"]]
        applied = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=UUID(created["bundle_id"]), results=results, partial=False,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert applied["status"] == "finalized"
        assert applied["metrics"]["logical_page_read_count"] == 1
        assert applied["metrics"]["physical_page_read_attempt_count"] == 1
        assert applied["metrics"]["page_cache_hit_count"] >= 1
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0
        assert session.scalar(select(func.count()).select_from(ExtractionFieldReview)) == 2
        assert session.scalar(select(func.count()).select_from(EvidenceLocator).where(
            EvidenceLocator.parser_source == "content_web_local_verification"
        )) == 2
        first = session.get(PaperSection, UUID(seeded["first_section_id"]))
        assert content_object_gate(session, "sections", first).can_use_for_writing is True


def test_revised_is_atomic_updates_value_and_materializes_canonical_review(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        check = next(row for row in plan["required_object_checks"] if row["target_id"] == seeded["first_section_id"])
        applied = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=UUID(created["bundle_id"]),
            results=[_result(check, "REVISED", "revised grounded statement")],
            partial=True, source_prefix="ide_ai", identity_verified=True,
        )
        result = applied["submitted_results"][0]
        assert result["status"] == "applied" and result["correction_id"]
        assert result["target_type"] == "paper_section"
        assert result["target_id"] == seeded["first_section_id"]
        assert result["field_name"] == "text"
        assert result["review_id"] and result["locator_id"]
        assert session.get(PaperSection, UUID(seeded["first_section_id"])).text == "revised grounded statement"
        correction = session.get(PaperCorrection, UUID(result["correction_id"]))
        assert correction.status == "approved" and correction.source == "ide_ai"


def test_rejected_suppresses_real_old_gate_but_projection_flags_do_not_define_plan(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    paper_id = UUID(seeded["paper_id"])
    section_id = seeded["first_section_id"]
    with _factory(setup_test_db).begin() as session:
        section = session.get(PaperSection, UUID(section_id))
        review = ExtractionFieldReview(
            paper_id=paper_id, target_type="sections", target_id=section_id, field_name="text",
            original_value=section.text, reviewed_value=section.text, evidence_text=section.text,
            reviewer_status="verified", target_resolution_status="active", reviewer="human",
        )
        session.add(review)
        session.add(EvidenceLocator(
            paper_id=paper_id, target_type="sections", target_id=section_id, field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="human",
        ))
        session.add(ContentEvidenceItem(
            paper_id=paper_id, category="section", source_type="section", source_id=section_id,
            content=section.text, evidence_text=section.text, review_status="needs_review",
            citation_status="blocked", risk_flags=[],
        ))
        session.flush()
        service = ContentWebReviewBundleV2Service(session)
        created = service.generate(paper_id=paper_id, module="sections")
        decision = {target["plan_item_id"]: ("REJECT" if target["target_id"] == section_id else "NEEDS_HUMAN") for target in created["manifest"]["targets"]}
        assert service.validate_web_proposal(UUID(created["bundle_id"]), _proposal(created["manifest"], decision))["valid"]
        plan = service.local_verification_plan(UUID(created["bundle_id"]))
        assert len(plan["required_object_checks"]) == 1
        check = plan["required_object_checks"][0]
        assert check["decision"] == "REJECT"
        applied = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(check, "REJECTED")], partial=False,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert applied["submitted_results"][0]["status"] == "applied"
        assert content_object_gate(session, "sections", section).can_use_for_writing is False
        projection = session.scalar(select(ContentEvidenceItem).where(ContentEvidenceItem.source_id == section_id))
        projection.review_status = "safe_verified"
        projection.citation_status = "citable"
        session.flush()
        assert content_object_gate(session, "sections", section).can_use_for_writing is False


def test_needs_human_is_persisted_without_formal_write_and_identity_is_required(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        check = plan["required_object_checks"][0]
        service = ContentWebReviewLocalVerificationService(session)
        with pytest.raises(PermissionError):
            service.apply(
                bundle_id=UUID(created["bundle_id"]), results=[_result(check)], partial=True,
                source_prefix="open_mcp", identity_verified=False,
            )
        applied = service.apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(check, "NEEDS_HUMAN")], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert applied["submitted_results"][0]["status"] == "awaiting_human"
        assert session.scalar(select(func.count()).select_from(PaperCorrection)) == 0
        assert session.scalar(select(func.count()).select_from(ExtractionFieldReview)) == 0


def test_partial_stale_does_not_roll_back_valid_object_and_retry_is_idempotent(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        results = [_result(check) for check in plan["required_object_checks"]]
        session.get(PaperSection, UUID(seeded["first_section_id"])).text = "externally changed"
        session.flush()
        service = ContentWebReviewLocalVerificationService(session)
        applied = service.apply(
            bundle_id=UUID(created["bundle_id"]), results=results, partial=False,
            source_prefix="ide_ai", identity_verified=True,
        )
        statuses = {row["status"] for row in applied["submitted_results"]}
        assert statuses == {"applied", "stale"}
        retried = service.apply(
            bundle_id=UUID(created["bundle_id"]), results=results, partial=False,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert retried["idempotent"] is True
        conflict = dict(results[0]); conflict["verification_note"] = "conflicting retry"
        with pytest.raises(ContentWebReviewLocalVerificationError, match="idempotency_conflict"):
            service.apply(
                bundle_id=UUID(created["bundle_id"]), results=[conflict], partial=True,
                source_prefix="ide_ai", identity_verified=True,
            )


def test_same_writing_card_fields_use_latest_object_gate_without_false_stale(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "writing_cards")
        first, second = plan["required_object_checks"]
        service = ContentWebReviewLocalVerificationService(session)
        one = service.apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(first)], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert one["submitted_results"][0]["status"] == "applied"
        two = service.apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(second)], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert two["submitted_results"][0]["status"] == "applied"
        assert two["status"] == "finalized"


def test_direct_review_write_without_lock_fails(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        section = session.get(PaperSection, UUID(seeded["first_section_id"]))
        with pytest.raises(ValueError, match="module_write_lock_required"):
            ReviewService(session).apply_content_verification_review(
                paper_id=section.paper_id, collection="sections", target_id=str(section.id), field_name="text",
                original_value=section.text, reviewed_value=section.text, reviewer_status="verified",
                reviewer="ide_ai", reviewer_note="bypass", evidence_payload={"page": 1, "quoted_text": section.text},
                write_lock_tokens=None, write_lock_owner="ide_ai",
            )


def test_low_risk_verified_layout_allows_empty_checked_pages(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    preview = tmp_path / "verified-page.png"
    preview.write_bytes(b"verified materialized page")
    with _factory(setup_test_db).begin() as session:
        section = session.get(PaperSection, UUID(seeded["first_section_id"]))
        session.delete(session.get(PaperSection, UUID(seeded["second_section_id"])))
        session.add(EvidenceLocator(
            paper_id=section.paper_id, target_type="paper_section", target_id=str(section.id), field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="layout_verifier",
            bbox={"full_page_image_path": str(preview), "layout_consistency_status": "verified"},
        ))
        session.flush()
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        check = next(row for row in plan["required_object_checks"] if row["target_id"] == str(section.id))
        assert check["layout_consistency_status"] == "verified"
        assert check["requires_page_render"] is False
        assert plan["unique_page_count"] == 0
        assert plan["page_batches"] == []
        assert len(plan["optional_page_refs"]) == 1
        result = _result(check)
        assert result["checked_pages"] == []
        claimed_optional_page = _result(check)
        claimed_optional_page["checked_pages"] = [{
            "source_paper_id": check["source_paper_id"],
            "source_pdf_sha256": check["source_pdf_sha256"],
            "page": check["page"],
            "page_asset_sha256": check["page_asset_sha256"],
        }]
        with pytest.raises(
            ContentWebReviewLocalVerificationError,
            match="content_web_local_verification_wrong_checked_pages",
        ):
            ContentWebReviewLocalVerificationService(session).apply(
                bundle_id=UUID(created["bundle_id"]), results=[claimed_optional_page], partial=True,
                source_prefix="ide_ai", identity_verified=True,
            )
        applied = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=UUID(created["bundle_id"]), results=[result], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert applied["submitted_results"][0]["status"] == "applied"
        assert applied["metrics"]["logical_page_read_count"] == 0
        assert applied["metrics"]["physical_page_read_attempt_count"] == 0


def test_layout_unchecked_still_requires_full_page_tuple(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        _, plan = _bundle(session, seeded["paper_id"], "sections")
        check = plan["required_object_checks"][0]
        assert "unchecked" in check["layout_consistency_status"]
        assert check["requires_page_render"] is True
        assert _result(check)["checked_pages"] == [{
            "source_paper_id": check["source_paper_id"],
            "source_pdf_sha256": check["source_pdf_sha256"],
            "page": check["page"],
            "page_asset_sha256": check["page_asset_sha256"],
        }]


def test_untrusted_locator_cannot_forge_verified_layout(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    preview = tmp_path / "forged-verified-page.png"
    preview.write_bytes(b"untrusted page")
    with _factory(setup_test_db).begin() as session:
        section = session.get(PaperSection, UUID(seeded["first_section_id"]))
        session.add(EvidenceLocator(
            paper_id=section.paper_id, target_type="paper_section", target_id=str(section.id), field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="ordinary_parser",
            bbox={"full_page_image_path": str(preview), "layout_consistency_status": "verified"},
        ))
        session.flush()
        _, plan = _bundle(session, seeded["paper_id"], "sections")
        check = next(row for row in plan["required_object_checks"] if row["target_id"] == str(section.id))
        assert check["layout_consistency_status"] == "asset_available_unchecked"
        assert check["requires_page_render"] is True


def test_optional_and_required_targets_only_read_required_page_batch(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    preview = tmp_path / "optional-page.png"
    preview.write_bytes(b"trusted optional page")
    with _factory(setup_test_db).begin() as session:
        first = session.get(PaperSection, UUID(seeded["first_section_id"]))
        session.add(EvidenceLocator(
            paper_id=first.paper_id, target_type="paper_section", target_id=str(first.id), field_name="text",
            source_type="pdf", page=1, evidence_text=first.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="layout_verifier",
            bbox={"full_page_image_path": str(preview), "layout_consistency_status": "verified"},
        ))
        session.flush()
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        by_id = {row["target_id"]: row for row in plan["required_object_checks"]}
        assert by_id[seeded["first_section_id"]]["requires_page_render"] is False
        assert by_id[seeded["second_section_id"]]["requires_page_render"] is True
        assert plan["unique_page_count"] == 1
        assert plan["page_batches"][0]["plan_item_ids"] == [by_id[seeded["second_section_id"]]["plan_item_id"]]
        assert [row["plan_item_id"] for row in plan["optional_page_refs"]] == [by_id[seeded["first_section_id"]]["plan_item_id"]]
        applied = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(row) for row in by_id.values()], partial=False,
            source_prefix="ide_ai", identity_verified=True,
        )
        assert applied["metrics"]["logical_page_read_count"] == 1
        assert applied["metrics"]["physical_page_read_attempt_count"] == 1


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda payload: payload.update(object_snapshot_hash="0" * 64), "wrong_object_hash"),
        (lambda payload: payload.update(checked_evidence_ids=["evidence:wrong"]), "wrong_evidence_ids"),
        (lambda payload: payload.update(checked_pages=[]), "wrong_checked_pages"),
        (lambda payload: payload.pop("verification_note"), "missing_result_field"),
    ],
)
def test_strict_result_binding_rejects_wrong_hash_evidence_page_and_missing_fields(
    setup_test_db, tmp_path, mutate, error
):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        payload = _result(plan["required_object_checks"][0])
        mutate(payload)
        with pytest.raises(ContentWebReviewLocalVerificationError, match=error):
            ContentWebReviewLocalVerificationService(session).apply(
                bundle_id=UUID(created["bundle_id"]), results=[payload], partial=True,
                source_prefix="ide_ai", identity_verified=True,
            )
        assert session.scalar(select(func.count()).select_from(ContentWebReviewLocalVerificationResult)) == 0


def test_policy_pdf_and_page_asset_changes_persist_dependency_scoped_stale(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path, "A")
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        bundle = ContentWebReviewBundleV2Service(session)._bundle(UUID(created["bundle_id"]))
        bundle.policy_version = "content_web_review_bundle_v2.old"
        result = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=bundle.id, results=[_result(plan["required_object_checks"][0])], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )["submitted_results"][0]
        assert result["status"] == "stale"
        assert "policy_version_changed" in result["stale_reasons"]

    seeded = _seed(setup_test_db, tmp_path, "B")
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        Path(session.get(Paper, UUID(seeded["paper_id"])).pdf_path).write_bytes(b"changed PDF")
        result = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(plan["required_object_checks"][0])], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )["submitted_results"][0]
        assert result["status"] == "stale"
        assert "source_pdf_changed" in result["stale_reasons"]

    seeded = _seed(setup_test_db, tmp_path, "C")
    preview = tmp_path / "mutable-page.png"; preview.write_bytes(b"page before")
    with _factory(setup_test_db).begin() as session:
        section = session.get(PaperSection, UUID(seeded["first_section_id"]))
        session.add(EvidenceLocator(
            paper_id=section.paper_id, target_type="paper_section", target_id=str(section.id), field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="layout_verifier",
            bbox={"full_page_image_path": str(preview), "layout_consistency_status": "verified"},
        ))
        session.flush()
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        check = next(row for row in plan["required_object_checks"] if row["target_id"] == str(section.id))
        preview.write_bytes(b"page after")
        result = ContentWebReviewLocalVerificationService(session).apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(check)], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )["submitted_results"][0]
        assert result["status"] == "stale"
        assert {"page_asset_changed", "evidence_asset_changed"} <= set(result["stale_reasons"])


def test_review_gate_change_is_stale_but_unrelated_si_pdf_change_is_not(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    si_pdf = tmp_path / "si.pdf"; _pdf(si_pdf)
    with _factory(setup_test_db).begin() as session:
        main = session.get(Paper, UUID(seeded["paper_id"]))
        si = Paper(paper_code="WV301-SI", title="SI", pdf_path=str(si_pdf), authors=[])
        session.add(si); session.flush()
        session.add(PaperRelationship(
            source_paper_id=main.id, target_paper_id=si.id, relationship_type="supplementary"
        ))
        session.flush()
        created, _ = _bundle(session, seeded["paper_id"], "sections")
        bundle_id = UUID(created["bundle_id"])
        si_pdf.write_bytes(b"changed unrelated SI")
        plan = ContentWebReviewBundleV2Service(session).local_verification_plan(bundle_id)
        assert plan["status"] != "stale"

        section = session.get(PaperSection, UUID(seeded["first_section_id"]))
        session.add(ExtractionFieldReview(
            paper_id=main.id, target_type="sections", target_id=str(section.id), field_name="text",
            original_value=section.text, reviewed_value=section.text, evidence_text=section.text,
            reviewer_status="verified", target_resolution_status="active", reviewer="external",
        ))
        session.add(EvidenceLocator(
            paper_id=main.id, target_type="sections", target_id=str(section.id), field_name="text",
            source_type="pdf", page=1, evidence_text=section.text, locator_status="exact_page",
            locator_confidence=1.0, parser_source="external",
        ))
        session.flush()
        stale = ContentWebReviewBundleV2Service(session).local_verification_plan(bundle_id)
        assert stale["status"] == "stale"
        assert "review_gate" in stale["stale"]["changed_dependencies"]


def test_status_api_is_read_only_and_no_http_apply_route_exists(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, _ = _bundle(session, seeded["paper_id"], "sections")
        bundle_id = created["bundle_id"]
    response = TestClient(app).get(
        f"/api/content-knowledge/review-bundles/{bundle_id}/local-verification-status"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object_counts"]["pending"] == payload["object_counts"]["required"]
    assert payload["formal_eligibility_delta"] == {"writing": 0, "citation": 0, "rag": 0}
    local_routes = [
        route for route in app.routes
        if "local-verification" in getattr(route, "path", "")
    ]
    assert local_routes
    assert all("POST" not in getattr(route, "methods", set()) for route in local_routes)


def test_unknown_duplicate_and_same_owner_preexisting_lock_are_rejected_or_isolated(setup_test_db, tmp_path):
    seeded = _seed(setup_test_db, tmp_path)
    with _factory(setup_test_db).begin() as session:
        created, plan = _bundle(session, seeded["paper_id"], "sections")
        check = plan["required_object_checks"][0]
        service = ContentWebReviewLocalVerificationService(session)
        unknown = _result(check); unknown["plan_item_id"] = "00000000-0000-0000-0000-000000000000"
        with pytest.raises(ContentWebReviewLocalVerificationError, match="unknown_plan_item"):
            service.apply(
                bundle_id=UUID(created["bundle_id"]), results=[unknown], partial=True,
                source_prefix="ide_ai", identity_verified=True,
            )
        with pytest.raises(ContentWebReviewLocalVerificationError, match="duplicate_plan_item"):
            service.apply(
                bundle_id=UUID(created["bundle_id"]), results=[_result(check), _result(check)], partial=True,
                source_prefix="ide_ai", identity_verified=True,
            )
        lock = ModuleWriteLockService(session).acquire(
            paper_id=UUID(seeded["paper_id"]), module_name="sections", locked_by="ide_ai", ttl_minutes=5
        )
        failed = service.apply(
            bundle_id=UUID(created["bundle_id"]), results=[_result(check)], partial=True,
            source_prefix="ide_ai", identity_verified=True,
        )["submitted_results"][0]
        assert failed["status"] == "failed"
        assert failed["error_code"] == "module_write_lock_conflict"
        assert session.get(type(lock), lock.id).status == "active"
        ModuleWriteLockService(session).release(lock_token=lock.lock_token, released_by="ide_ai")
