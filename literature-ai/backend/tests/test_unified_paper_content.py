from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db.models import (
    AuditLog,
    ContentEvidenceItem,
    EvidenceLocator,
    MechanismClaim,
    Paper,
    PaperFigure,
    WritingCard,
)
from app.services.content_figure_link_service import ContentFigureLinkService
from app.services.content_knowledge_service import ContentKnowledgeService
from app.services.content_writing_plan_service import ContentWritingPlanService
from app.services.evidence_locator_service import EvidenceLocatorService
from app.services.extraction_pipeline import ExtractionPipelineService
from app.services.paper_knowledge_service import PaperKnowledgeService
from app.utils.review_safety import ContentObjectGateResult, content_object_gate


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _figure(paper_id, *, number: int) -> PaperFigure:
    return PaperFigure(
        paper_id=paper_id,
        figure_label=f"Figure {number}",
        caption=f"Figure {number}. Sulfur conversion evidence for the catalyst.",
        image_path=f"figures/figure-{number}.png",
        page=number + 1,
        figure_role="mechanism_diagram",
        role_confidence=0.96,
        content_summary=f"Panel {number} maps adsorption and conversion intermediates on the active sites.",
        key_elements=["active sites", "conversion intermediates", "reaction arrows"],
        crop_status="recropped",
        crop_source="test",
    )


def test_content_links_only_explicitly_reviewed_figures_and_tracks_current_snapshot(setup_test_db):
    with _factory(setup_test_db).begin() as session:
        paper = Paper(paper_code="PC101", title="Unified paper content", pdf_path="pc101.pdf", authors=[])
        session.add(paper)
        session.flush()
        reviewed = _figure(paper.id, number=2)
        unreviewed = _figure(paper.id, number=3)
        session.add_all([reviewed, unreviewed])
        session.flush()
        session.add(
            AuditLog(
                paper_id=paper.id,
                action="review_figure",
                source="figure_reviewer",
                target_type="paper_figure",
                target_id=str(reviewed.id),
                payload={"verdict": "verified"},
            )
        )
        card = WritingCard(
            paper_id=paper.id,
            research_gap="The conversion pathway remains unresolved.",
            evidence_chain=[
                {
                    "text": "As shown in Fig. 2a, the catalyst accelerates sulfur conversion.",
                    "source": "Results",
                    "page": 3,
                    "locator_status": "exact_page",
                    "evidence_type": "result",
                },
                {
                    "text": "Figure 3 provides an unreviewed comparison.",
                    "source": "Results",
                    "page": 4,
                    "locator_status": "exact_page",
                    "evidence_type": "result",
                },
            ],
            figure_logic=json.dumps(
                [
                    {"fig_id": "Fig. 2", "purpose": "support conversion", "supports_claim": "kinetics"},
                    {"fig_id": "Fig. 3", "purpose": "comparison", "supports_claim": "comparison"},
                ]
            ),
            abstract_logic="LEGACY ABSTRACT LOGIC MUST NOT ENTER PAPER CONTENT",
        )
        claim = MechanismClaim(
            paper_id=paper.id,
            claim_type="conversion",
            claim_text="Figure 2 supports accelerated sulfur conversion.",
            evidence_text="The conversion intermediates are shown in Figure 2.",
        )
        session.add_all([card, claim])
        session.flush()
        session.add(
            EvidenceLocator(
                paper_id=paper.id,
                target_type="mechanism_claims",
                target_id=str(claim.id),
                source_type="figure",
                page=reviewed.page,
                figure_id=reviewed.id,
                evidence_text=claim.evidence_text,
                locator_status="exact_page",
                locator_confidence=1.0,
                parser_source="test",
            )
        )
        session.flush()

        service = ContentFigureLinkService(session)
        card_links = service.links_for_writing_card(card)
        assert [item["figure_id"] for item in card_links] == [str(reviewed.id)]
        assert card_links[0]["figure_label"] == "Figure 2"
        assert card_links[0]["matched_by"] == "explicit_reference"
        assert card_links[0]["evidence_ids"] == ["evidence_chain:0"]
        assert card_links[0]["purpose"] == "support conversion"
        assert "support conversion" not in card_links[0]["evidence_ids"]
        assert card_links[0]["review_snapshot_fingerprint"]
        assert service.links_for_mechanism_claim(claim)[0]["matched_by"] == "evidence_locator"

        unrelated_card = WritingCard(
            paper_id=paper.id,
            research_gap="This card contains no figure reference.",
            evidence_chain=[
                {
                    "text": "The control experiment confirms the reported trend.",
                    "source": "Results",
                    "page": 5,
                    "locator_status": "exact_page",
                    "evidence_type": "result",
                }
            ],
            figure_logic=json.dumps(
                [{"fig_id": "Figure 2", "purpose": "paper-level overview"}]
            ),
        )
        session.add(unrelated_card)
        session.flush()
        assert ContentFigureLinkService(session).links_for_writing_card(unrelated_card) == []

        context = PaperKnowledgeService(session).build_context(paper.id)
        assert context is not None
        assert not any(
            "LEGACY ABSTRACT LOGIC" in str(item.get("content") or "")
            for item in context["candidates"]
        )
        key_result = next(
            item for item in context["candidates"]
            if item["source_type"] == "writing_card_evidence"
            and item["source_id"] == str(card.id)
            and item["category"] == "key_result"
        )
        assert key_result["metadata"]["linked_figures"][0]["figure_id"] == str(reviewed.id)

        previous_fingerprint = card_links[0]["review_snapshot_fingerprint"]
        reviewed.content_summary = "The reviewed mechanism map now includes Li2S nucleation and decomposition."
        session.flush()
        service = ContentFigureLinkService(session)
        refreshed = service.links_for_writing_card(card)
        assert refreshed[0]["review_snapshot_fingerprint"] != previous_fingerprint

        session.add(
            AuditLog(
                paper_id=paper.id,
                action="review_figure",
                source="figure_reviewer",
                target_type="paper_figure",
                target_id=str(reviewed.id),
                payload={"verdict": "rejected"},
            )
        )
        session.flush()
        service = ContentFigureLinkService(session)
        assert service.links_for_writing_card(card) == []
        assert service.links_for_mechanism_claim(claim) == []


