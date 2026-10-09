from __future__ import annotations

import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, EvidenceLocator, EvidenceSpan, ExtractionFieldReview, Paper, PaperSection
from app.services.evidence_locator_service import EvidenceLocatorService
from scripts.recover_evidence_pages import analyze_evidence_pages, apply_recovery_decisions


def _session(tmp_path):
    db_url = os.environ["LITAI_TEST_DATABASE_URL"]
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, Session


def test_missing_page_locator_degrades_without_fake_jump_or_highlight(tmp_path):
    engine, Session = _session(tmp_path)
    with Session() as session:
        paper = Paper(title="Missing Page", pdf_path="paper.pdf")
        session.add(paper)
        session.flush()

        EvidenceLocatorService(session).create_locator_for_span(
            paper_id=paper.id,
            object_type="dft_result",
            object_id="result-1",
            evidence_text="Evidence text exists but no parser page was preserved.",
            page=None,
            section="Results",
            figure=None,
            table=None,
            confidence=0.5,
            bbox=None,
            parser_source="fallback",
            field_name="value",
        )
        session.commit()

        locator = EvidenceLocatorService(session).list_locators_for_paper(paper.id)[0]
        assert locator.locator_status == "missing_page"
        assert locator.provenance_level == "text_evidence_only"
        assert locator.can_jump_to_pdf_page is False
        assert locator.can_highlight_in_pdf is False
        assert locator.bbox is None
    engine.dispose()


def test_approximate_locator_is_not_serialized_as_exact_page(tmp_path):
    engine, Session = _session(tmp_path)
    with Session() as session:
        paper = Paper(title="Approximate Page", pdf_path="paper.pdf")
        session.add(paper)
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                evidence_text="This page candidate needs review.",
                page=3,
                locator_status="approximate_candidate",
                locator_confidence=0.55,
            )
        )
        session.commit()

        locator = EvidenceLocatorService(session).list_locators_for_paper(paper.id)[0]
        assert locator.locator_status == "approximate"
        assert locator.provenance_level == "approximate_pdf_page"
        assert locator.can_jump_to_pdf_page is False
        assert locator.can_highlight_in_pdf is False
    engine.dispose()


def test_recovery_unique_match_can_be_proposed_and_applied_without_review_mutation(tmp_path):
    engine, Session = _session(tmp_path)
    evidence_text = "The adsorption energy of Li2S4 on Fe-N4 is reported as -1.45 eV after geometry optimization."
    with Session() as session:
        paper = Paper(title="Recoverable Page", pdf_path="paper.pdf")
        session.add(paper)
        session.flush()
        span = EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id="result-1",
            text=evidence_text,
            page=None,
        )
        review = ExtractionFieldReview(
            paper_id=paper.id,
            target_type="dft_results",
            target_id="result-1",
            field_name="value",
            original_value=-1.45,
            reviewed_value=-1.45,
            evidence_text=evidence_text,
            reviewer_status="verified",
            target_resolution_status="active",
        )
        section = PaperSection(
            paper_id=paper.id,
            section_title="Results",
            text="Intro. " + evidence_text + " Additional discussion.",
            page_start=4,
            page_end=4,
        )
        session.add_all([span, review, section])
        session.commit()

        report = analyze_evidence_pages(session)
        decision = report["decisions"][0]
        assert decision["decision"] == "exact_recovered"
        assert decision["proposed_page"] == 4
        assert decision["apply_eligible"] is True

        applied = apply_recovery_decisions(session, report["decisions"])
        session.commit()
        assert applied == 1
        stored_span = session.scalars(select(EvidenceSpan).where(EvidenceSpan.id == span.id)).one()
        stored_review = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.id == review.id)).one()
        assert stored_span.page == 4
        assert stored_span.text == evidence_text
        assert stored_review.reviewer_status == "verified"
        assert stored_review.target_resolution_status == "active"
        assert stored_review.evidence_text == evidence_text
    engine.dispose()


def test_recovery_rejects_multi_page_match_and_short_text(tmp_path):
    engine, Session = _session(tmp_path)
    repeated = "The same evidence sentence appears in more than one parsed page."
    with Session() as session:
        paper = Paper(title="Ambiguous Page", pdf_path="paper.pdf")
        session.add(paper)
        session.flush()
        ambiguous = EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id="ambiguous",
            text=repeated,
            page=None,
        )
        short = EvidenceSpan(
            paper_id=paper.id,
            object_type="dft_results",
            object_id="short",
            text="short evidence",
            page=None,
        )
        session.add_all(
            [
                ambiguous,
                short,
                PaperSection(paper_id=paper.id, text=repeated, page_start=1, page_end=1),
                PaperSection(paper_id=paper.id, text=repeated, page_start=2, page_end=2),
            ]
        )
        session.commit()

        report = analyze_evidence_pages(session)
        by_id = {item["evidence_id"]: item for item in report["decisions"]}
        assert by_id[str(ambiguous.id)]["decision"] == "ambiguous_match"
        assert by_id[str(ambiguous.id)]["locator_status"] == "approximate"
        assert by_id[str(ambiguous.id)]["apply_eligible"] is False
        assert by_id[str(short.id)]["decision"] == "text_too_short"
        assert by_id[str(short.id)]["apply_eligible"] is False
    engine.dispose()
