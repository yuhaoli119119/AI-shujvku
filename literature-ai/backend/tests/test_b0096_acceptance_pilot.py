from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import (
    ContentEvidenceItem,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    WritingCard,
)
from app.main import app
from app.rag.multi_paper_evidence_plan import MultiPaperEvidencePlanner
from app.services.content_knowledge_review_service import ContentKnowledgeReviewService
from app.services.content_knowledge_service import ContentKnowledgeService
from app.utils.review_safety import content_object_gate


class _PilotRetriever:
    def __init__(self, payload: dict):
        self.payload = payload

    def retrieve(self, **_kwargs):
        return self.payload


def _review(
    paper_id,
    target_type: str,
    target_id: str,
    field_name: str,
    value,
    evidence_text: str,
    *,
    status: str = "verified",
) -> ExtractionFieldReview:
    return ExtractionFieldReview(
        paper_id=paper_id,
        target_type=target_type,
        target_id=target_id,
        field_name=field_name,
        original_value=value,
        reviewed_value=value,
        evidence_text=evidence_text,
        reviewer_status=status,
        target_resolution_status="active",
        review_payload=(
            {
                "human_verification": {
                    "decision": "verified",
                    "verification_actor_type": "human",
                    "identity_verified": True,
                    "writes_final_truth": True,
                }
            }
            if status == "verified"
            else None
        ),
    )


def _locator(paper_id, target_type: str, target_id: str, field_name: str, text: str, page: int):
    return EvidenceLocator(
        paper_id=paper_id,
        source_type="pdf",
        target_type=target_type,
        target_id=target_id,
        field_name=field_name,
        evidence_text=text,
        page=page,
        locator_status="exact_page",
        locator_confidence=1.0,
        parser_source="b0096_acceptance_pilot",
    )


