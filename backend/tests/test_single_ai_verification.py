from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTAuditIssue,
    DFTResult,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
)
from app.schemas.ai_verification import AIVerificationSubmission
from app.services.ai_verification_service import (
    AIVerificationService,
    AuthenticatedAIVerificationIdentity,
)
from app.utils.ai_verification import AI_VERIFICATION_CAPABILITY, ai_target_fingerprint
from app.utils.review_safety import content_object_gate, is_safe_verified_review, is_unsafe_review_status


def _pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _identity(*, capable: bool = True, verified: bool = True) -> AuthenticatedAIVerificationIdentity:
    return AuthenticatedAIVerificationIdentity(
        source_identity="mcp:single-verifier",
        source_label="single-verifier",
        model_agent="codex-single-ai",
        capabilities=frozenset({AI_VERIFICATION_CAPABILITY}) if capable else frozenset(),
        identity_verified=verified,
    )


def _submission(claim: MechanismClaim, evidence: str, **overrides) -> AIVerificationSubmission:
    payload = {
        "target_type": "mechanism_claims",
        "target_id": str(claim.id),
        "field_name": "claim_text",
        "decision": "accept",
        "confidence": 0.97,
        "evidence_text": evidence,
        "page": 1,
        "reasoning_summary": "The claim is directly supported on the cited PDF page.",
        "expected_target_fingerprint": ai_target_fingerprint("mechanism_claims", claim),
    }
    payload.update(overrides)
    return AIVerificationSubmission.model_validate(payload)


def _seed_claim(session: Session, tmp_path: Path, *, claim_text: str, evidence: str) -> tuple[Paper, MechanismClaim]:
    pdf_path = tmp_path / "single-ai-evidence.pdf"
    _pdf(pdf_path, evidence)
    paper = Paper(title="Single AI verification", pdf_path=str(pdf_path), authors=["Tester"])
    session.add(paper)
    session.flush()
    claim = MechanismClaim(
        paper_id=paper.id,
        claim_type="mechanism",
        claim_text=claim_text,
        evidence_types=["pdf_text"],
        evidence_text=evidence,
    )
    session.add(claim)
    session.flush()
    return paper, claim


@pytest.mark.parametrize(
    ("claim_count", "expected_page_sizes"),
    [
        (21, [20, 1]),
        (22, [20, 2]),
        (25, [20, 5]),
        (50, [20, 20, 10]),
    ],
)
def test_single_ai_task_pagination_is_stable_complete_and_read_only(
    setup_test_db,
    claim_count,
    expected_page_sizes,
):
    with Session(setup_test_db) as session:
        paper = Paper(
            title=f"Pagination {claim_count}",
            pdf_path=f"pagination-{claim_count}.pdf",
            authors=["Tester"],
        )
        other_paper = Paper(
            title="Pagination isolation",
            pdf_path=f"pagination-isolation-{claim_count}.pdf",
            authors=["Tester"],
        )
        session.add_all([paper, other_paper])
        session.flush()
        claims = [
            MechanismClaim(
                paper_id=paper.id,
                claim_type="mechanism",
                claim_text=f"Mechanism claim {index:02d}",
                evidence_types=["pdf_text"],
                evidence_text=f"Evidence {index:02d}",
            )
            for index in range(claim_count)
        ]
        isolated_claim = MechanismClaim(
            paper_id=other_paper.id,
            claim_type="mechanism",
            claim_text="Must never cross into another paper",
            evidence_types=["pdf_text"],
            evidence_text="Isolated evidence",
        )
        session.add_all([*claims, isolated_claim])
        session.commit()

        expected_ids = {str(claim.id) for claim in claims}
        isolated_id = str(isolated_claim.id)
        before = {
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
            "audits": session.scalar(select(func.count(AuditLog.id))),
            "locators": session.scalar(select(func.count(EvidenceLocator.id))),
        }

        offset = 0
        collected_ids: list[str] = []
        page_sizes: list[int] = []
        while True:
            page = AIVerificationService(session).list_tasks(
                paper_id=paper.id,
                limit=20,
                offset=offset,
                recover_evidence=False,
                target_type="mechanism_claims",
            )
            page_ids = [task["target_id"] for task in page["tasks"]]
            page_sizes.append(len(page_ids))
            collected_ids.extend(page_ids)

            assert page["paper_id"] == str(paper.id)
            assert page["target_type"] == "mechanism_claims"
            assert page["total"] == claim_count
            assert page["returned"] == len(page_ids)
            assert page["task_count"] == len(page_ids)
            assert page["offset"] == offset
            assert page["limit"] == 20
            assert page["max_page_size"] == 50
            assert page["batch_limit"] == 20
            assert page["single_ai"] is True
            assert page["second_ai_used"] is False
            assert page["database_writes"] is False
            assert all(task["paper_id"] == str(paper.id) for task in page["tasks"])
            assert all(task["target_type"] == "mechanism_claims" for task in page["tasks"])
            assert isolated_id not in page_ids

            if not page["has_more"]:
                assert page["next_offset"] is None
                break
            assert page["next_offset"] == offset + len(page_ids)
            offset = page["next_offset"]

        assert page_sizes == expected_page_sizes
        assert len(collected_ids) == claim_count
        assert len(set(collected_ids)) == claim_count
        assert set(collected_ids) == expected_ids

        capped = AIVerificationService(session).list_tasks(
            paper_id=paper.id,
            limit=999,
            offset=0,
            recover_evidence=False,
            target_type="mechanism_claims",
        )
        assert capped["limit"] == 50
        assert capped["returned"] == claim_count
        assert capped["has_more"] is False

        after = {
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
            "audits": session.scalar(select(func.count(AuditLog.id))),
            "locators": session.scalar(select(func.count(EvidenceLocator.id))),
        }
        assert after == before


