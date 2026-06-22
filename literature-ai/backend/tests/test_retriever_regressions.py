import os
import pytest
from app.rag.retriever import Retriever
from app.services.embedding import EmbeddingUnavailableError
from app.services.retrieval_service import RetrievalService
from app.schemas.retrieval import RetrievalSearchRequest
from unittest.mock import MagicMock, patch
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    CatalystSample,
    DFTResult,
    EvidenceSpan,
    ExtractionFieldReview,
    FigureDataPoint,
    Paper,
    PaperFigure,
    PaperSection,
    WritingCard,
)

def test_global_dedup_truncation():
    long_prefix = "A" * 80
    item1 = {"paper_id": "p1", "type": "section", "object_id": "obj1", "score": 0.9, "text": f"{long_prefix} suffix1"}
    item2 = {"paper_id": "p1", "type": "section", "object_id": "obj2", "score": 0.8, "text": f"{long_prefix} suffix2"}
    
    retrieved = {"section": [item1, item2]}
    deduped = Retriever._global_dedup(retrieved, 10)
    assert len(deduped["section"]) == 2, "Both items should be kept because text differs"

    item3 = {"paper_id": "p1", "type": "section", "object_id": "obj3", "score": 0.9, "text": "Exact Same Text"}
    item4 = {"paper_id": "p1", "type": "dft", "object_id": "obj4", "score": 0.8, "text": "Exact Same Text"}
    deduped2 = Retriever._global_dedup({"section": [item3], "dft": [item4]}, 10)
    assert len(deduped2["section"]) == 1
    assert len(deduped2["dft"]) == 1, "Distinct object_id rows must not be silently deduped"


def test_global_dedup_content_fallback_only_for_synthetic_items():
    high = {"paper_id": "p1", "score": 0.9, "text": "Synthetic exact evidence"}
    low = {"paper_id": "p1", "score": 0.7, "text": "Synthetic exact evidence"}

    deduped = Retriever._global_dedup({"sections": [high], "cards": [low]}, 10)

    assert len(deduped["sections"]) == 1
    assert len(deduped["cards"]) == 0

def test_figure_evidence_conditions():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="Figure evidence paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            figure = PaperFigure(
                paper_id=paper.id,
                caption="Fig 1. Overpotential comparison under KOH electrolyte.",
                image_path="figures/fig1.png",
                page=1,
                crop_status="candidate_crop",
                figure_role="performance_plot",
                content_summary="Overpotential values for Pt/C under two KOH electrolyte concentrations.",
                key_elements=["overpotential", "Pt/C", "0.1M KOH", "1.0M KOH"],
            )
            session.add(figure)
            session.flush()
            session.add_all(
                [
                    FigureDataPoint(
                        figure_id=figure.id,
                        paper_id=paper.id,
                        metric_name="overpotential",
                        metric_value=100,
                        unit="mV",
                        sample_label="Pt/C",
                        conditions={"electrolyte": "0.1M KOH"},
                    ),
                    FigureDataPoint(
                        figure_id=figure.id,
                        paper_id=paper.id,
                        metric_name="overpotential",
                        metric_value=100,
                        unit="mV",
                        sample_label="Pt/C",
                        conditions={"electrolyte": "1.0M KOH"},
                    ),
                ]
            )
            session.commit()

            retriever = Retriever(session, embedding_dimension=1536, embedding=MagicMock())
            retriever._score_text = MagicMock(return_value=1.0)

            results = retriever._retrieve_figure_data({"test", "query"}, [0.1, 0.2], [], 10)

            assert len(results) == 2
            evidence = "\n".join(result["evidence_text"] for result in results)
            assert "0.1M KOH" in evidence
            assert "1.0M KOH" in evidence
        engine.dispose()

class TinySemanticEmbedding:
    dimension = 2

    def __init__(self):
        self.calls = 0

    def embed_text(self, text):
        self.calls += 1
        if "oxygen reduction" in text.lower():
            return [1.0, 0.0]
        return [0.0, 1.0]

    def cosine_similarity(self, left, right):
        if not left or not right or len(left) != len(right):
            return 0.0
        return sum(a * b for a, b in zip(left, right))


def test_hybrid_score_embeds_structured_text_without_stored_embedding():
    embedding = TinySemanticEmbedding()
    retriever = Retriever(MagicMock(), embedding_dimension=1536, embedding=embedding)

    retriever._score_text = MagicMock(return_value=0.5)

    score, breakdown = retriever._hybrid_score({"orr"}, [1.0, 0.0], "oxygen reduction reaction", None, False)

    assert embedding.calls == 1
    assert breakdown["semantic"] == 1.0
    assert score == 0.675


