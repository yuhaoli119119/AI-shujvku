from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.db.models import Paper
from app.rag.multi_paper_evidence_plan import MultiPaperEvidencePlanner
from app.rag.prompt_builder import PaperWriterPromptBuilder


class FakeRetriever:
    def __init__(self, provider):
        self.provider = provider
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return self.provider(**kwargs)


def _paper(index: int) -> Paper:
    return Paper(
        id=UUID(int=index + 1),
        paper_code=f"B{index + 1:04d}",
        doi=f"10.1000/{index + 1}",
        title=f"Paper {index + 1}",
        pdf_path=f"paper-{index + 1}.pdf",
    )


def _session_with_papers(papers: list[Paper]) -> MagicMock:
    session = MagicMock()
    session.scalars.return_value.all.return_value = papers
    return session


def _evidence(
    paper_id: UUID,
    index: int,
    *,
    evidence_type: str = "writing_cards",
    score: float = 0.9,
    review_status: str = "safe_verified",
    candidate_status: str | None = None,
    value: float | None = None,
    unit: str | None = None,
    property_name: str | None = None,
    rate: str | None = None,
) -> dict:
    item = {
        "paper_id": paper_id,
        "object_id": f"{paper_id}-{evidence_type}-{index}",
        "source_id": f"{paper_id}-{evidence_type}-{index}",
        "source_type": evidence_type.rstrip("s"),
        "type": evidence_type.rstrip("s"),
        "score": score,
        "score_breakdown": {
            "lexical": score,
            "semantic": 0.0,
            "hybrid": score,
        },
        "evidence_text": f"Evidence {index} from {paper_id}",
        "text": f"Evidence {index} from {paper_id}",
        "review_status": review_status,
        "candidate_status": candidate_status,
        "page": index + 1,
        "page_start": index + 1,
        "page_end": index + 1,
        "evidence_locator": {
            "page": index + 1,
            "locator_status": "exact_page",
        },
    }
    if value is not None:
        item["value"] = value
    if unit is not None:
        item["unit"] = unit
    if property_name is not None:
        if evidence_type == "dft_results":
            item["property_type"] = property_name
            item["adsorbate"] = "Li2S4"
            item["material_identity"] = {"name": f"Catalyst {paper_id}"}
        else:
            item["metric_name"] = property_name
    if rate is not None:
        item["rate"] = rate
    return item


def _typed_results(evidence_type: str, items: list[dict]) -> dict[str, list[dict]]:
    return {evidence_type: items}


def test_thirty_papers_are_split_into_three_bounded_batches_with_stable_fingerprint():
    papers = [_paper(index) for index in range(30)]
    session = _session_with_papers(papers)

    def provider(**kwargs):
        return _typed_results(
            "writing_cards",
            [_evidence(paper_id, 0) for paper_id in kwargs["paper_ids"]],
        )

    retriever = FakeRetriever(provider)
    planner = MultiPaperEvidencePlanner(session, retriever=retriever)
    kwargs = {
        "query": "Summarize the research problem, solution, mechanism, and key figures",
        "paper_ids": [str(paper.id) for paper in papers],
        "evidence_budget": 24,
        "batch_size": 10,
    }

    first = planner.plan(**kwargs)
    second = planner.plan(**kwargs)

    assert first["requires_batched_synthesis"] is True
    assert len(first["batches"]) == 3
    assert all(len(batch["paper_ids"]) <= 10 for batch in first["batches"])
    assert [batch["budget"]["allocated"] for batch in first["batches"]] == [8, 8, 8]
    assert first["budgets"]["used"] == 24
    assert len(first["selected_evidence"]) <= first["budgets"]["evidence_budget"]
    assert all(item["can_use_for_citation"] is True for item in first["selected_evidence"])
    assert first["coverage"]["omitted_counts"]["budget_exhausted"] == 6
    assert first["collection"]["retrieval_call_count"] == 3
    assert all(call["limit_per_type"] == 24 for call in retriever.calls)
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert [batch["batch_id"] for batch in first["batches"]] == [
        batch["batch_id"] for batch in second["batches"]
    ]