def test_single_ai_dry_run_zero_writes_then_formal_accept_is_idempotent_and_citable(setup_test_db, tmp_path):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion and suppress polysulfide shuttling."
    with Session(setup_test_db) as session:
        paper, claim = _seed_claim(session, tmp_path, claim_text=evidence, evidence=evidence)
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="mechanism_claims",
                target_id=str(claim.id),
                field_name="claim_text",
                page=1,
                evidence_text=evidence,
                locator_status="exact_page",
                locator_confidence=0.98,
                parser_source="pytest",
            )
        )
        session.commit()
        submission = _submission(claim, evidence)

        before = (
            session.scalar(select(func.count(ExtractionFieldReview.id))),
            session.scalar(select(func.count(AuditLog.id))),
        )
        dry_run = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=True,
        )
        after = (
            session.scalar(select(func.count(ExtractionFieldReview.id))),
            session.scalar(select(func.count(AuditLog.id))),
        )
        assert dry_run["auto_verified"] == 1
        assert dry_run["database_writes"] is False
        assert before == after

        formal = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=False,
        )
        review = session.scalar(select(ExtractionFieldReview))
        assert formal["auto_verified"] == 1
        assert review is not None
        assert review.reviewer_status == "ai_verified"
        verification = review.review_payload["ai_verification"]
        assert verification["actor_type"] == "ai"
        assert verification["single_ai"] is True
        assert verification["second_ai_used"] is False
        assert verification["source_identity"] == "mcp:single-verifier"
        gate = content_object_gate(session, "mechanism_claims", claim)
        assert gate.can_use_for_writing is True
        assert gate.can_use_for_citation is True

        audit_count = session.scalar(select(func.count(AuditLog.id)))
        repeated = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=False,
        )
        assert repeated["items"][0]["idempotent"] is True
        assert session.scalar(select(func.count(AuditLog.id))) == audit_count

        locator = session.scalar(select(EvidenceLocator))
        locator.evidence_text = "Locator evidence changed after verification."
        session.flush()
        assert content_object_gate(session, "mechanism_claims", claim).can_use_for_citation is False


def test_single_ai_corrects_supported_claim_and_stale_snapshot_revokes_gate(setup_test_db, tmp_path):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion."
    with Session(setup_test_db) as session:
        paper, claim = _seed_claim(session, tmp_path, claim_text="Fe-N4 sites slow Li2S4 conversion.", evidence=evidence)
        session.commit()
        submission = _submission(
            claim,
            evidence,
            decision="correct",
            proposed_value=evidence,
            reasoning_summary="The extracted direction was inverted; the PDF says accelerate.",
        )
        result = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=False,
        )
        assert result["auto_repaired"] == 1
        assert claim.claim_text == evidence
        assert content_object_gate(session, "mechanism_claims", claim).can_use_for_citation is True

        claim.claim_text = "A later mutation invalidates the accepted target snapshot."
        session.flush()
        stale_gate = content_object_gate(session, "mechanism_claims", claim)
        assert stale_gate.can_use_for_writing is False
        assert stale_gate.can_use_for_citation is False