def test_writing_card_persistence_creates_core_and_chain_locators(setup_test_db, tmp_path):
    settings = Settings(storage_root=Path(tmp_path), embedding_provider="deterministic")
    with _factory(setup_test_db).begin() as session:
        paper = Paper(paper_code="PC102", title="Grounded paper content", pdf_path="pc102.pdf", authors=[])
        session.add(paper)
        session.flush()
        figure = _figure(paper.id, number=2)
        session.add(figure)
        session.flush()

        service = ExtractionPipelineService(session, settings)
        service._embed_text = lambda _text: [0.0] * settings.embedding_dimension  # type: ignore[method-assign]
        created = service._persist_writing_card(
            paper.id,
            {
                "paper_type": "mixed",
                "research_gap": "Existing catalysts do not resolve the conversion pathway.",
                "proposed_solution": "This work uses a dual-atom catalyst to direct conversion.",
                "evidence_chain": [
                    {
                        "text": "Existing catalysts do not resolve the conversion pathway.",
                        "source": "Introduction",
                        "page": 1,
                        "supports_fields": ["research_gap"],
                        "locator_status": "exact_page",
                        "evidence_type": "core_field",
                    },
                    {
                        "text": figure.caption,
                        "source": "Figure",
                        "page": figure.page,
                        "supports_fields": [],
                        "locator_status": "exact_page",
                        "evidence_type": "caption",
                    },
                ],
                "figure_logic": [{"fig_id": "Figure 2", "purpose": "mechanism", "supports_claim": "conversion"}],
            },
        )
        assert created == 1
        card = session.scalar(select(WritingCard).where(WritingCard.paper_id == paper.id))
        assert card is not None
        locators = list(
            session.scalars(
                select(EvidenceLocator)
                .where(
                    EvidenceLocator.paper_id == paper.id,
                    EvidenceLocator.target_id == str(card.id),
                    EvidenceLocator.target_type.in_({"writing_card", "writing_cards"}),
                )
                .order_by(EvidenceLocator.field_name, EvidenceLocator.chunk_id)
            ).all()
        )
        assert any(
            item.field_name == "research_gap"
            and item.page == 1
            and item.evidence_text.startswith("Existing catalysts")
            for item in locators
        )
        generic = [item for item in locators if item.field_name is None]
        assert len(generic) == 2
        assert any(item.figure_id == figure.id for item in generic)
        session.add(
            AuditLog(
                paper_id=paper.id,
                action="review_figure",
                source="figure_reviewer",
                target_type="paper_figure",
                target_id=str(figure.id),
                payload={"verdict": "verified"},
            )
        )
        session.flush()
        links = ContentFigureLinkService(session).links_for_writing_card(card)
        assert len(links) == 1
        assert links[0]["matched_by"] == "evidence_locator"
        assert links[0]["purpose"] == "mechanism"
        assert "evidence_chain:1" in links[0]["evidence_ids"]
        assert "mechanism" not in links[0]["evidence_ids"]