def test_fair_round_robin_and_per_paper_cap_prevent_monopoly():
    papers = [_paper(index) for index in range(3)]
    session = _session_with_papers(papers)

    def provider(**kwargs):
        results: dict[str, list[dict]] = {
            "writing_cards": [],
            "mechanism_claims": [],
            "figure_cards": [],
        }
        evidence_types = list(results)
        for paper_id in kwargs["paper_ids"]:
            for index in range(6):
                evidence_type = evidence_types[index % len(evidence_types)]
                results[evidence_type].append(
                    _evidence(
                        paper_id,
                        index,
                        evidence_type=evidence_type,
                        score=0.99 - index * 0.01,
                    )
                )
        return results

    retriever = FakeRetriever(provider)
    planner = MultiPaperEvidencePlanner(session, retriever=retriever)
    plan = planner.plan(
        query="Explain the mechanism and key figures",
        paper_ids=[str(paper.id) for paper in papers],
        evidence_budget=9,
        max_evidence_per_paper=2,
    )

    counts = Counter(item["source_paper_id"] for item in plan["selected_evidence"])
    assert counts == Counter({str(paper.id): 2 for paper in papers})
    assert plan["budgets"]["used"] == 6
    assert len({item["evidence_type"] for item in plan["selected_evidence"]}) >= 2
    assert all(
        len(group["source_paper_ids"]) <= plan["budgets"]["per_claim"]
        for group in plan["claim_evidence_matrix"]
    )


def test_semantic_duplicate_dft_does_not_create_false_selection_truncation():
    paper = _paper(0)
    session = _session_with_papers([paper])
    first = _evidence(
        paper.id,
        0,
        evidence_type="dft_results",
        value=-1.23,
        unit="eV",
        property_name="adsorption_energy",
    )
    duplicate = {
        **first,
        "object_id": f"{paper.id}-duplicate-object",
        "source_id": f"{paper.id}-duplicate-object",
    }

    plan = MultiPaperEvidencePlanner(
        session,
        retriever=FakeRetriever(
            lambda **kwargs: _typed_results("dft_results", [first, duplicate])
        ),
    ).plan(
        query="DFT adsorption energy",
        paper_ids=[str(paper.id)],
        evidence_budget=3,
    )

    assert len(plan["selected_evidence"]) == 1
    assert plan["coverage"]["unselected_relevant_evidence_count"] == 0
    assert plan["coverage"]["coverage_complete"] is True
    assert not any(
        warning["code"] == "selection_truncated" for warning in plan["warnings"]
    )
    coverage = plan["coverage"]["by_paper"][0]
    assert coverage["raw_relevant_candidate_count"] == 2
    assert coverage["relevant_candidate_count"] == 1


def test_unused_empty_batch_budget_is_redistributed_to_relevant_batch():
    papers = [_paper(index) for index in range(12)]
    session = _session_with_papers(papers)

    def provider(**kwargs):
        if papers[10].id not in kwargs["paper_ids"]:
            return {}
        return _typed_results(
            "writing_cards",
            [
                _evidence(paper_id, index)
                for paper_id in kwargs["paper_ids"]
                for index in range(3)
            ],
        )

    plan = MultiPaperEvidencePlanner(
        session,
        retriever=FakeRetriever(provider),
    ).plan(
        query="Summarize the mechanism",
        paper_ids=[str(paper.id) for paper in papers],
        evidence_budget=6,
        batch_size=10,
        max_evidence_per_paper=3,
    )

    assert plan["budgets"]["used"] == 6
    assert [batch["budget"]["allocated"] for batch in plan["batches"]] == [0, 6]
    assert plan["batches"][0]["selected_evidence_ids"] == []
    assert len(plan["batches"][1]["selected_evidence_ids"]) == 6
    second_batch_coverage = [
        item
        for item in plan["coverage"]["by_paper"]
        if item["paper_id"] in set(plan["batches"][1]["paper_ids"])
    ]
    assert all(item["status"] == "represented" for item in second_batch_coverage)


