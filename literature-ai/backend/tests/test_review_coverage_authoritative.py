from contextlib import contextmanager
import os
from pathlib import Path

import fitz
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import (
    Base,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperSection,
    WritingCard,
)
from app.schemas.ai_verification import AIVerificationSubmission
from app.services.ai_verification_service import (
    AIVerificationService,
    AuthenticatedAIVerificationIdentity,
)
from app.services.content_knowledge_review_service import ContentKnowledgeReviewService
from app.utils.ai_verification import (
    AI_VERIFICATION_CAPABILITY,
    ai_target_fingerprint,
)


def _pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(60, 60, 540, 780), text, fontsize=10)
    document.save(path)
    document.close()


def _identity() -> AuthenticatedAIVerificationIdentity:
    return AuthenticatedAIVerificationIdentity(
        source_identity="mcp:coverage-single-ai",
        source_label="coverage-single-ai",
        model_agent="codex-single-ai",
        capabilities=frozenset({AI_VERIFICATION_CAPABILITY}),
        identity_verified=True,
    )


def test_get_review_coverage_reports_authoritative_content_classes(monkeypatch, tmp_path):
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    settings = get_settings().model_copy(update={"database_url": os.environ["LITAI_TEST_DATABASE_URL"]})
    try:
        with Session(engine) as session:
            captions = [f"Figure {index}. Exact verified caption {index}." for index in range(1, 5)]
            verified_mechanism_texts = [
                f"The PDF supports safe mechanism claim {index}."
                for index in range(1, 12)
            ]
            pdf_path = tmp_path / "coverage.pdf"
            _pdf(pdf_path, [*captions, "\n".join(verified_mechanism_texts)])
            paper = Paper(title="Coverage authority", pdf_path=str(pdf_path), authors=[])
            session.add(paper)
            session.flush()
            caption_sections = [
                PaperSection(
                    paper_id=paper.id,
                    section_title=f"Figure {index}",
                    section_type="figure_caption",
                    text=caption,
                    page_start=index,
                    page_end=index,
                )
                for index, caption in enumerate(captions, start=1)
            ]
            body_sections = [
                PaperSection(
                    paper_id=paper.id,
                    section_title="Introduction" if index == 0 else "Results",
                    section_type="body",
                    text=(
                        "This long body section crosses multiple physical pages and has no complete page binding. "
                        f"Body object {index + 1}."
                    ),
                    page_start=None,
                    page_end=None,
                )
                for index in range(2)
            ]
            verified_mechanisms = [
                MechanismClaim(
                    paper_id=paper.id,
                    claim_type="conversion",
                    claim_text=f"Safe mechanism claim {index}",
                    evidence_text=evidence_text,
                )
                for index, evidence_text in enumerate(verified_mechanism_texts, start=1)
            ]
            blocked_mechanisms = [
                MechanismClaim(
                    paper_id=paper.id,
                    claim_type="conversion",
                    claim_text=f"Ambiguous mechanism claim {index}",
                    evidence_text=f"Ambiguous mechanism evidence {index}.",
                )
                for index in range(1, 10)
            ]
            card = WritingCard(
                paper_id=paper.id,
                research_gap="Writing card with an explicit exception decision",
            )
            session.add_all([
                *caption_sections,
                *body_sections,
                *verified_mechanisms,
                *blocked_mechanisms,
                card,
            ])
            session.flush()
            verified = AIVerificationService(session).process_batch(
                paper_id=paper.id,
                submissions=[
                    AIVerificationSubmission(
                        target_type="sections",
                        target_id=str(section.id),
                        field_name="text",
                        decision="accept",
                        confidence=0.99,
                        evidence_text=section.text,
                        page=int(section.page_start),
                        reasoning_summary="Exact caption on its physical PDF page.",
                        expected_target_fingerprint=ai_target_fingerprint("sections", section),
                    )
                    for section in caption_sections
                ],
                identity=_identity(),
                dry_run=False,
                commit=False,
            )
            assert verified["auto_repaired"] == 4
            verified_mechanism_result = AIVerificationService(session).process_batch(
                paper_id=paper.id,
                submissions=[
                    AIVerificationSubmission(
                        target_type="mechanism_claims",
                        target_id=str(mechanism.id),
                        field_name="claim_text",
                        decision="accept",
                        confidence=0.99,
                        evidence_text=mechanism.evidence_text,
                        page=5,
                        reasoning_summary="Exact mechanism evidence on its physical PDF page.",
                        expected_target_fingerprint=ai_target_fingerprint(
                            "mechanism_claims", mechanism
                        ),
                    )
                    for mechanism in verified_mechanisms
                ],
                identity=_identity(),
                dry_run=False,
                commit=False,
            )
            assert verified_mechanism_result["auto_repaired"] == 11
            session.add_all([
                *[
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="sections",
                        target_id=str(section.id),
                        field_name="text",
                        reviewer_status="exception",
                        target_resolution_status="active",
                        evidence_text=None,
                    )
                    for section in body_sections
                ],
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="writing_cards",
                    target_id=str(card.id),
                    field_name="evidence_chain",
                    reviewer_status="exception",
                    target_resolution_status="active",
                ),
                *[
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="mechanism_claims",
                        target_id=str(mechanism.id),
                        field_name="claim_text",
                        reviewer_status="needs_human",
                        target_resolution_status="active",
                        evidence_text=mechanism.evidence_text,
                    )
                    for mechanism in blocked_mechanisms
                ],
            ])
            session.commit()
            paper_id = paper.id

        @contextmanager
        def fake_session_scope(_database_url):
            session = factory()
            try:
                yield session
            finally:
                session.close()

        monkeypatch.setattr("app.mcp.server.get_settings", lambda: settings)
        monkeypatch.setattr("app.mcp.server.require_mcp_capability", lambda _capability: None)
        monkeypatch.setattr("app.mcp.server.session_scope", fake_session_scope)

        from app.mcp.server import get_review_coverage

        coverage = get_review_coverage(str(paper_id))

        assert coverage["mechanism_claims"]["total"] == 20
        assert coverage["mechanism_claims"]["ai_verified"] == 11
        assert coverage["mechanism_claims"]["human_verified"] == 0
        assert coverage["mechanism_claims"]["exception"] == 9
        assert coverage["mechanism_claims"]["decision_recorded"] == 20
        assert coverage["mechanism_claims"]["unreviewed"] == 0
        assert coverage["mechanism_claims"]["authoritative_reviewed"] == 11
        assert coverage["mechanism_claims"]["can_use_for_writing"] == 11
        assert coverage["mechanism_claims"]["can_use_for_citation"] == 11
        assert coverage["mechanism_claims"]["blocked"] == 9
        sections = coverage["sections"]
        assert sections["total"] == 6
        assert sections["ai_verified"] == 4
        assert sections["human_verified"] == 0
        assert sections["exception"] == 2
        assert sections["decision_recorded"] == 6
        assert sections["reviewed"] == 6
        assert sections["unreviewed"] == 0
        assert sections["authoritative_reviewed"] == 4
        assert sections["blocked"] == 2
        assert sections["can_use_for_writing"] == 4
        assert sections["can_use_for_citation"] == 4
        assert sections["by_section_type"]["figure_caption"]["verified"] == 4
        assert sections["by_section_type"]["figure_caption"]["exception"] == 0
        assert sections["by_section_type"]["body"]["verified"] == 0
        assert sections["by_section_type"]["body"]["exception"] == 2
        assert coverage["writing_cards"]["total"] == 1
        assert coverage["writing_cards"]["ai_verified"] == 0
        assert coverage["writing_cards"]["human_verified"] == 0
        assert coverage["writing_cards"]["exception"] == 1
        assert coverage["writing_cards"]["decision_recorded"] == 1
        assert coverage["writing_cards"]["reviewed"] == 1
        assert coverage["writing_cards"]["unreviewed"] == 0
        assert coverage["writing_cards"]["authoritative_reviewed"] == 0
        assert coverage["writing_cards"]["blocked"] == 1
        assert coverage["writing_cards"]["can_use_for_writing"] == 0
        assert coverage["writing_cards"]["can_use_for_citation"] == 0
        with factory() as api_session:
            api_summary = ContentKnowledgeReviewService(api_session).paper_summary(str(paper_id))
            for key in (
                "total",
                "ai_verified",
                "human_verified",
                "exception",
                "decision_recorded",
                "unreviewed",
                "authoritative_reviewed",
                "blocked",
                "can_use_for_writing",
                "can_use_for_citation",
            ):
                assert api_summary["review_coverage"]["sections"][key] == sections[key]
                assert (
                    api_summary["review_coverage"]["writing_cards"][key]
                    == coverage["writing_cards"][key]
                )
        for key in ("sections", "mechanism_claims", "writing_cards", "figures", "tables"):
            assert {
                "total",
                "authoritative_reviewed",
                "can_use_for_writing",
                "can_use_for_citation",
                "blocked",
                "blocked_reasons",
            } <= set(coverage[key])
    finally:
        engine.dispose()