@pytest.mark.parametrize(
    ("overrides", "outcome", "reason"),
    [
        ({"evidence_text": "This sentence is absent from the PDF."}, "auto_rejected", "evidence_on_pdf_page"),
        ({"page": None}, "exception", "page_valid"),
        ({"confidence": 0.2}, "exception", "confidence_threshold"),
    ],
)
def test_single_ai_routes_mismatch_and_missing_prerequisites_without_verified_state(
    setup_test_db,
    tmp_path,
    overrides,
    outcome,
    reason,
):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion."
    with Session(setup_test_db) as session:
        paper, claim = _seed_claim(session, tmp_path, claim_text=evidence, evidence=evidence)
        session.commit()
        result = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[_submission(claim, evidence, **overrides)],
            identity=_identity(),
            dry_run=False,
        )
        item = result["items"][0]
        assert item["outcome"] == outcome
        assert reason in item["blocked_reasons"]
        review = session.scalar(select(ExtractionFieldReview))
        assert review is not None
        assert review.reviewer_status != "ai_verified"


def test_single_ai_dft_numeric_unit_and_material_gates(setup_test_db, tmp_path):
    evidence = "On Fe-N4, the Li2S4 adsorption energy is -1.23 eV for the S8 to Li2S4 step."
    pdf_path = tmp_path / "dft-single-ai.pdf"
    _pdf(pdf_path, evidence)
    with Session(setup_test_db) as session:
        paper = Paper(title="DFT verification", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", metal_centers=["Fe"], coordination="N4")
        session.add(catalyst)
        session.flush()
        result_row = DFTResult(
            paper_id=paper.id,
            catalyst_sample_id=catalyst.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            reaction_step="S8 to Li2S4",
            evidence_text=evidence,
        )
        session.add(result_row)
        session.commit()
        submission = AIVerificationSubmission(
            target_type="dft_results",
            target_id=str(result_row.id),
            field_name="value",
            decision="accept",
            confidence=0.98,
            evidence_text=evidence,
            page=1,
            reasoning_summary="Value, unit, material, adsorbate, and reaction step agree.",
            expected_target_fingerprint=ai_target_fingerprint("dft_results", result_row),
        )
        accepted = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=False,
        )
        assert accepted["auto_repaired"] == 1  # exact locator was recovered
        review = session.scalar(select(ExtractionFieldReview))
        assert review.reviewer_status == "ai_verified"


def test_untrusted_identity_and_forged_ai_payload_cannot_authorize_content(setup_test_db, tmp_path):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion."
    with Session(setup_test_db) as session:
        paper, claim = _seed_claim(session, tmp_path, claim_text=evidence, evidence=evidence)
        session.commit()
        for identity in (_identity(capable=False), _identity(verified=False)):
            with pytest.raises(PermissionError):
                AIVerificationService(session).process_batch(
                    paper_id=paper.id,
                    submissions=[_submission(claim, evidence)],
                    identity=identity,
                    dry_run=True,
                )

        forged = {
            "reviewer_status": "ai_verified",
            "target_resolution_status": "active",
            "target_fingerprint": "forged",
            "review_payload": {"ai_verification": {"actor_type": "ai"}},
        }
        assert is_safe_verified_review(forged) is False
        assert is_unsafe_review_status(forged) is True


@pytest.mark.parametrize("field_name", ["claim_type", "key_species", "mechanism_direction"])
def test_non_core_mechanism_fields_never_authorize_the_object(setup_test_db, tmp_path, field_name):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion."
    with Session(setup_test_db) as session:
        paper, claim = _seed_claim(session, tmp_path, claim_text=evidence, evidence=evidence)
        session.commit()
        result = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[
                AIVerificationSubmission(
                    target_type="mechanism_claims",
                    target_id=str(claim.id),
                    field_name=field_name,
                    decision="accept",
                    confidence=0.99,
                    evidence_text=evidence,
                    page=1,
                    reasoning_summary="Non-core field must not authorize the claim.",
                    expected_target_fingerprint=ai_target_fingerprint("mechanism_claims", claim),
                )
            ],
            identity=_identity(),
            dry_run=False,
        )
        assert result["exception"] == 1
        assert result["items"][0]["status"] == "needs_human"
        assert content_object_gate(session, "mechanism_claims", claim).can_use_for_writing is False


def test_partial_batch_failure_keeps_success_atomic_without_half_writes(setup_test_db, tmp_path):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion."
    with Session(setup_test_db) as session:
        paper, claim = _seed_claim(session, tmp_path, claim_text=evidence, evidence=evidence)
        session.commit()
        invalid = _submission(claim, evidence).model_copy(
            update={"target_id": "00000000-0000-0000-0000-000000000000"}
        )
        result = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[_submission(claim, evidence), invalid],
            identity=_identity(),
            dry_run=False,
        )
        assert result["auto_repaired"] == 1
        assert result["exception"] == 1
        assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 1
        assert session.scalar(select(func.count(AuditLog.id))) == 1


