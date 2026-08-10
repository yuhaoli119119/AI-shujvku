from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api import content_knowledge as content_knowledge_api
from app.services import content_writing_plan_service as writing_plan_module
from app.services.content_writing_plan_service import ContentWritingPlanService


PAPER_A = UUID(int=101)
PAPER_B = UUID(int=102)


def _planned_evidence(
    paper_id: UUID,
    evidence_id: str,
    *,
    can_cite: bool,
) -> dict:
    return {
        "evidence_id": evidence_id,
        "source_paper_id": str(paper_id),
        "paper_code": f"B{paper_id.int:04d}",
        "doi": f"10.1000/{paper_id.int}",
        "evidence_type": "writing_cards",
        "object_id": f"object-{evidence_id}",
        "page_start": 4,
        "page_end": 4,
        "evidence_locator": {"page": 4, "locator_status": "exact_page"},
        "review_status": "safe_verified",
        "gate_status": "safe_verified",
        "can_use_for_writing": True,
        "can_use_for_citation": can_cite,
        "excerpt": f"Safe evidence {evidence_id}",
        "property": None,
        "value": None,
        "unit": None,
        "context": None,
    }


def _fake_plan() -> dict:
    citable = _planned_evidence(PAPER_A, "ev-a", can_cite=True)
    writing_only = _planned_evidence(PAPER_B, "ev-b", can_cite=False)
    return {
        "schema_version": "multi_paper_evidence_plan.v1",
        "plan_id": "plan:test",
        "plan_fingerprint": "stable-plan-fingerprint",
        "query": "safe narrative",
        "retrieval_intent": "narrative",
        "retrieval_mode": "narrative",
        "selected_evidence_types": ["writing_cards"],
        "dft_included": False,
        "dft_included_reason": "not_requested",
        "paper_scope": {
            "valid_papers": [
                {"paper_id": str(PAPER_A), "paper_code": "B0101", "doi": "10.1000/101"},
                {"paper_id": str(PAPER_B), "paper_code": "B0102", "doi": "10.1000/102"},
            ],
        },
        "requested_paper_count": 2,
        "valid_paper_count": 2,
        "represented_paper_count": 2,
        "batches": [
            {
                "batch_id": "batch-001",
                "batch_index": 1,
                "paper_ids": [str(PAPER_A)],
                "paper_codes": ["B0101"],
                "selected_evidence_ids": ["ev-a"],
                "budget": {"used": 1},
            },
            {
                "batch_id": "batch-002",
                "batch_index": 2,
                "paper_ids": [str(PAPER_B)],
                "paper_codes": ["B0102"],
                "selected_evidence_ids": ["ev-b"],
                "budget": {"used": 1},
            },
        ],
        "budgets": {"evidence_budget": 24, "used": 2, "remaining": 22},
        "selected_evidence": [citable, writing_only],
        "claim_evidence_matrix": [],
        "coverage": {
            "coverage_complete": True,
            "by_paper": [
                {"paper_id": str(PAPER_A), "paper_code": "B0101", "status": "represented"},
                {"paper_id": str(PAPER_B), "paper_code": "B0102", "status": "represented"},
            ],
        },
        "warnings": [],
        "database_writes": False,
        "read_only": True,
    }


@pytest.mark.no_test_database
def test_service_builds_bounded_batch_contexts_and_safe_compatibility_fields(monkeypatch):
    session = MagicMock()
    planner = MagicMock()
    planner.plan.return_value = _fake_plan()
    captured_scope: dict = {}

    class FakeKnowledge:
        def __init__(self, _session):
            pass

        def count_unreviewed_matching(self, **kwargs):
            captured_scope.update(kwargs)
            return 2

    monkeypatch.setattr(writing_plan_module, "ContentKnowledgeService", FakeKnowledge)
    result = ContentWritingPlanService(
        session,
        multi_paper_planner=planner,
    ).build(
        query="safe narrative",
        paper_ids=[str(PAPER_A), str(PAPER_B)],
    )

    assert result["bounded_multi_paper_plan_used"] is True
    assert result["web_model_disabled"] is True
    assert result["database_writes"] is False
    assert result["persistence"] == {"writes_db": False, "saved_plan": False}
    assert [context["batch_id"] for context in result["batch_prompt_contexts"]] == [
        "batch-001",
        "batch-002",
    ]
    assert [
        [card["evidence_id"] for card in context["evidence_cards"]]
        for context in result["batch_prompt_contexts"]
    ] == [["ev-a"], ["ev-b"]]
    assert all(
        any(
            constraint["code"] == "citation_eligibility_required"
            for constraint in context["constraints"]
        )
        for context in result["batch_prompt_contexts"]
    )
    assert result["citation_eligible"] == 1
    assert result["writing_only_eligible"] == 1
    assert result["citation_plan"][0]["evidence_item_id"] == "ev-a"
    assert result["citation_plan"][0]["locator"] == {
        "page": 4,
        "locator_status": "exact_page",
    }
    assert result["writing_context"][0]["content_item_id"] == "ev-b"
    assert result["writing_context"][0]["gate_status"] == "safe_verified"
    assert captured_scope["paper_ids"] == [PAPER_A, PAPER_B]
    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.no_test_database
def test_writing_plan_api_forwards_bounded_defaults_and_preserves_unknown_ids(monkeypatch):
    captured: dict = {}

    class FakeService:
        def __init__(self, _session):
            pass

        def build(self, **kwargs):
            captured.update(kwargs)
            return {"persistence": {"writes_db": False}, "database_writes": False}

    monkeypatch.setattr(content_knowledge_api, "ContentWritingPlanService", FakeService)
    unknown_id = str(UUID(int=999))
    result = content_knowledge_api.content_writing_plan(
        {
            "query": "ordinary narrative",
            "paper_ids": [unknown_id, unknown_id],
        },
        session=MagicMock(),
    )

    assert result["database_writes"] is False
    assert captured == {
        "query": "ordinary narrative",
        "paper_ids": [unknown_id, unknown_id],
        "mode": "narrative",
        "evidence_types": None,
        "requested_sections": None,
        "evidence_budget": 24,
        "batch_size": 10,
        "max_evidence_per_paper": 3,
        "max_sources_per_claim": 5,
        "candidate_pool_per_type": 24,
    }


@pytest.mark.no_test_database
@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"query": "x", "paper_ids": "not-a-list"}, "paper_ids must be a list"),
        ({"query": "x", "paper_ids": [], "batch_size": True}, "batch_size must be an integer"),
        ({"query": "x", "paper_ids": [], "evidence_budget": "many"}, "evidence_budget must be an integer"),
        ({"query": "x", "paper_ids": [], "evidence_budget": 49}, "evidence_budget must be between 1 and 48"),
        ({"query": "x", "paper_ids": [], "batch_size": 11}, "batch_size must be between 1 and 10"),
        ({"query": "x", "paper_ids": [], "evidence_types": "all"}, "evidence_types must be a list"),
    ],
)
def test_writing_plan_api_validation_returns_4xx(monkeypatch, payload, detail):
    session = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        content_knowledge_api.content_writing_plan(payload, session=session)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == detail
    session.rollback.assert_called_once_with()


@pytest.mark.no_test_database
def test_writing_plan_api_rejects_blank_query_with_422():
    with pytest.raises(HTTPException) as exc_info:
        content_knowledge_api.content_writing_plan(
            {"query": "  ", "paper_ids": []},
            session=MagicMock(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "query must not be blank"