def test_persistent_content_rebuilds_cached_figure_links_from_current_review_state(
    setup_test_db,
    monkeypatch,
):
    with _factory(setup_test_db).begin() as session:
        paper = Paper(paper_code="PC104", title="Projection freshness", pdf_path="pc104.pdf", authors=[])
        session.add(paper)
        session.flush()
        figure = _figure(paper.id, number=4)
        card = WritingCard(
            paper_id=paper.id,
            research_gap="The conversion mechanism needs direct evidence.",
            evidence_chain=[
                {
                    "text": "Figure 4 directly resolves the conversion pathway.",
                    "source": "Results",
                    "page": 5,
                    "locator_status": "exact_page",
                    "evidence_type": "result",
                }
            ],
        )
        session.add_all([figure, card])
        session.flush()
        session.add(
            AuditLog(
                paper_id=paper.id,
                action="review_figure",
                source="figure_reviewer",
                target_type="paper_figure",
                target_id=str(figure.id),
                payload={"verdict": "verified"},
            )
        )
        projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="writing_material",
            source_type="writing_card",
            source_id=str(card.id),
            source_record={"linked_figures": [{"figure_id": "stale-cached-id"}]},
            content="research_gap: The conversion mechanism needs direct evidence. | result: Figure 4 directly resolves the conversion pathway.",
            evidence_text="Figure 4 directly resolves the conversion pathway.",
            review_status="safe_verified",
            citation_status="writing_only",
        )
        session.add(projection)
        session.flush()
        safe_gate = ContentObjectGateResult(
            can_use_for_writing=True,
            can_use_for_citation=False,
            review_gate_status="safe_verified",
            locator_status="exact_page",
            blocked_reasons=(),
        )
        monkeypatch.setattr(
            "app.services.content_knowledge_service.content_object_gate",
            lambda *_args, **_kwargs: safe_gate,
        )
        monkeypatch.setattr(
            "app.services.content_knowledge_service.get_embedding_service",
            lambda **_: type("NoEmbedding", (), {"embed_text": lambda self, text: []})(),
        )

        rows = ContentKnowledgeService(session).search_for_rag(
            query="conversion mechanism",
            paper_ids=[paper.id],
        )
        assert len(rows) == 1
        item, _score = rows[0]
        assert item.metadata["linked_figures"][0]["figure_id"] == str(figure.id)
        assert projection.source_record["linked_figures"][0]["figure_id"] == "stale-cached-id"

        session.add(
            AuditLog(
                paper_id=paper.id,
                action="review_figure",
                source="figure_reviewer",
                target_type="paper_figure",
                target_id=str(figure.id),
                payload={"verdict": "rejected"},
            )
        )
        session.flush()
        refreshed_rows = ContentKnowledgeService(session).search_for_rag(
            query="conversion mechanism",
            paper_ids=[paper.id],
        )
        assert len(refreshed_rows) == 1
        refreshed, _score = refreshed_rows[0]
        assert refreshed.metadata["linked_figures"] == []


def test_legacy_figure_caption_prefix_never_matches_an_empty_caption(setup_test_db):
    with _factory(setup_test_db).begin() as session:
        paper = Paper(paper_code="PC105", title="Empty caption", pdf_path="pc105.pdf", authors=[])
        session.add(paper)
        session.flush()
        empty_caption = PaperFigure(
            paper_id=paper.id,
            figure_label="Figure 1",
            caption=None,
            image_path="figures/empty-caption.png",
            page=2,
        )
        session.add(empty_caption)
        session.flush()

        resolved = EvidenceLocatorService(session).resolve_figure_id(
            paper.id,
            "A sufficiently long legacy caption that has no matching stored caption.",
        )
        assert resolved is None


