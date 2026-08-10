from __future__ import annotations

from pathlib import Path
from uuid import UUID

import fitz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    EvidenceClaim,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperSection,
)
from app.schemas.ai_verification import AIVerificationSubmission
from app.services.ai_verification_service import AIVerificationService, AuthenticatedAIVerificationIdentity
from app.services.content_writing_plan_service import ContentWritingPlanService
from app.services.evidence_page_recovery import EvidencePageRecoveryService, PaperPageTextProvider
from app.services.section_page_fragment_materialization_service import (
    SectionPageFragmentMaterializationService,
)
from app.utils.ai_verification import AI_VERIFICATION_CAPABILITY, ai_target_fingerprint
from app.utils.review_safety import content_object_gate


def _multipage_pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(60, 60, 540, 780), text, fontsize=10)
    document.save(path)
    document.close()


def _paper_and_claim(
    session: Session,
    tmp_path: Path,
    *,
    pages: list[str],
    claim_text: str,
    evidence_text: str,
) -> tuple[Paper, MechanismClaim]:
    pdf_path = tmp_path / "recovery.pdf"
    _multipage_pdf(pdf_path, pages)
    paper = Paper(title="Evidence recovery", pdf_path=str(pdf_path), authors=["Tester"])
    session.add(paper)
    session.flush()
    claim = MechanismClaim(
        paper_id=paper.id,
        claim_type="mechanism",
        claim_text=claim_text,
        evidence_text=evidence_text,
        evidence_types=["pdf_text"],
    )
    session.add(claim)
    session.flush()
    return paper, claim


def _identity() -> AuthenticatedAIVerificationIdentity:
    return AuthenticatedAIVerificationIdentity(
        source_identity="mcp:single-verifier",
        source_label="single-verifier",
        model_agent="codex-single-ai",
        capabilities=frozenset({AI_VERIFICATION_CAPABILITY}),
        identity_verified=True,
    )