def test_b0096_isolated_end_to_end_acceptance_pilot(setup_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", "b0096-owner-secret")
    monkeypatch.setenv("LITAI_EMBEDDING_PROVIDER", "deterministic")
    get_settings.cache_clear()
    pdf_path = tmp_path / "B0096.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\nB0096 isolated acceptance evidence\n%%EOF\n")
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)

    with factory.begin() as session:
        paper = Paper(
            paper_code="B0096",
            title="Isolated acceptance pilot",
            pdf_path=str(pdf_path),
            authors=[],
        )
        session.add(paper)
        session.flush()
        mechanism = MechanismClaim(
            paper_id=paper.id,
            claim_type="conversion",
            claim_text="Fe-N4 sites accelerate polysulfide conversion.",
            evidence_text="The PDF states that Fe-N4 sites accelerate polysulfide conversion.",
        )
        blocked_mechanism = MechanismClaim(
            paper_id=paper.id,
            claim_type="conversion",
            claim_text="An unreviewed mechanism candidate must stay blocked.",
            evidence_text="Candidate-only evidence.",
        )
        card = WritingCard(
            paper_id=paper.id,
            research_gap="A documented conversion limitation remains unresolved in current hosts.",
            proposed_solution="This work develops a documented catalyst solution for conversion.",
            evidence_chain=[
                {
                    "text": "A documented conversion limitation remains unresolved in current hosts.",
                    "source": "Introduction",
                    "page": 1,
                    "locator_status": "exact_page",
                    "supports_fields": ["research_gap"],
                },
                {
                    "text": "This work develops a documented catalyst solution for conversion.",
                    "source": "Introduction",
                    "page": 1,
                    "locator_status": "exact_page",
                    "supports_fields": ["proposed_solution"],
                },
            ],
        )
        session.add_all([mechanism, blocked_mechanism, card])
        session.flush()
        mechanism_review = _review(
            paper.id,
            "mechanism_claims",
            str(mechanism.id),
            "claim_text",
            mechanism.claim_text,
            mechanism.evidence_text,
            status="pending",
        )
        session.add_all(
            [
                mechanism_review,
                _locator(
                    paper.id,
                    "mechanism_claims",
                    str(mechanism.id),
                    "claim_text",
                    mechanism.evidence_text,
                    2,
                ),
                _review(
                    paper.id,
                    "writing_cards",
                    str(card.id),
                    "research_gap",
                    card.research_gap,
                    card.evidence_chain[0]["text"],
                ),
                _locator(
                    paper.id,
                    "writing_cards",
                    str(card.id),
                    "research_gap",
                    card.evidence_chain[0]["text"],
                    1,
                ),
            ]
        )
        session.flush()
        paper_id = paper.id
        mechanism_id = mechanism.id
        blocked_mechanism_id = blocked_mechanism.id
        card_id = card.id
        review_id = mechanism_review.id

    anonymous = TestClient(app)
    rejected = anonymous.post(
        f"/api/reviews/{review_id}/promote",
        json={
            "target_status": "verified",
            "reviewed_value": "Fe-N4 sites accelerate polysulfide conversion.",
            "confirm_human_review": True,
        },
    )
    assert rejected.status_code == 401
    with factory() as session:
        unchanged = session.get(ExtractionFieldReview, review_id)
        assert unchanged.reviewer_status == "pending"
        assert content_object_gate(session, "mechanism_claims", session.get(MechanismClaim, mechanism_id)).can_use_for_writing is False

    browser = TestClient(app)
    unlocked = browser.post(
        "/api/settings/owner-session",
        json={"token": "b0096-owner-secret"},
    )
    assert unlocked.status_code == 200
    promoted = browser.post(
        f"/api/reviews/{review_id}/promote",
        json={
            "target_status": "verified",
            "reviewed_value": "Fe-N4 sites accelerate polysulfide conversion.",
            "confirm_human_review": True,
        },
    )
    assert promoted.status_code == 200

    with factory.begin() as session:
        safe_mechanism = session.get(MechanismClaim, mechanism_id)
        blocked_mechanism = session.get(MechanismClaim, blocked_mechanism_id)
        safe_card = session.get(WritingCard, card_id)
        mechanism_gate = content_object_gate(session, "mechanism_claims", safe_mechanism)
        blocked_gate = content_object_gate(session, "mechanism_claims", blocked_mechanism)
        card_gate = content_object_gate(session, "writing_cards", safe_card)
        assert mechanism_gate.can_use_for_writing is True
        assert mechanism_gate.can_use_for_citation is True
        assert blocked_gate.can_use_for_writing is False
        assert card_gate.can_use_for_writing is False
        assert any("blocked_or_missing_authoritative_source" in reason for reason in card_gate.blocked_reasons)

        ContentKnowledgeService(session).sync_items(paper_id=paper_id)
        summary = ContentKnowledgeReviewService(session).paper_summary(str(paper_id))
        projections = list(
            session.scalars(select(ContentEvidenceItem).where(ContentEvidenceItem.paper_id == paper_id)).all()
        )
        assert len(projections) == 3

    @contextmanager
    def pilot_session_scope(_database_url):
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("app.mcp.server.require_mcp_capability", lambda _capability: None)
    monkeypatch.setattr("app.mcp.server.session_scope", pilot_session_scope)
    monkeypatch.setattr(
        "app.mcp.server.get_settings",
        lambda: get_settings().model_copy(update={"database_url": str(get_settings().database_url)}),
    )
    from app.mcp.server import get_review_coverage

    coverage = get_review_coverage(str(paper_id))
    mcp_safe_total = (
        coverage["mechanism_claims"]["can_use_for_writing"]
        + coverage["writing_cards"]["can_use_for_writing"]
    )
    assert summary["can_use_for_writing_total"] == mcp_safe_total == 1
    assert summary["blocked_total"] == 2

    safe_mechanism_item = {
        "paper_id": str(paper_id),
        "object_id": str(mechanism_id),
        "evidence_text": "The PDF states that Fe-N4 sites accelerate polysulfide conversion.",
        "evidence_locator": {"page": 2, "locator_status": "exact_page"},
        "review_status": "safe_verified",
        "review_gate_status": "safe_verified",
        "can_use_for_writing": True,
        "can_use_for_citation": True,
        "score": 1.0,
    }
    blocked_item = {
        "paper_id": str(paper_id),
        "object_id": str(blocked_mechanism_id),
        "evidence_text": "Candidate-only evidence.",
        "evidence_locator": {"page": 3, "locator_status": "exact_page"},
        "review_status": "blocked",
        "review_gate_status": "blocked",
        "can_use_for_writing": False,
        "can_use_for_citation": False,
        "score": 1.0,
    }
    safe_card_item = {
        "paper_id": str(paper_id),
        "object_id": str(card_id),
        "evidence_text": "A documented conversion limitation remains unresolved in current hosts.",
        "evidence_locator": {"page": 1, "locator_status": "exact_page"},
        "review_status": "blocked",
        "review_gate_status": "blocked",
        "can_use_for_writing": False,
        "can_use_for_citation": False,
        "score": 0.9,
    }
    with factory() as session:
        plan = MultiPaperEvidencePlanner(
            session,
            retriever=_PilotRetriever(
                {
                    "mechanism_claims": [safe_mechanism_item, blocked_item],
                    "writing_cards": [safe_card_item],
                }
            ),
        ).plan(
            query="conversion mechanism writing synthesis",
            paper_ids=[paper_id],
            evidence_types=["mechanism_claims", "writing_cards"],
            evidence_budget=4,
        )
    selected_ids = {item["object_id"] for item in plan["selected_evidence"]}
    assert selected_ids == {str(mechanism_id)}
    assert str(card_id) not in selected_ids
    assert str(blocked_mechanism_id) not in selected_ids
    assert plan["database_writes"] is False
