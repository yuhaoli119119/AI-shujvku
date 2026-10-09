import pytest
from app.rag.retriever import Retriever

def test_retriever_global_dedup_none_values():
    retrieved = {
        "mechanism_claims": [
            {
                "paper_id": "some_uuid",
                "text": None,
                "evidence_text": None,
                "score": 0.9,
            }
        ]
    }
    
    # Should not raise AttributeError: 'NoneType' object has no attribute 'strip'
    result = Retriever._global_dedup(retrieved, limit_per_type=1)
    
    # Assert it returns without crashing and preserves the structure
    assert len(result["mechanism_claims"]) == 1
    assert result["mechanism_claims"][0]["score"] == 0.9
