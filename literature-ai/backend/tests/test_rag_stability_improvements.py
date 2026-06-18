from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Base, ExtractionFieldReview, Paper, PaperChunk, PaperFigure, PaperSection
from app.rag.eligibility import is_rag_eligible
from app.rag.prompt_builder import PaperWriterPromptBuilder
from app.rag.retriever import Retriever
from app.services.embedding import EmbeddingUnavailableError


def _engine(name: str):
    tempdir = TemporaryDirectory()
    engine = create_engine(f"sqlite:///{Path(tempdir.name) / name}", future=True)
    Base.metadata.create_all(engine)
    return tempdir, engine


def _safe_figure(paper_id, **overrides):
    values = {
        "paper_id": paper_id,
        "caption": "Figure 1. Catalytic pathway comparison.",
        "image_path": "figures/one.png",
        "page": 2,
        "figure_role": "mechanism",
        "content_summary": "Two pathways compare distinct intermediates and activation barriers.",
        "key_elements": ["reaction arrows", "intermediates", "barrier labels"],
        "crop_status": "candidate_crop",
    }
    values.update(overrides)
    return PaperFigure(**values)


def test_latest_figure_review_is_authoritative_and_rejected_survives_recrop():
    tempdir, engine = _engine("figures.db")
    try:
        with Session(engine) as session:
            paper = Paper(title="Figure paper", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            figure = _safe_figure(paper.id)
            session.add(figure)
            session.flush()

            session.add(AuditLog(paper_id=paper.id, action="review_figure", source="reviewer", target_type="paper_figure", target_id=str(figure.id), payload={"verdict": "verified"}))
            session.flush()
            assert is_rag_eligible(session, figure, "figure") is True

            session.add(AuditLog(paper_id=paper.id, action="review_figure", source="reviewer", target_type="paper_figure", target_id=str(figure.id), payload={"verdict": "rejected"}))
            figure.crop_status = "recropped"
            figure.crop_source = "recrop:ai_bbox:reviewer"
            session.flush()
            assert is_rag_eligible(session, figure, "figure") is False
    finally:
        engine.dispose()
        tempdir.cleanup()


def test_all_unsafe_figure_states_remain_blocked():
    tempdir, engine = _engine("unsafe_figures.db")
    try:
        with Session(engine) as session:
            paper = Paper(title="Unsafe figures", pdf_path="paper.pdf", authors=["A"])
            session.add(paper)
            session.flush()
            for status in ["needs_repair", "missing", "failed", "noisy", "full_page"]:
                figure = _safe_figure(paper.id, crop_status=status)
                session.add(figure)
                session.flush()
                session.add(AuditLog(paper_id=paper.id, action="review_figure", source="reviewer", target_type="paper_figure", target_id=str(figure.id), payload={"verdict": "verified"}))
                session.flush()
                assert is_rag_eligible(session, figure, "figure") is False
    finally:
        engine.dispose()
        tempdir.cleanup()


class FailingEmbedding:
    dimension = 2

    def embed_text(self, text):
        raise EmbeddingUnavailableError("offline")

    def cosine_similarity(self, left, right):
        raise AssertionError("semantic score must not run after embedding failure")


def test_section_and_chunk_candidates_merge_without_entering_writing_context():
    tempdir, engine = _engine("sections.db")
    try:
        with Session(engine) as session:
            paper = Paper(title="Boundary paper", pdf_path="paper.pdf", authors=["A"])
            other = Paper(title="Other paper", pdf_path="other.pdf", authors=["B"])
            session.add_all([paper, other])
            session.flush()
            section = PaperSection(paper_id=paper.id, section_title="Results", section_type="results", text="alpha bridge beta describes a boundary-spanning catalytic mechanism.", page_start=3, page_end=4)
            other_section = PaperSection(paper_id=other.id, section_title="Results", section_type="results", text="alpha unrelated beta belongs to another paper.", page_start=8, page_end=8)
            session.add_all([section, other_section])
            session.flush()
            session.add_all([
                PaperChunk(paper_id=paper.id, section_id=section.id, chunk_index=0, text="alpha bridge begins the catalytic discussion.", page_start=3, page_end=3, content_hash="a"),
                PaperChunk(paper_id=paper.id, section_id=section.id, chunk_index=1, text="beta completes the catalytic mechanism.", page_start=4, page_end=4, content_hash="b"),
                PaperChunk(paper_id=other.id, section_id=other_section.id, chunk_index=0, text="alpha unrelated beta from other paper.", page_start=8, page_end=8, content_hash="c"),
            ])
            session.commit()

            results = Retriever(session, embedding=FailingEmbedding()).retrieve("alpha beta", [paper.id], 5)["sections"]
            assert results
            assert all(item["paper_id"] == paper.id for item in results)
            assert any(item.get("chunk_index") is not None for item in results)
            assert any(item.get("chunk_index") is None and item["section_id"] == section.id for item in results)
            assert all(item["retrieval_tier"] == "discovery_candidate" for item in results)
            assert all(item["score_breakdown"]["semantic"] == 0.0 for item in results)

            payload = PaperWriterPromptBuilder().build("alpha beta", None, ["introduction"], {"sections": results})
            assert payload["retrieved"]["sections"] == []
            assert payload["evidence_pack"]["introduction"] == []

            session.add(ExtractionFieldReview(paper_id=paper.id, target_type="sections", target_id=str(section.id), field_name="text", reviewer_status="verified", target_resolution_status="active", evidence_text=section.text))
            session.flush()
            formal = Retriever(session, embedding=FailingEmbedding()).retrieve("alpha beta", [paper.id], 5)["sections"]
            assert any(item["can_use_for_writing"] is True for item in formal)
            formal_payload = PaperWriterPromptBuilder().build("alpha beta", None, ["introduction"], {"sections": formal})
            assert formal_payload["retrieved"]["sections"]
    finally:
        engine.dispose()
        tempdir.cleanup()