def test_b0102_and_imported_dft_candidates_share_the_unified_single_ai_gate(setup_test_db, tmp_path):
    evidence_rows = (
        "On Fe-N4, Li2S4 adsorption energy is -1.23 eV for S8 to Li2S4.",
        "On Co-N4, Li2S adsorption energy is -0.88 eV for Li2S4 to Li2S.",
    )
    pdf_path = tmp_path / "dft-unified-candidates.pdf"
    document = fitz.open()
    page = document.new_page()
    for index, evidence in enumerate(evidence_rows):
        page.insert_text((72, 72 + index * 20), evidence)
    document.save(pdf_path)
    document.close()
    with Session(setup_test_db) as session:
        paper = Paper(title="Unified DFT candidates", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        rows = [
            DFTResult(
                paper_id=paper.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                reaction_step="S8 to Li2S4",
                evidence_text=evidence_rows[0],
                evidence_payload={"material_identity": "Fe-N4", "candidate_source": "b0102"},
                candidate_status="pending_ai_verification",
            ),
            DFTResult(
                paper_id=paper.id,
                adsorbate="Li2S",
                property_type="adsorption_energy",
                value=-0.88,
                unit="eV",
                reaction_step="Li2S4 to Li2S",
                evidence_text=evidence_rows[1],
                evidence_payload={"material_identity": "Co-N4", "candidate_source": "imported_dft"},
                candidate_status="system_candidate",
            ),
        ]
        session.add_all(rows)
        session.commit()
        service = AIVerificationService(session)
        tasks = service.list_tasks(paper_id=paper.id, limit=20)
        task_ids = {item["target_id"] for item in tasks["tasks"] if item["target_type"] == "dft_results"}
        assert task_ids == {str(row.id) for row in rows}
        submissions = [
            AIVerificationSubmission(
                target_type="dft_results",
                target_id=str(row.id),
                field_name="value",
                decision="accept",
                confidence=0.98,
                evidence_text=row.evidence_text,
                page=1,
                reasoning_summary="Unified DFT evidence gates pass.",
                expected_target_fingerprint=ai_target_fingerprint("dft_results", row),
            )
            for row in rows
        ]
        result = service.process_batch(
            paper_id=paper.id,
            submissions=submissions,
            identity=_identity(),
            dry_run=False,
        )
        assert result["auto_repaired"] == 2, result["items"]
        reviews = session.scalars(select(ExtractionFieldReview)).all()
        assert len(reviews) == 2
        assert {review.reviewer_status for review in reviews} == {"ai_verified"}


def test_unresolved_dft_conflict_routes_to_exception_and_body_cannot_forge_actor(setup_test_db, tmp_path):
    evidence = "On Fe-N4, Li2S4 adsorption energy is -1.23 eV for S8 to Li2S4."
    pdf_path = tmp_path / "dft-conflict.pdf"
    _pdf(pdf_path, evidence)
    with Session(setup_test_db) as session:
        paper = Paper(title="DFT conflict", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        row = DFTResult(
            paper_id=paper.id,
            adsorbate="Li2S4",
            property_type="adsorption_energy",
            value=-1.23,
            unit="eV",
            reaction_step="S8 to Li2S4",
            evidence_text=evidence,
            evidence_payload={"material_identity": "Fe-N4"},
        )
        session.add(row)
        session.flush()
        session.add(
            DFTAuditIssue(
                paper_id=paper.id,
                target_type="dft_results",
                target_id=str(row.id),
                result_id=row.id,
                issue_type="conflicting_value",
                status="needs_primary_ai",
                fingerprint="pytest-conflict",
            )
        )
        session.commit()
        submission = AIVerificationSubmission(
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            decision="accept",
            confidence=0.98,
            evidence_text=evidence,
            page=1,
            reasoning_summary="Conflict must block admission.",
            expected_target_fingerprint=ai_target_fingerprint("dft_results", row),
        )
        result = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=False,
        )
        assert result["exception"] == 1
        assert "no_unresolved_conflict" in result["items"][0]["blocked_reasons"]

        with pytest.raises(ValueError):
            AIVerificationSubmission.model_validate(
                {**submission.model_dump(), "actor_type": "human"}
            )
