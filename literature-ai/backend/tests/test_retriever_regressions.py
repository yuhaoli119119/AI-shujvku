import pytest
from app.rag.retriever import Retriever
from app.services.embedding import EmbeddingUnavailableError
from app.services.retrieval_service import RetrievalService
from app.schemas.retrieval import RetrievalSearchRequest
from unittest.mock import MagicMock
import uuid

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
    session = MagicMock()
    embedding = MagicMock()
    retriever = Retriever(session, embedding_dimension=1536, embedding=embedding)
    
    class MockRow:
        def __init__(self, cond):
            self.metric_name = "overpotential"
            self.metric_value = 100
            self.unit = "mV"
            self.sample_label = "Pt/C"
            self.conditions = cond
            self.id = "id1"
            self.paper_id = "p1"
            self.figure_id = "fig1"

    row1 = MockRow("0.1M KOH")
    row2 = MockRow("1.0M KOH")
    
    session.execute.return_value.all.return_value = [(row1, "Fig 1"), (row2, "Fig 1")]
    retriever._score_text = MagicMock(return_value=1.0)
    
    results = retriever._retrieve_figure_data({"test", "query"}, [0.1, 0.2], [], 10)
    
    assert len(results) == 2
    assert "0.1M KOH" in results[0]["evidence_text"]
    assert "1.0M KOH" in results[1]["evidence_text"]

def test_hybrid_score_no_dynamic_embed():
    embedding = MagicMock()
    retriever = Retriever(MagicMock(), embedding_dimension=1536, embedding=embedding)
    
    retriever._score_text = MagicMock(return_value=0.5)
    
    score, breakdown = retriever._hybrid_score({"test"}, [0.1], "test text", None, False)
    
    assert embedding.embed_text.call_count == 0
    assert breakdown["semantic"] == 0.0
    assert score > 0


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
    assert score > 0
    assert breakdown["semantic"] == 0.0

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
    res = retrieval_service.search(req)
    
    assert res.reranker["enabled"] is False
    assert res.reranker["name"] == "disabled_for_full_context"
    assert len(res.items) == 3
