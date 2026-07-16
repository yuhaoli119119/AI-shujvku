from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Base,
    ContentEvidenceItem,
    ContentReviewBundle,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperCorrection,
    PaperSection,
    WritingCard,
)
from app.main import app
from app.services.content_knowledge_service import ContentKnowledgeService
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.content_review_bundle_service import (
    ContentReviewBundleService,
    ContentReviewBundleV1DeprecatedError,
)
from app.services.review_service import ReviewService
from app.utils.review_safety import content_object_gate


def _session():
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _paper(session: Session, *, abstract: str = "Original abstract") -> Paper:
    paper = Paper(title="P0 content safety", abstract=abstract, pdf_path="paper.pdf", authors=[])
    session.add(paper)
    session.flush()
    return paper


def _safe_mechanism(session: Session, paper: Paper) -> MechanismClaim:
    claim = MechanismClaim(
        paper_id=paper.id,
        claim_type="conversion",
        claim_text="Canonical mechanism claim",
        evidence_text="The PDF reports the canonical mechanism claim.",
    )
    session.add(claim)
    session.flush()
    session.add_all([
        ExtractionFieldReview(
            paper_id=paper.id,
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            reviewer_status="verified",
            target_resolution_status="active",
            evidence_text=claim.evidence_text,
        ),
        EvidenceLocator(
            paper_id=paper.id,
            source_type="pdf",
            target_type="mechanism_claims",
            target_id=str(claim.id),
            field_name="claim_text",
            page=3,
            evidence_text=claim.evidence_text,
            locator_status="exact_page",
            locator_confidence=1.0,
            parser_source="test",
        ),
    ])
    session.flush()
    return claim


def _mark_abstract_safe(session: Session, paper: Paper) -> None:
    session.add_all([
        ExtractionFieldReview(
            paper_id=paper.id,
            target_type="abstract",
            target_id=str(paper.id),
            field_name="abstract",
            reviewer_status="verified",
            target_resolution_status="active",
            evidence_text=paper.abstract,
        ),
        EvidenceLocator(
            paper_id=paper.id,
            source_type="pdf",
            target_type="abstract",
            target_id=str(paper.id),
            field_name="abstract",
            page=1,
            evidence_text=paper.abstract,
            locator_status="exact_page",
            locator_confidence=1.0,
            parser_source="test",
        ),
    ])
    session.flush()


def test_v1_bundle_mutators_are_410_and_cannot_change_projection_state(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session)
        item = ContentEvidenceItem(
            paper_id=paper.id,
            category="mechanism_evidence",
            source_type="paper_note",
            source_id="legacy-note",
            content="Legacy projection",
            review_status="needs_review",
            citation_status="needs_review",
        )
        bundle = ContentReviewBundle(
            paper_id=paper.id,
            snapshot_fingerprint="legacy",
            manifest={"schema_version": "content_evidence_review_bundle_v1"},
            result_payload={"items": [{"item_id": str(item.id), "decision": "approve_citable"}]},
            status="validated",
            created_by="legacy",
        )
        session.add_all([item, bundle])
        session.commit()
        paper_id, item_id, bundle_id = paper.id, item.id, bundle.id

        with pytest.raises(ContentReviewBundleV1DeprecatedError):
            ContentReviewBundleService(session).apply_result(bundle_id, reviewer="human")
        session.rollback()

    client = TestClient(app)
    calls = [
        ("/api/content-knowledge/review-bundles", {"paper_id": str(paper_id)}),
        (f"/api/content-knowledge/review-bundles/{bundle_id}/validate", {"items": []}),
        (f"/api/content-knowledge/review-bundles/{bundle_id}/apply", {"reviewer": "human"}),
        (f"/api/content-knowledge/review-bundles/{bundle_id}/finalize", {"reviewer": "human"}),
    ]
    for path, payload in calls:
        response = client.post(path, json=payload)
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "content_review_bundle_v1_deprecated"

    readonly = client.get(f"/api/content-knowledge/review-bundles/{bundle_id}")
    assert readonly.status_code == 200
    assert readonly.json()["deprecated"] is True
    with Session(setup_test_db) as session:
        item = session.get(ContentEvidenceItem, item_id)
        assert item.review_status == "needs_review"
        assert item.citation_status == "needs_review"
        assert session.query(ContentReviewBundle).count() == 1


def test_forged_citable_projection_cannot_bypass_canonical_gate(setup_test_db, monkeypatch):
    monkeypatch.setattr(
        "app.services.content_knowledge_service.get_embedding_service",
        lambda **_: type("NoEmbedding", (), {"embed_text": lambda self, text: []})(),
    )
    with Session(setup_test_db) as session:
        paper = _paper(session)
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="unsafe",
            claim_text="Forged citable projection",
            evidence_text="Projection-only evidence.",
        )
        session.add(claim)
        session.flush()
        projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="mechanism_evidence",
            source_type="mechanism_claim",
            source_id=str(claim.id),
            content=claim.claim_text,
            evidence_text=claim.evidence_text,
            evidence_locator={"page": 9, "locator_status": "exact_page"},
            page_start=9,
            review_status="safe_verified",
            citation_status="citable",
        )
        session.add(projection)
        session.commit()

        gate = content_object_gate(session, projection.source_type, projection)
        assert gate.can_use_for_writing is False
        assert gate.can_use_for_citation is False
        assert "missing_review" in gate.blocked_reasons
        assert "content_projection_gate_mismatch" in gate.blocked_reasons
        assert ContentKnowledgeService(session).search_for_rag(
            query="Forged citable projection",
            paper_ids=[paper.id],
        ) == []