def test_page_provider_uses_physical_one_based_pdf_pages(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper, _claim = _paper_and_claim(
            session,
            tmp_path,
            pages=["Page one body text and Figure 1 caption.", "Page two body text."],
            claim_text="Page one body text",
            evidence_text="Page one body text and Figure 1 caption.",
        )
        provider = PaperPageTextProvider()
        first = provider.read_page(paper, 1)
        second = provider.read_page(paper, 2)
        assert first.status == "ok"
        assert first.page == 1
        assert "Page one body text" in first.text
        assert "Figure 1 caption" in first.text
        assert second.page == 2
        assert "Page two body text" in second.text
        assert provider.read_page(paper, 3).status == "invalid_pdf_page"


def test_exact_recovery_handles_whitespace_hyphenation_and_is_idempotent(setup_test_db, tmp_path):
    evidence = "The catalyst enables rapid polysulfide conversion."
    with Session(setup_test_db) as session:
        paper, claim = _paper_and_claim(
            session,
            tmp_path,
            pages=["The catalyst enables rapid poly-\nsulfide conversion."],
            claim_text=evidence,
            evidence_text=evidence,
        )
        service = EvidencePageRecoveryService(session)
        kwargs = dict(
            paper=paper,
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            target_value=claim.claim_text,
            evidence_text=claim.evidence_text or "",
            evidence_types=claim.evidence_types,
        )
        first = service.recover_for_target(**kwargs)
        second = service.recover_for_target(**kwargs)
        assert first == second
        assert first["status"] == "recovered"
        candidate = first["candidates"][0]
        assert candidate["locator_status"] == "exact_page"
        assert candidate["page"] == 1
        assert candidate["quoted_text"] in service.page_provider.read_page(paper, 1).text
        assert first["database_writes"] is False


def test_cross_page_boundary_never_becomes_exact_page_candidate(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper, claim = _paper_and_claim(
            session,
            tmp_path,
            pages=["The first half ends with lithium", "sulfide conversion starts here."],
            claim_text="lithium sulfide conversion",
            evidence_text="lithium sulfide conversion",
        )
        result = EvidencePageRecoveryService(session).recover_for_target(
            paper=paper,
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            target_value=claim.claim_text,
            evidence_text=claim.evidence_text or "",
        )
        assert result["exact_candidate_count"] == 0
        assert all(item["locator_status"] == "approximate" for item in result["candidates"])


def test_body_section_recovers_independent_physical_page_fragments_without_unlocking_parent(
    setup_test_db,
    tmp_path,
):
    first = "Fe-N4 sites accelerate Li2S4 conversion at the cathode."
    second = "The resulting pathway lowers the reported barrier to 0.42 eV."
    pdf_path = tmp_path / "body-pages.pdf"
    _multipage_pdf(pdf_path, [first, second])
    with Session(setup_test_db) as session:
        paper = Paper(title="Page fragments", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        section = PaperSection(
            paper_id=paper.id,
            section_title="Results",
            section_type="body",
            text=f"{first} {second}",
            page_start=None,
            page_end=None,
        )
        session.add(section)
        session.flush()

        service = EvidencePageRecoveryService(session)
        recovered = service.recover_section_page_fragments(paper=paper, section=section)
        assert recovered == service.recover_section_page_fragments(paper=paper, section=section)
        assert recovered["status"] == "recovered"
        assert recovered["physical_page_numbering"] == "1_based_pdf"
        assert recovered["physical_pages"] == [1, 2]
        assert recovered["fragment_count"] == 2
        assert recovered["database_writes"] is False
        assert recovered["embedding_role"] == "retrieval_only"
        assert [item["page"] for item in recovered["fragments"]] == [1, 2]
        assert all(item["page_start"] == item["page_end"] == item["page"] for item in recovered["fragments"])
        assert all(item["checks"]["text_exists_on_pdf_page"] for item in recovered["fragments"])
        assert all(item["checks"]["numeric_consistency"] for item in recovered["fragments"])
        assert all(item["checks"]["unit_consistency"] for item in recovered["fragments"])

        partial_parent = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[
                AIVerificationSubmission(
                    target_type="sections",
                    target_id=str(section.id),
                    field_name="text",
                    decision="accept",
                    confidence=0.99,
                    evidence_text=first,
                    page=1,
                    reasoning_summary="A local page quote cannot authorize the cross-page parent.",
                    expected_target_fingerprint=ai_target_fingerprint("sections", section),
                )
            ],
            identity=_identity(),
            dry_run=True,
        )
        assert partial_parent["items"][0]["status"] != "ai_verified"
        assert "complete_section_page_coverage" in partial_parent["items"][0]["blocked_reasons"]
        parent_gate_before_embedding = content_object_gate(session, "sections", section)
        assert parent_gate_before_embedding.can_use_for_writing is False
        section.embedding = [0.999] * 1024
        session.flush()
        parent_gate_after_embedding = content_object_gate(session, "sections", section)
        assert parent_gate_after_embedding.can_use_for_writing is False
        assert parent_gate_after_embedding.blocked_reasons == parent_gate_before_embedding.blocked_reasons

        candidate = recovered["fragments"][0]
        materialized = SectionPageFragmentMaterializationService(session).materialize(
            paper_id=paper.id,
            parent_section_id=section.id,
            candidates=[
                {
                    "fragment_id": candidate["fragment_id"],
                    "fragment_fingerprint": candidate["fragment_fingerprint"],
                }
            ],
            identity=_identity(),
            dry_run=False,
            commit=False,
        )
        assert materialized["items"][0]["status"] == "pending"
        fragment = session.get(EvidenceClaim, UUID(candidate["fragment_id"]))
        assert fragment is not None
        verified_fragment = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[
                AIVerificationSubmission(
                    target_type="section_page_fragments",
                    target_id=str(fragment.id),
                    field_name="text",
                    decision="accept",
                    confidence=0.99,
                    evidence_text=fragment.evidence_text,
                    page=fragment.page_start,
                    reasoning_summary="Exact page fragment remains bound to one physical PDF page.",
                    expected_target_fingerprint=ai_target_fingerprint(
                        "section_page_fragments", fragment
                    ),
                )
            ],
            identity=_identity(),
            dry_run=False,
            commit=False,
        )
        assert verified_fragment["items"][0]["status"] == "ai_verified"
        fragment_gate = content_object_gate(session, "section_page_fragments", fragment)
        assert fragment_gate.can_use_for_writing is True
        assert fragment_gate.can_use_for_citation is True
        assert content_object_gate(session, "sections", section).can_use_for_writing is False

        plan = ContentWritingPlanService(session).build(
            query="Fe-N4 sites accelerate Li2S4 conversion",
            paper_ids=[str(paper.id)],
            evidence_types=["sections"],
            evidence_budget=3,
        )
        selected = plan["selected_evidence"]
        assert any(
            item["object_id"] == str(fragment.id)
            and item["evidence_type"] == "sections"
            and item["can_use_for_writing"] is True
            and item["can_use_for_citation"] is True
            for item in selected
        )
        assert all(item["object_id"] != str(section.id) for item in selected)


def test_numeric_conflict_is_approximate_and_cannot_authorize_verification(setup_test_db, tmp_path):
    with Session(setup_test_db) as session:
        paper, claim = _paper_and_claim(
            session,
            tmp_path,
            pages=["The catalyst barrier is 2.5 eV under the tested condition."],
            claim_text="The catalyst barrier is 3.5 eV under the tested condition.",
            evidence_text="The catalyst barrier is 3.5 eV under the tested condition.",
        )
        result = EvidencePageRecoveryService(session).recover_for_target(
            paper=paper,
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            target_value=claim.claim_text,
            evidence_text=claim.evidence_text or "",
        )
        assert result["status"] == "approximate_only"
        assert result["exact_candidate_count"] == 0
        assert result["candidates"][0]["numeric_consistency"] is False
        assert result["candidates"][0]["can_jump_to_pdf_page"] is False


def test_missing_or_unreadable_pdf_returns_explicit_status(setup_test_db, tmp_path):
    bad_pdf = tmp_path / "broken.pdf"
    bad_pdf.write_bytes(b"not a pdf")
    with Session(setup_test_db) as session:
        paper = Paper(title="Broken", pdf_path=str(bad_pdf), authors=["Tester"])
        session.add(paper)
        session.flush()
        record = PaperPageTextProvider().read_page(paper, 1)
        assert record.status == "extraction_failed"
        assert record.text == ""


def test_task_recovery_flows_into_zero_write_dry_run(setup_test_db, tmp_path):
    evidence = "Fe-N4 sites accelerate Li2S4 conversion and suppress shuttling."
    with Session(setup_test_db) as session:
        paper, claim = _paper_and_claim(
            session,
            tmp_path,
            pages=[evidence, "Unrelated supplementary discussion."],
            claim_text=evidence,
            evidence_text=evidence,
        )
        session.commit()
        before = {
            "locators": session.scalar(select(func.count(EvidenceLocator.id))),
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
            "audits": session.scalar(select(func.count(AuditLog.id))),
        }
        service = AIVerificationService(session)
        tasks = service.list_tasks(paper_id=paper.id, limit=20, recover_evidence=True)
        task = next(item for item in tasks["tasks"] if item["target_id"] == str(claim.id))
        candidate = task["evidence_candidates"][0]
        result = service.process_batch(
            paper_id=paper.id,
            submissions=[
                AIVerificationSubmission(
                    target_type=task["target_type"],
                    target_id=task["target_id"],
                    field_name=task["field_name"],
                    decision="accept",
                    confidence=0.99,
                    evidence_text=candidate["quoted_text"],
                    page=candidate["page"],
                    reasoning_summary="Recovered exact text is present on the physical PDF page.",
                    expected_target_fingerprint=task["target_snapshot_fingerprint"],
                    expected_write_version=task["expected_write_version"],
                )
            ],
            identity=_identity(),
            dry_run=True,
        )
        after = {
            "locators": session.scalar(select(func.count(EvidenceLocator.id))),
            "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
            "audits": session.scalar(select(func.count(AuditLog.id))),
        }
        assert tasks["evidence_recovery_summary"]["exact_candidate_count"] >= 1
        assert result["actor_type"] == "ai"
        assert result["auto_repaired"] == 1
        assert result["database_writes"] is False
        assert before == after