def test_unified_writing_projection_snapshot_excludes_legacy_logic_fields(setup_test_db):
    with _factory(setup_test_db).begin() as session:
        paper = Paper(paper_code="PC106", title="Unified snapshot", pdf_path="pc106.pdf", authors=[])
        session.add(paper)
        session.flush()
        card = WritingCard(
            paper_id=paper.id,
            research_gap="The active-site pathway remains unresolved.",
            evidence_chain=[
                {
                    "text": "The operando result resolves the active-site pathway.",
                    "source": "Results",
                    "page": 6,
                    "locator_status": "exact_page",
                    "evidence_type": "result",
                }
            ],
            abstract_logic="legacy abstract logic",
            introduction_logic="legacy introduction logic",
            discussion_logic="legacy discussion logic",
            figure_logic=json.dumps([{"fig_id": "Figure 8", "purpose": "legacy overview"}]),
        )
        session.add(card)
        session.flush()
        projection = ContentEvidenceItem(
            paper_id=paper.id,
            category="writing_material",
            source_type="writing_card",
            source_id=str(card.id),
            content=(
                "research_gap: The active-site pathway remains unresolved. | "
                "result: The operando result resolves the active-site pathway."
            ),
            evidence_text="The operando result resolves the active-site pathway.",
            review_status="needs_review",
            citation_status="needs_review",
        )
        session.add(projection)
        session.flush()

        gate = content_object_gate(session, projection.source_type, projection)
        assert "content_projection_snapshot_mismatch" not in gate.blocked_reasons


def test_writing_plan_exposes_reviewed_writing_context_with_chain_and_figures(
    setup_test_db,
    monkeypatch,
):
    monkeypatch.setattr(
        ContentKnowledgeService,
        "count_unreviewed_matching",
        lambda self, **kwargs: 0,
    )

    with _factory(setup_test_db).begin() as session:
        paper = Paper(paper_code="PC103", title="Current links", pdf_path="pc103.pdf", authors=[])
        session.add(paper)
        session.flush()
        figure = _figure(paper.id, number=2)
        card = WritingCard(
            paper_id=paper.id,
            research_gap="A reviewed key result supports the discussion.",
            evidence_chain=[
                {
                    "text": "The reviewed result appears in Figure 2.",
                    "source": "Results",
                    "page": 4,
                    "locator_status": "exact_page",
                    "evidence_type": "result",
                }
            ],
        )
        session.add_all([figure, card])
        session.flush()
        session.add(
            AuditLog(
                paper_id=paper.id,
                action="review_figure",
                source="figure_reviewer",
                target_type="paper_figure",
                target_id=str(figure.id),
                payload={"verdict": "verified"},
            )
        )
        session.flush()
        writing_only = SimpleNamespace(
            can_use_for_writing=True,
            can_use_for_citation=False,
            item_id="content-item-1",
            paper_id=str(paper.id),
            paper_code=paper.paper_code,
            source_type="writing_card",
            source_id=str(card.id),
            content="A reviewed key result supports the discussion.",
            evidence_text="The reviewed result appears in Figure 2.",
            evidence_locator={"page": 4, "locator_status": "exact_page"},
            page_start=4,
            page_end=4,
            metadata={
                "evidence_chain": [{"evidence_id": "evidence_chain:0", "text": "Reviewed result"}],
                "linked_figures": [{"figure_id": "stale-projection-link"}],
            },
        )
        monkeypatch.setattr(
            ContentKnowledgeService,
            "search_for_rag",
            lambda self, **kwargs: [(writing_only, 1.0)],
        )
        plan = ContentWritingPlanService(session).build(query="reviewed result")

        assert plan["writing_context"][0]["linked_figures"][0]["figure_id"] == str(figure.id)
        session.add(
            AuditLog(
                paper_id=paper.id,
                action="review_figure",
                source="figure_reviewer",
                target_type="paper_figure",
                target_id=str(figure.id),
                payload={"verdict": "rejected"},
            )
        )
        session.flush()
        refreshed_plan = ContentWritingPlanService(session).build(query="reviewed result")

    assert plan["citation_eligible"] == 0
    assert plan["writing_only_eligible"] == 1
    assert plan["no_citable_match"] is True
    assert plan["writing_context"][0]["evidence_chain"][0]["text"] == "Reviewed result"
    assert refreshed_plan["writing_context"][0]["linked_figures"] == []
