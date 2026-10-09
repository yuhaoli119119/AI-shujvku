from __future__ import annotations

from pathlib import Path

import fitz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AuditLog,
    ContentEvidenceItem,
    EvidenceLocator,
    ExtractionFieldReview,
    Paper,
    PaperSection,
    WritingCard,
)
from app.schemas.ai_verification import AIVerificationSubmission
from app.services.ai_verification_service import (
    AIVerificationService,
    AuthenticatedAIVerificationIdentity,
)
from app.utils.ai_verification import AI_VERIFICATION_CAPABILITY, ai_target_fingerprint
from app.utils.review_safety import content_object_gate, writing_card_authoritative_chain_gate


def _pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(60, 60, 540, 780), text, fontsize=10)
    document.save(path)
    document.close()


def _identity() -> AuthenticatedAIVerificationIdentity:
    return AuthenticatedAIVerificationIdentity(
        source_identity="mcp:single-verifier",
        source_label="single-verifier",
        model_agent="codex-single-ai",
        capabilities=frozenset({AI_VERIFICATION_CAPABILITY}),
        identity_verified=True,
    )


def _submission(target_type: str, target, field_name: str, evidence: str, page: int, **overrides):
    payload = {
        "target_type": target_type,
        "target_id": str(target.id),
        "field_name": field_name,
        "decision": "accept",
        "confidence": 0.98,
        "evidence_text": evidence,
        "page": page,
        "reasoning_summary": "The quoted text is exact on the physical PDF page.",
        "expected_target_fingerprint": ai_target_fingerprint(target_type, target),
    }
    payload.update(overrides)
    return AIVerificationSubmission.model_validate(payload)


def _counts(session: Session) -> dict[str, int]:
    return {
        "locators": session.scalar(select(func.count(EvidenceLocator.id))),
        "reviews": session.scalar(select(func.count(ExtractionFieldReview.id))),
        "audits": session.scalar(select(func.count(AuditLog.id))),
    }


def test_section_text_recovers_exact_page_but_title_and_approximate_support_do_not_authorize(
    setup_test_db,
    tmp_path,
):
    exact = "Figure 1. Categories of adsorption configurations."
    partial = "The Fe-N4 catalyst is discussed without a reported barrier."
    pdf_path = tmp_path / "sections.pdf"
    _pdf(pdf_path, [f"{exact}\n{partial}"])
    with Session(setup_test_db) as session:
        paper = Paper(title="Section verification", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        section = PaperSection(
            paper_id=paper.id,
            section_title="Figure 1",
            section_type="figure_caption",
            text=exact.replace("configurations", "con /uniFB01 gurations"),
            page_start=None,
            page_end=None,
        )
        approximate = PaperSection(
            paper_id=paper.id,
            section_title="Results",
            section_type="body",
            text="The Fe-N4 catalyst lowers the Li2S barrier to 0.42 eV.",
        )
        session.add_all([section, approximate])
        session.flush()

        tasks = AIVerificationService(session).list_tasks(
            paper_id=paper.id,
            target_type="sections",
            recover_evidence=True,
        )
        recovered = next(item for item in tasks["tasks"] if item["target_id"] == str(section.id))
        assert recovered["field_name"] == "text"
        assert recovered["evidence_recovery"]["status"] == "recovered"
        assert recovered["evidence_candidates"][0]["page"] == 1
        assert recovered["evidence_candidates"][0]["locator_status"] == "exact_page"
        assert tasks["database_writes"] is False
        assert tasks["embedding_requests"] == 0
        assert tasks["embedding_role"] == "retrieval_only"

        session.add_all(
            [
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="sections",
                    target_id=str(section.id),
                    field_name="section_title",
                    reviewed_value=section.section_title,
                    reviewer_status="verified",
                    target_resolution_status="active",
                    evidence_text="Figure 1",
                ),
                EvidenceLocator(
                    paper_id=paper.id,
                    source_type="pdf",
                    target_type="sections",
                    target_id=str(section.id),
                    field_name="section_title",
                    evidence_text="Figure 1",
                    page=1,
                    locator_status="exact_page",
                    locator_confidence=1.0,
                    parser_source="test",
                ),
            ]
        )
        session.flush()
        title_gate = content_object_gate(session, "sections", section)
        assert title_gate.can_use_for_writing is False
        assert "missing_required_review:text" in title_gate.blocked_reasons

        exact_dry = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[_submission("sections", section, "text", exact, 1)],
            identity=_identity(),
            dry_run=True,
        )
        assert exact_dry["auto_repaired"] == 1
        assert exact_dry["items"][0]["evidence_checks"]["numeric_value_matches"] is True

        dry = AIVerificationService(session).process_batch(
            paper_id=paper.id,
            submissions=[_submission("sections", approximate, "text", partial, 1)],
            identity=_identity(),
            dry_run=True,
        )
        assert dry["auto_rejected"] == 1
        assert "numeric_value_matches" in dry["items"][0]["blocked_reasons"]
        assert dry["database_writes"] is False