def test_coverage_distinguishes_observed_irrelevance_from_bounded_no_safe_result_and_budget():
    papers = [_paper(index) for index in range(4)]
    session = _session_with_papers(papers)

    def provider(**kwargs):
        return _typed_results(
            "writing_cards",
            [
                _evidence(kwargs["paper_ids"][0], 0, score=0.9),
                _evidence(kwargs["paper_ids"][1], 0, score=0.05),
                _evidence(kwargs["paper_ids"][3], 0, score=0.8),
            ],
        )

    plan = MultiPaperEvidencePlanner(session, retriever=FakeRetriever(provider)).plan(
        query="Summarize the mechanism",
        paper_ids=[str(paper.id) for paper in papers],
        evidence_budget=1,
    )
    coverage = {
        item["paper_id"]: item for item in plan["coverage"]["by_paper"]
    }

    assert coverage[str(papers[0].id)]["status"] == "represented"
    assert coverage[str(papers[1].id)]["status"] == "not_relevant"
    assert coverage[str(papers[1].id)]["classification_confidence"] == "medium"
    assert coverage[str(papers[2].id)]["status"] == "no_safe_evidence"
    assert coverage[str(papers[2].id)]["classification_confidence"] == "bounded_pool_only"
    assert coverage[str(papers[3].id)]["status"] == "budget_exhausted"


def test_narrative_excludes_dft_and_explicit_dft_obeys_cap_and_rejects_unsafe_rows():
    paper = _paper(0)
    session = _session_with_papers([paper])

    def provider(**kwargs):
        verified = [
            _evidence(
                paper.id,
                index,
                evidence_type="dft_results",
                value=-1.0 - index,
                unit="eV",
                property_name="adsorption_energy",
            )
            for index in range(5)
        ]
        unsafe = [
            _evidence(
                paper.id,
                10,
                evidence_type="dft_results",
                value=-9.9,
                unit="eV",
                property_name="adsorption_energy",
                review_status="rejected",
            ),
            _evidence(
                paper.id,
                11,
                evidence_type="dft_results",
                value=-8.8,
                unit="eV",
                property_name="adsorption_energy",
                review_status="candidate_unverified",
                candidate_status="candidate_unverified",
            ),
            _evidence(
                paper.id,
                12,
                evidence_type="dft_results",
                value=-7.7,
                unit="eV",
                property_name="adsorption_energy",
                review_status="blocked",
            ),
        ]
        return {
            "writing_cards": [_evidence(paper.id, 20)],
            "dft_results": [*verified, *unsafe],
        }

    planner = MultiPaperEvidencePlanner(session, retriever=FakeRetriever(provider))
    narrative = planner.plan(
        query="Summarize the research problem and mechanism",
        paper_ids=[str(paper.id)],
        max_evidence_per_paper=8,
    )
    assert narrative["dft_included"] is False
    assert narrative["budgets"]["dft_cap"] == 0
    assert all(item["evidence_type"] != "dft_results" for item in narrative["selected_evidence"])

    dft = planner.plan(
        query="Compare DFT adsorption energies and charge transfer",
        paper_ids=[str(paper.id)],
        evidence_budget=8,
        max_evidence_per_paper=8,
    )
    selected_dft = [
        item for item in dft["selected_evidence"] if item["evidence_type"] == "dft_results"
    ]
    assert dft["budgets"]["dft_cap"] == 3
    assert len(selected_dft) == 3
    assert all(item["review_status"] == "safe_verified" for item in selected_dft)
    assert all(item["property"] == "adsorption_energy" for item in selected_dft)
    assert all(item["unit"] == "eV" for item in selected_dft)
    assert all(item["context"]["evidence_class"] == "computational" for item in selected_dft)
    assert all(
        {
            "source_paper_id",
            "paper_code",
            "doi",
            "evidence_type",
            "object_id",
            "evidence_locator",
            "review_status",
            "gate_status",
            "excerpt",
            "property",
            "unit",
            "context",
        }.issubset(item)
        for item in selected_dft
    )

    elevated = planner.plan(
        query="Summarize this computational study",
        paper_ids=[str(paper.id)],
        requested_sections=["dft_results"],
        evidence_budget=8,
        max_evidence_per_paper=8,
    )
    assert elevated["budgets"]["dft_cap"] == 8
    assert sum(
        item["evidence_type"] == "dft_results"
        for item in elevated["selected_evidence"]
    ) == 5