def test_hybrid_score_uses_cached_structured_text_embedding():
    embedding = TinySemanticEmbedding()
    retriever = Retriever(MagicMock(), embedding_dimension=1536, embedding=embedding)

    retriever._score_text = MagicMock(return_value=0.0)

    first_score, first_breakdown = retriever._hybrid_score({"orr"}, [1.0, 0.0], "oxygen reduction reaction", None, False)
    second_score, second_breakdown = retriever._hybrid_score({"orr"}, [1.0, 0.0], "oxygen reduction reaction", None, False)

    assert embedding.calls == 1
    assert first_score == second_score
    assert first_breakdown == second_breakdown


def test_query_embedding_failure_falls_back_to_lexical():
    embedding = MagicMock()
    embedding.embed_text.side_effect = EmbeddingUnavailableError("rate limited")
    retriever = Retriever(MagicMock(), embedding_dimension=1536, embedding=embedding)

    query_embedding = retriever._safe_query_embedding("graphdiyne adsorption energy")
    score, breakdown = retriever._hybrid_score(
        {"graphdiyne", "adsorption", "energy"},
        query_embedding,
        "graphdiyne adsorption energy from DFT",
        None,
        False,
    )

    assert query_embedding == []
    assert score == 1.0
    assert breakdown["semantic"] == 0.0


def test_structured_token_prefilter_empty_result_falls_back_with_paper_scope():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            p1 = Paper(title="Target Paper", pdf_path="target.pdf", authors=["A"])
            p2 = Paper(title="Other Paper", pdf_path="other.pdf", authors=["B"])
            session.add_all([p1, p2])
            session.flush()
            session.add(
                DFTResult(
                    paper_id=p1.id,
                    property_type="adsorption_energy",
                    adsorbate="H2O",
                    value=-0.5,
                    unit="eV",
                    evidence_text="No matching query token appears here.",
                )
            )
            session.add(
                DFTResult(
                    paper_id=p2.id,
                    property_type="band_gap",
                    value=1.1,
                    unit="eV",
                    evidence_text="This row is outside the requested paper.",
                )
            )
            session.commit()

            retriever = Retriever(session)
            query = select(DFTResult).where(DFTResult.paper_id == p1.id)
            rows = retriever._scalars_with_token_prefilter(
                query,
                {"zzzzzz"},
                [DFTResult.evidence_text],
                fallback_limit=20,
            )

            assert len(rows) == 1
            assert rows[0].paper_id == p1.id
            assert rows[0].value == -0.5

        engine.dispose()