def test_writing_card_requires_authoritative_sources_and_is_writing_only(
    setup_test_db,
    tmp_path,
):
    gap = "A documented polysulfide conversion limitation remains unresolved."
    solution = "This work develops Fe-N4 sites to accelerate polysulfide conversion."
    blocked_text = "An unverified cobalt mechanism is proposed."
    pdf_path = tmp_path / "writing-card.pdf"
    _pdf(pdf_path, [f"{gap}\n{solution}\n{blocked_text}"])
    with Session(setup_test_db) as session:
        paper = Paper(title="WritingCard verification", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        safe_gap = PaperSection(paper_id=paper.id, section_title="Introduction", section_type="body", text=gap)
        safe_solution = PaperSection(paper_id=paper.id, section_title="Introduction", section_type="body", text=solution)
        blocked = PaperSection(paper_id=paper.id, section_title="Discussion", section_type="body", text=blocked_text)
        session.add_all([safe_gap, safe_solution, blocked])
        session.flush()

        service = AIVerificationService(session)
        verified_sources = service.process_batch(
            paper_id=paper.id,
            submissions=[
                _submission("sections", safe_gap, "text", gap, 1),
                _submission("sections", safe_solution, "text", solution, 1),
            ],
            identity=_identity(),
            dry_run=False,
        )
        assert verified_sources["auto_repaired"] == 2
        section_projections = list(
            session.scalars(
                select(ContentEvidenceItem).where(
                    ContentEvidenceItem.paper_id == paper.id,
                    ContentEvidenceItem.source_type == "section",
                )
            ).all()
        )
        assert {item.source_id for item in section_projections} == {
            str(safe_gap.id),
            str(safe_solution.id),
        }

        chain = [
            {
                "text": gap,
                "source": "Introduction",
                "page": 1,
                "locator_status": "exact_page",
                "supports_fields": ["research_gap"],
                "source_target_type": "sections",
                "source_target_id": str(safe_gap.id),
            },
            {
                "text": solution,
                "source": "Introduction",
                "page": 1,
                "locator_status": "exact_page",
                "supports_fields": ["proposed_solution"],
                "source_target_type": "sections",
                "source_target_id": str(safe_solution.id),
            },
        ]
        card = WritingCard(
            paper_id=paper.id,
            paper_type="computational",
            research_gap=gap,
            proposed_solution=solution,
            evidence_chain=chain,
            embedding=[0.125] * 1024,
        )
        blocked_card = WritingCard(
            paper_id=paper.id,
            research_gap=gap,
            proposed_solution=blocked_text,
            evidence_chain=[
                chain[0],
                {
                    "text": blocked_text,
                    "source": "Discussion",
                    "page": 1,
                    "locator_status": "exact_page",
                    "supports_fields": ["proposed_solution"],
                    "source_target_type": "sections",
                    "source_target_id": str(blocked.id),
                },
            ],
        )
        session.add_all([card, blocked_card])
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                source_type="pdf",
                target_type="sections",
                target_id=str(blocked.id),
                field_name="text",
                evidence_text=blocked_text,
                page=1,
                locator_status="exact_page",
                locator_confidence=1.0,
                parser_source="test",
            )
        )
        session.flush()

        blocked_gate = writing_card_authoritative_chain_gate(session, blocked_card)
        assert blocked_gate.can_use_for_writing is False
        assert any("blocked_or_missing_authoritative_source" in reason for reason in blocked_gate.blocked_reasons)

        task_page = service.list_tasks(
            paper_id=paper.id,
            target_type="writing_cards",
            recover_evidence=True,
        )
        task = next(item for item in task_page["tasks"] if item["target_id"] == str(card.id))
        assert task["evidence_candidates"][0]["locator_status"] == "exact_page"
        submission = _submission("writing_cards", card, "evidence_chain", gap, 1)
        before = _counts(session)
        original_embedding = list(card.embedding)

        dry = service.process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=True,
        )
        assert dry["auto_repaired"] == 1
        assert dry["database_writes"] is False
        assert dry["single_ai"] is True
        assert dry["second_ai_used"] is False
        assert dry["embedding_requests"] == 0
        assert dry["embedding_role"] == "retrieval_only"
        assert _counts(session) == before

        formal = service.process_batch(
            paper_id=paper.id,
            submissions=[submission],
            identity=_identity(),
            dry_run=False,
        )
        assert formal["auto_repaired"] == 1
        assert formal["database_writes"] is True
        assert formal["single_ai"] is True
        assert formal["second_ai_used"] is False
        assert card.embedding == original_embedding
        assert session.scalar(
            select(func.count(ContentEvidenceItem.id)).where(
                ContentEvidenceItem.paper_id == paper.id,
                ContentEvidenceItem.source_type == "writing_card",
                ContentEvidenceItem.source_id == str(card.id),
            )
        ) == 1
        after = _counts(session)
        assert after == {key: value + 1 for key, value in before.items()}

        gate = content_object_gate(session, "writing_cards", card)
        assert gate.can_use_for_writing is True
        assert gate.can_use_for_citation is False
        assert content_object_gate(session, "writing_cards", blocked_card).can_use_for_writing is False


def test_formal_target_failure_rolls_back_locator_review_and_audit(setup_test_db, tmp_path, monkeypatch):
    text = "The exact section text appears on the physical page."
    pdf_path = tmp_path / "atomic-section.pdf"
    _pdf(pdf_path, [text])
    with Session(setup_test_db) as session:
        paper = Paper(title="Atomic Section", pdf_path=str(pdf_path), authors=["Tester"])
        session.add(paper)
        session.flush()
        section = PaperSection(paper_id=paper.id, section_title="Results", section_type="body", text=text)
        session.add(section)
        session.commit()
        service = AIVerificationService(session)

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("forced audit failure")

        monkeypatch.setattr(service, "_add_audit", fail_audit)
        result = service.process_batch(
            paper_id=paper.id,
            submissions=[_submission("sections", section, "text", text, 1)],
            identity=_identity(),
            dry_run=False,
        )
        session.expire_all()
        assert result["exception"] == 1
        assert result["database_writes"] is False
        assert _counts(session) == {"locators": 0, "reviews": 0, "audits": 0}