def test_canonical_safe_gate_and_formal_retrieval_ignore_missing_projection_locator(setup_test_db, monkeypatch):
    monkeypatch.setattr(
        "app.services.content_knowledge_service.get_embedding_service",
        lambda **_: type("NoEmbedding", (), {"embed_text": lambda self, text: []})(),
    )
    with Session(setup_test_db) as session:
        paper = _paper(session)
        claim = _safe_mechanism(session, paper)
        projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="mechanism_evidence",
            source_type="mechanism_claim",
            source_id=str(claim.id),
            content=claim.claim_text,
            evidence_text=claim.evidence_text,
            evidence_locator=None,
            page_start=None,
            review_status="needs_review",
            citation_status="needs_review",
        )
        session.add(projection)
        session.commit()

        canonical_gate = content_object_gate(session, "mechanism_claims", claim)
        projection_gate = content_object_gate(session, projection.source_type, projection)
        for gate in (canonical_gate, projection_gate):
            assert gate.can_use_for_writing is True
            assert gate.can_use_for_citation is True
            assert gate.review_gate_status == "safe_verified"
            assert gate.locator_status == "exact_page"
            assert gate.policy_version == "content_object_gate.v1"

        rows = ContentKnowledgeService(session).search_for_rag(
            query="Canonical mechanism claim",
            paper_ids=[paper.id],
        )
        assert len(rows) == 1
        item, _score = rows[0]
        assert item.can_use_for_writing is True
        assert item.can_use_for_citation is True
        assert item.review_gate_status == "safe_verified"
        assert "content_projection_cache_stale" in item.risk_flags


@pytest.mark.parametrize("forged_field", ["content", "evidence_text"])
def test_canonical_safe_gate_rejects_forged_projection_snapshot(
    setup_test_db,
    monkeypatch,
    forged_field,
):
    monkeypatch.setattr(
        "app.services.content_knowledge_service.get_embedding_service",
        lambda **_: type("NoEmbedding", (), {"embed_text": lambda self, text: []})(),
    )
    with Session(setup_test_db) as session:
        paper = _paper(session)
        claim = _safe_mechanism(session, paper)
        values = {
            "content": claim.claim_text,
            "evidence_text": claim.evidence_text,
        }
        values[forged_field] = "Forged projection payload"
        projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="mechanism_evidence",
            source_type="mechanism_claim",
            source_id=str(claim.id),
            content=values["content"],
            evidence_text=values["evidence_text"],
            evidence_locator={"page": 3, "locator_status": "exact_page"},
            page_start=3,
            review_status="safe_verified",
            citation_status="citable",
        )
        session.add(projection)
        session.commit()

        gate = content_object_gate(session, projection.source_type, projection)
        assert gate.can_use_for_writing is False
        assert gate.can_use_for_citation is False
        assert gate.blocked_reasons == ("content_projection_snapshot_mismatch",)
        assert ContentKnowledgeService(session).search_for_rag(
            query="Forged projection payload",
            paper_ids=[paper.id],
        ) == []


def test_abstract_language_key_projection_uses_own_paper_canonical_gate(setup_test_db, monkeypatch):
    monkeypatch.setattr(
        "app.services.content_knowledge_service.get_embedding_service",
        lambda **_: type("NoEmbedding", (), {"embed_text": lambda self, text: []})(),
    )
    with Session(setup_test_db) as session:
        paper = _paper(session, abstract="Canonical English abstract")
        _mark_abstract_safe(session, paper)
        projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="abstract",
            source_type="abstract",
            source_id="en",
            content=paper.abstract,
            evidence_text=paper.abstract,
            evidence_locator=None,
            page_start=None,
            review_status="needs_review",
            citation_status="needs_review",
        )
        session.add(projection)
        session.commit()

        gate = content_object_gate(session, projection.source_type, projection)
        assert gate.can_use_for_writing is True
        assert gate.can_use_for_citation is True
        assert gate.review_gate_status == "safe_verified"
        assert gate.locator_status == "exact_page"

        rows = ContentKnowledgeService(session).search_for_rag(
            query="Canonical English abstract",
            paper_ids=[paper.id],
        )
        assert len(rows) == 1
        assert rows[0][0].can_use_for_citation is True