def test_retriever_returns_safe_cards_and_marks_raw_sections_discovery_only():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
        Base.metadata.create_all(engine)

        with Session(engine) as session:
            paper = Paper(title="RAG Cards Paper", pdf_path="paper.pdf", authors=["A"])
            paper.paper_code = "A0002"
            session.add(paper)
            session.flush()
            catalyst = CatalystSample(paper_id=paper.id, name="Fe-N4", catalyst_type="single_atom")
            session.add(catalyst)
            session.flush()
            raw_section = PaperSection(
                paper_id=paper.id,
                section_title="Results",
                section_type="results",
                text="Raw section mentions Fe-N4 Li2S4 adsorption energy but has not been AI reviewed.",
                page_start=5,
                page_end=5,
            )
            figure = PaperFigure(
                paper_id=paper.id,
                figure_label="Figure 2",
                caption="Figure 2. DFT adsorption energy map for Fe-N4 and Li2S4.",
                image_path="figures/figure-2.png",
                page=6,
                figure_role="dft_evidence",
                content_summary="DFT figure showing Li2S4 adsorption on Fe-N4.",
                key_elements=["Fe-N4", "Li2S4", "adsorption energy"],
            )
            dft = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=catalyst.id,
                adsorbate="Li2S4",
                property_type="adsorption_energy",
                value=-1.23,
                unit="eV",
                source_section="Results",
                source_figure="Figure 2",
                evidence_text="The adsorption energy of Li2S4 on Fe-N4 was -1.23 eV.",
            )
            candidate_dft = DFTResult(
                paper_id=paper.id,
                catalyst_sample_id=catalyst.id,
                adsorbate="Li2S6",
                property_type="adsorption_energy",
                value=-0.88,
                unit="eV",
                source_section="Results",
                source_figure="Figure 2",
                evidence_text="Candidate-only Li2S6 adsorption energy was -0.88 eV.",
                candidate_status="candidate",
            )
            writing_card = WritingCard(
                paper_id=paper.id,
                paper_type="A_dft",
                research_gap="Existing Li-S DFT studies lack reliable Fe-N4 Li2S4 adsorption comparisons.",
                proposed_solution="Use Fe-N4 active sites to regulate Li2S4 adsorption.",
                core_hypothesis="Verified writing card: Fe-N4 improves Li2S4 adsorption energetics.",
                    evidence_chain=[
                        {
                            "text": "Existing Li-S DFT studies lack reliable Fe-N4 Li2S4 adsorption comparisons.",
                            "source": "Introduction",
                            "page": 6,
                            "locator_status": "exact_page",
                            "supports_fields": ["research_gap"],
                        },
                        {
                            "text": "Use Fe-N4 active sites to regulate Li2S4 adsorption.",
                            "source": "Introduction",
                            "page": 6,
                            "locator_status": "exact_page",
                            "supports_fields": ["proposed_solution"],
                        },
                        {
                            "text": "Verified writing card: Fe-N4 improves Li2S4 adsorption energetics.",
                            "source": "Introduction",
                            "page": 6,
                            "locator_status": "exact_page",
                            "supports_fields": ["core_hypothesis"],
                        },
                ],
            )
            session.add_all([raw_section, figure, dft, candidate_dft, writing_card])
            session.flush()
            session.add(
                EvidenceSpan(
                    paper_id=paper.id,
                    object_type="dft_results",
                    object_id=str(dft.id),
                    text=dft.evidence_text,
                    page=6,
                    section="Results",
                    figure="Figure 2",
                )
            )
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(dft.id),
                    field_name="value",
                    reviewer_status="verified",
                    target_resolution_status="active",
                    evidence_text=dft.evidence_text,
                )
            )
            session.commit()

            retrieved = Retriever(session).retrieve("Fe-N4 Li2S4 adsorption energy Figure 2", [paper.id], 5)

            assert len(retrieved["sections"]) == 1
            assert retrieved["sections"][0]["object_id"] == raw_section.id
            assert retrieved["sections"][0]["retrieval_tier"] == "discovery_candidate"
            assert retrieved["sections"][0]["can_use_for_writing"] is False
            assert retrieved["figure_cards"]
            assert retrieved["figure_cards"][0]["object_id"] == figure.id
            assert retrieved["figure_cards"][0]["asset_url"] == "/api/papers/assets/figures/figure-2.png"
            assert retrieved["figure_cards"][0]["source_type"] == "figure"
            assert retrieved["figure_cards"][0]["source_id"] == str(figure.id)
            assert retrieved["figure_cards"][0]["paper_code"] == "A0002"
            assert retrieved["figure_cards"][0]["page"] == 6
            assert retrieved["figure_cards"][0]["review_status"] == "safe_verified_or_reliable_figure"
            assert retrieved["dft_results"]
            assert retrieved["dft_results"][0]["object_id"] == dft.id
            assert retrieved["dft_results"][0]["material_identity"]["name"] == "Fe-N4"
            assert retrieved["dft_results"][0]["evidence_locator"]["page"] == 6
            assert retrieved["dft_results"][0]["source_type"] == "dft_result"
            assert retrieved["dft_results"][0]["source_id"] == str(dft.id)
            assert retrieved["dft_results"][0]["paper_code"] == "A0002"
            assert retrieved["dft_results"][0]["page"] == 6
            assert retrieved["dft_results"][0]["review_status"] == "verified"
            assert all(item["object_id"] != candidate_dft.id for item in retrieved["dft_results"])
            assert retrieved["writing_cards"]
            assert retrieved["writing_cards"][0]["object_id"] == writing_card.id
            assert retrieved["writing_cards"][0]["source_type"] == "writing_card"
            assert retrieved["writing_cards"][0]["source_id"] == str(writing_card.id)
            assert retrieved["writing_cards"][0]["paper_code"] == "A0002"
            assert retrieved["writing_cards"][0]["page"] == 6
            assert retrieved["writing_cards"][0]["review_status"] == "content_verified"

        engine.dispose()

def test_full_context_mode():
    session = MagicMock()
    retrieval_service = RetrievalService(session=session)
    
    class MockSection:
        def __init__(self, pid, text):
            self.paper_id = pid
            self.id = uuid.uuid4()
            self.section_title = "Title"
            self.text = text
            self.page_start = 1
            self.page_end = 1
            self.section_type = "body"

    p1 = uuid.uuid4()
    p2 = uuid.uuid4()
    session.scalars.return_value.all.side_effect = [
        [MockSection(p1, "text1"), MockSection(p1, "text2")],
        [MockSection(p2, "text3")]
    ]

    req = RetrievalSearchRequest(query="test", mode="full_context", paper_ids=[p1, p2], limit=10, rerank=True)
    with patch("app.services.retrieval_service.is_rag_eligible", return_value=True):
        res = retrieval_service.search(req)
    
    assert res.reranker["enabled"] is False
    assert res.reranker["name"] == "disabled_for_full_context"
    assert len(res.items) == 3


def test_retrieval_service_cleans_mojibake_result_text():
    paper_id = uuid.uuid4()
    row = {
        "type": "figure_card",
        "paper_id": paper_id,
        "object_id": uuid.uuid4(),
        "score": 1.0,
        "text": "\u00ce\u00b1 absorption",
        "evidence_text": "\u00ce\u00b5 dielectric",
        "section_title": "\u00ce\u00b1 section",
    }

    items = RetrievalService._flatten_retrieved({"figure_cards": [row]})

    assert items[0].text == "\u03b5 dielectric"
    assert items[0].evidence_text == "\u03b5 dielectric"
    assert items[0].section_title == "\u03b1 section"
    assert items[0].evidence.evidence_text == "\u03b5 dielectric"
