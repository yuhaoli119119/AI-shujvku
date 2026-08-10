from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.rag.retrieval_intent import SUPPORTED_EVIDENCE_TYPES, route_retrieval_intent
from app.rag.retriever import Retriever


_RETRIEVAL_METHODS = {
    "sections": "_retrieve_sections",
    "catalyst_samples": "_retrieve_catalyst_samples",
    "dft_results": "_retrieve_dft_results",
    "electrochemical_performance": "_retrieve_electrochemical",
    "mechanism_claims": "_retrieve_mechanism_claims",
    "writing_cards": "_retrieve_writing_cards",
    "figure_cards": "_retrieve_figure_cards",
    "figure_data_points": "_retrieve_figure_data",
}


def _mocked_retriever() -> Retriever:
    retriever = Retriever(MagicMock())
    retriever._safe_query_embedding = MagicMock(return_value=[])
    for method_name in _RETRIEVAL_METHODS.values():
        setattr(retriever, method_name, MagicMock(return_value=[]))
    return retriever


def test_narrative_router_excludes_dft_and_keeps_writing_evidence_types():
    intent = route_retrieval_intent("总结研究问题、解决方案、机理与关键图")

    assert intent.intent == "mechanism"
    assert intent.dft_included is False
    assert intent.dft_included_reason == "no_dft_intent"
    assert "dft_results" not in intent.selected_evidence_types
    assert {
        "sections",
        "catalyst_samples",
        "mechanism_claims",
        "writing_cards",
        "figure_cards",
    }.issubset(intent.selected_evidence_types)


@pytest.mark.parametrize(
    ("query", "paper_ids"),
    [
        ("总结研究问题、解决方案、机理与关键图", [uuid4()]),
        ("", [uuid4()]),
        ("general literature narrative", None),
    ],
)
def test_narrative_retrieval_never_dispatches_dft_even_with_scope_or_empty_query(query, paper_ids):
    retriever = _mocked_retriever()

    result = retriever.retrieve(query, paper_ids=paper_ids)

    retriever._retrieve_dft_results.assert_not_called()
    assert result["dft_results"] == []
    assert set(result) == set(SUPPORTED_EVIDENCE_TYPES)
    assert retriever.last_retrieval_intent is not None
    assert retriever.last_retrieval_intent.dft_included is False


def test_explicit_dft_query_dispatches_dft_retrieval():
    retriever = _mocked_retriever()

    retriever.retrieve("Compare DFT adsorption energies and charge transfer")

    retriever._retrieve_dft_results.assert_called_once()
    retriever._retrieve_electrochemical.assert_not_called()
    retriever._retrieve_figure_data.assert_not_called()
    assert retriever.last_retrieval_intent is not None
    assert retriever.last_retrieval_intent.intent == "dft_quantitative"
    assert retriever.last_retrieval_intent.dft_included_reason == "query_intent"


def test_requested_dft_section_dispatches_dft_retrieval():
    retriever = _mocked_retriever()

    retriever.retrieve(
        "Summarize the catalyst study",
        requested_sections=["introduction", "dft_results"],
    )

    retriever._retrieve_dft_results.assert_called_once()
    assert retriever.last_retrieval_intent is not None
    assert retriever.last_retrieval_intent.dft_included_reason == "requested_section"


def test_explicit_evidence_types_are_an_authoritative_dispatch_allowlist():
    retriever = _mocked_retriever()

    retriever.retrieve(
        "Summarize DFT adsorption energy and mechanism",
        evidence_types=["dft_results"],
    )

    retriever._retrieve_dft_results.assert_called_once()
    for evidence_type, method_name in _RETRIEVAL_METHODS.items():
        if evidence_type != "dft_results":
            getattr(retriever, method_name).assert_not_called()
    assert retriever.last_retrieval_intent is not None
    assert retriever.last_retrieval_intent.selected_evidence_types == ("dft_results",)
    assert retriever.last_retrieval_intent.dft_included_reason == "explicit_evidence_types"


def test_comprehensive_mode_preserves_explicit_legacy_all_type_retrieval():
    retriever = _mocked_retriever()

    result = retriever.retrieve("legacy broad search", mode="all")

    for method_name in _RETRIEVAL_METHODS.values():
        getattr(retriever, method_name).assert_called_once()
    assert set(result) == set(SUPPORTED_EVIDENCE_TYPES)
    assert retriever.last_retrieval_intent is not None
    assert retriever.last_retrieval_intent.mode == "comprehensive"
    assert retriever.last_retrieval_intent.dft_included_reason == "comprehensive_mode"


def test_unknown_evidence_type_is_rejected_before_retrieval():
    retriever = _mocked_retriever()

    with pytest.raises(ValueError, match="Unsupported evidence_types"):
        retriever.retrieve("test", evidence_types=["not_a_real_type"])

    for method_name in _RETRIEVAL_METHODS.values():
        getattr(retriever, method_name).assert_not_called()