def test_abstract_language_key_projection_cannot_bypass_own_paper_gate(setup_test_db):
    with Session(setup_test_db) as session:
        unsafe_paper = _paper(session, abstract="Unsafe abstract")
        other_paper = _paper(session, abstract="Safe abstract on another paper")
        _mark_abstract_safe(session, other_paper)
        projection = ContentEvidenceItem(
            paper_id=unsafe_paper.id,
            category="abstract",
            source_type="abstract",
            source_id="en",
            content=unsafe_paper.abstract,
            evidence_text=unsafe_paper.abstract,
            evidence_locator={"page": 1, "locator_status": "exact_page"},
            page_start=1,
            review_status="safe_verified",
            citation_status="citable",
        )
        session.add(projection)
        session.commit()

        gate = content_object_gate(session, projection.source_type, projection)
        assert gate.can_use_for_writing is False
        assert gate.can_use_for_citation is False
        assert "missing_review" in gate.blocked_reasons
        assert "content_projection_gate_mismatch" in gate.blocked_reasons


@pytest.mark.parametrize(
    "source_type",
    ["paper_note", "evidence_claim", "external_analysis_candidate"],
)
def test_unmapped_projection_types_remain_blocked(setup_test_db, source_type):
    with Session(setup_test_db) as session:
        paper = _paper(session)
        projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="uncertainty_note",
            source_type=source_type,
            source_id=str(uuid4()),
            content="Unmapped content",
            evidence_text="Projection evidence",
            evidence_locator={"page": 1},
            page_start=1,
            review_status="safe_verified",
            citation_status="citable",
        )
        session.add(projection)
        session.commit()

        gate = content_object_gate(session, source_type, projection)
        assert gate.can_use_for_writing is False
        assert gate.can_use_for_citation is False
        assert "no_real_object_mapping" in gate.blocked_reasons


def test_local_ai_requires_module_locks_for_all_content_direct_writes(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session)
        section = PaperSection(paper_id=paper.id, section_title="Old section", text="Old section text")
        mechanism = MechanismClaim(paper_id=paper.id, claim_text="Old mechanism")
        card = WritingCard(paper_id=paper.id, research_gap="Old gap")
        session.add_all([section, mechanism, card])
        session.flush()
        corrections = [
            (PaperCorrection(
                paper_id=paper.id, source="local_ai", field_name="abstract", target_path="abstract",
                operation="replace", proposed_value="New abstract", reason="AI rewrite", status="pending",
            ), "metadata"),
            (PaperCorrection(
                paper_id=paper.id, source="local_ai", field_name="sections",
                target_path=f"sections:{section.id}:text", operation="replace",
                proposed_value="New section text", reason="AI rewrite", status="pending",
            ), "sections"),
            (PaperCorrection(
                paper_id=paper.id, source="local_ai", field_name="mechanism_claims",
                target_path=f"mechanism_claims:{mechanism.id}:claim_text", operation="replace",
                proposed_value="New mechanism", reason="AI rewrite", status="pending",
            ), "mechanism_claims"),
            (PaperCorrection(
                paper_id=paper.id, source="local_ai", field_name="writing_cards",
                target_path=f"writing_cards:{card.id}:research_gap", operation="replace",
                proposed_value="New gap", reason="AI rewrite", status="pending",
            ), "writing_cards"),
        ]
        session.add_all([correction for correction, _module in corrections])
        session.commit()

        service = ReviewService(session)
        for correction, module in corrections:
            with pytest.raises(ValueError, match=f"module_write_lock_required:{module}"):
                service.approve_correction(correction.id, reviewer="local_ai")
            session.rollback()

        assert session.get(Paper, paper.id).abstract == "Original abstract"
        assert session.get(PaperSection, section.id).text == "Old section text"
        assert session.get(MechanismClaim, mechanism.id).claim_text == "Old mechanism"
        assert session.get(WritingCard, card.id).research_gap == "Old gap"

        human = PaperCorrection(
            paper_id=paper.id, source="manual", field_name="abstract", target_path="abstract",
            operation="replace", proposed_value="Human abstract", reason="Curated", status="pending",
        )
        session.add(human)
        session.flush()
        service.approve_correction(human.id, reviewer="curator")
        assert session.get(Paper, paper.id).abstract == "Human abstract"


def test_unknown_authenticated_agent_defaults_to_module_lock_required(setup_test_db):
    with Session(setup_test_db) as session:
        paper = _paper(session)
        correction = PaperCorrection(
            paper_id=paper.id,
            source="custom_agent",
            field_name="abstract",
            target_path="abstract",
            operation="replace",
            proposed_value="Custom agent abstract",
            reason="Authenticated custom writer",
            status="pending",
        )
        session.add(correction)
        session.commit()

        with pytest.raises(ValueError, match="module_write_lock_required:metadata"):
            ReviewService(session).approve_correction(
                correction.id,
                reviewer="unknown_authenticated_agent",
            )
        session.rollback()
        assert session.get(Paper, paper.id).abstract == "Original abstract"

        lock = ModuleWriteLockService(session).acquire(
            paper_id=paper.id,
            module_name="metadata",
            locked_by="unknown_authenticated_agent",
        )
        ReviewService(session).approve_correction(
            correction.id,
            reviewer="unknown_authenticated_agent",
            write_lock_tokens=[lock.lock_token],
        )
        assert session.get(Paper, paper.id).abstract == "Custom agent abstract"