def test_incompatible_numeric_contexts_are_split_and_never_auto_compared():
    papers = [_paper(index) for index in range(3)]
    session = _session_with_papers(papers)

    def provider(**kwargs):
        return _typed_results(
            "electrochemical_performance",
            [
                _evidence(
                    papers[0].id,
                    0,
                    evidence_type="electrochemical_performance",
                    value=900,
                    unit="mAh/g",
                    property_name="capacity",
                    rate="0.5C",
                ),
                _evidence(
                    papers[1].id,
                    0,
                    evidence_type="electrochemical_performance",
                    value=1.0,
                    unit="Ah/g",
                    property_name="capacity",
                    rate="0.5C",
                ),
                _evidence(
                    papers[2].id,
                    0,
                    evidence_type="electrochemical_performance",
                    value=950,
                    unit="mAh/g",
                    property_name="capacity",
                    rate="0.5C",
                ),
            ],
        )

    plan = MultiPaperEvidencePlanner(session, retriever=FakeRetriever(provider)).plan(
        query="Compare electrochemical capacity",
        paper_ids=[str(paper.id) for paper in papers],
        mode="comparison",
    )

    assert any(
        warning["code"] == "comparison_incompatible_contexts"
        for warning in plan["warnings"]
    )
    assert all(group["automatic_conclusion_allowed"] is False for group in plan["evidence_groups"])
    assert not any(
        str(papers[0].id) in group["source_paper_ids"]
        and str(papers[1].id) in group["source_paper_ids"]
        for group in plan["evidence_groups"]
    )
    compatible = [
        group for group in plan["evidence_groups"] if group["comparison_allowed"]
    ]
    assert len(compatible) == 1
    assert compatible[0]["source_paper_ids"] == [str(papers[0].id), str(papers[2].id)]


def test_duplicate_unknown_ids_and_invalid_limits_are_deterministic():
    paper = _paper(0)
    unknown = UUID(int=999)
    session = _session_with_papers([paper])
    empty_retriever = FakeRetriever(lambda **kwargs: {})
    planner = MultiPaperEvidencePlanner(session, retriever=empty_retriever)

    plan = planner.plan(
        query="summary",
        paper_ids=[str(paper.id), str(paper.id), str(unknown)],
    )
    assert plan["requested_paper_count"] == 3
    assert plan["paper_scope"]["unique_requested_paper_count"] == 2
    assert plan["paper_scope"]["duplicate_paper_ids"] == [str(paper.id)]
    assert plan["paper_scope"]["unknown_paper_ids"] == [str(unknown)]
    assert plan["coverage"]["by_paper"][-1]["status"] == "not_found"

    with pytest.raises(ValueError, match="Invalid paper_ids"):
        planner.plan(query="summary", paper_ids=["not-a-uuid"])
    with pytest.raises(ValueError, match="evidence_budget"):
        planner.plan(query="summary", paper_ids=[str(paper.id)], evidence_budget=49)
    with pytest.raises(ValueError, match="batch_size"):
        planner.plan(query="summary", paper_ids=[str(paper.id)], batch_size=11)
    with pytest.raises(ValueError, match="max_sources_per_claim"):
        planner.plan(
            query="summary",
            paper_ids=[str(paper.id)],
            max_sources_per_claim=2,
        )


def test_batch_prompt_context_contains_only_its_evidence_and_incomplete_coverage_constraint():
    papers = [_paper(index) for index in range(30)]
    session = _session_with_papers(papers)

    def provider(**kwargs):
        return _typed_results(
            "writing_cards",
            [_evidence(paper_id, 0) for paper_id in kwargs["paper_ids"]],
        )

    plan = MultiPaperEvidencePlanner(session, retriever=FakeRetriever(provider)).plan(
        query="Summarize mechanism and figures",
        paper_ids=[str(paper.id) for paper in papers],
        evidence_budget=24,
    )
    first_batch = plan["batches"][0]
    second_batch_ids = set(plan["batches"][1]["selected_evidence_ids"])
    context = PaperWriterPromptBuilder().build_batch_prompt_context(
        plan,
        first_batch["batch_id"],
    )

    assert context["full_text_included"] is False
    assert context["database_writes"] is False
    assert {item["evidence_id"] for item in context["evidence_cards"]} == set(
        first_batch["selected_evidence_ids"]
    )
    assert not ({item["evidence_id"] for item in context["evidence_cards"]} & second_batch_ids)
    assert all(
        item["source_paper_id"] in set(first_batch["paper_ids"])
        for item in context["evidence_cards"]
    )
    assert any(
        item["code"] == "no_comprehensive_coverage_claim"
        for item in context["constraints"]
    )
