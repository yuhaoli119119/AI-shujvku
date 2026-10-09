from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper
from app.rag.retrieval_intent import RetrievalIntent, route_retrieval_intent
from app.rag.retriever import Retriever


SCHEMA_VERSION = "multi_paper_evidence_plan.v1"

DEFAULT_EVIDENCE_BUDGET = 24
MIN_EVIDENCE_BUDGET = 1
MAX_EVIDENCE_BUDGET = 48
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 10
DEFAULT_MAX_EVIDENCE_PER_PAPER = 3
MAX_EVIDENCE_PER_PAPER = 8
DEFAULT_MAX_SOURCES_PER_CLAIM = 5
MIN_SOURCES_PER_CLAIM = 3
MAX_SOURCES_PER_CLAIM = 5
MAX_EVIDENCE_PER_THEME = 8
DEFAULT_CANDIDATE_POOL_PER_TYPE = 24
MAX_CANDIDATE_POOL_PER_TYPE = 48
DEFAULT_DFT_CAP = 3
ELEVATED_DFT_CAP = 8
MIN_RELEVANCE_SCORE = 0.08
MAX_PAPERS = 200
MAX_EXCERPT_CHARS = 1200


_TYPE_ORDER = {
    evidence_type: index
    for index, evidence_type in enumerate(
        (
            "writing_cards",
            "mechanism_claims",
            "figure_cards",
            "sections",
            "catalyst_samples",
            "electrochemical_performance",
            "dft_results",
            "figure_data_points",
        )
    )
}
_UNSAFE_STATUS_MARKERS = {
    "blocked",
    "candidate",
    "candidate_unverified",
    "discovery_candidate",
    "needs_repair",
    "needs_review",
    "rejected",
    "unsafe",
    "unverified",
}


class MultiPaperEvidencePlanner:
    """Read-only deterministic evidence planner for bounded multi-paper synthesis."""

    def __init__(self, session: Session, retriever: Retriever | None = None) -> None:
        self.session = session
        self.retriever = retriever or Retriever(session)

    def plan(
        self,
        *,
        query: str,
        paper_ids: list[str | UUID],
        evidence_types: list[str] | None = None,
        mode: str | None = None,
        requested_sections: list[str] | None = None,
        evidence_budget: int = DEFAULT_EVIDENCE_BUDGET,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_evidence_per_paper: int = DEFAULT_MAX_EVIDENCE_PER_PAPER,
        max_sources_per_claim: int = DEFAULT_MAX_SOURCES_PER_CLAIM,
        candidate_pool_per_type: int = DEFAULT_CANDIDATE_POOL_PER_TYPE,
    ) -> dict[str, Any]:
        limits = _validate_limits(
            evidence_budget=evidence_budget,
            batch_size=batch_size,
            max_evidence_per_paper=max_evidence_per_paper,
            max_sources_per_claim=max_sources_per_claim,
            candidate_pool_per_type=candidate_pool_per_type,
        )
        scope = self._resolve_paper_scope(paper_ids)
        retrieval_intent = route_retrieval_intent(
            query,
            evidence_types=evidence_types,
            mode=mode,
            requested_sections=requested_sections,
        )
        dft_cap = _resolve_dft_cap(
            retrieval_intent,
            mode=mode,
            requested_sections=requested_sections,
            evidence_budget=limits["evidence_budget"],
        )

        paper_batches = _chunked(scope["valid_papers"], limits["batch_size"])
        candidate_state = {
            paper["paper_id"]: {
                "safe_candidate_count": 0,
                "raw_relevant_candidate_count": 0,
                "relevant_candidate_count": 0,
            }
            for paper in scope["valid_papers"]
        }
        candidates_by_key: dict[str, dict[str, Any]] = {}
        retrieval_calls = 0

        for batch in paper_batches:
            retrieval_calls += 1
            retrieved = self.retriever.retrieve(
                query=query,
                paper_ids=[UUID(paper["paper_id"]) for paper in batch],
                limit_per_type=limits["candidate_pool_per_type"],
                evidence_types=evidence_types,
                mode=mode,
                requested_sections=requested_sections,
            )
            batch_papers = {paper["paper_id"]: paper for paper in batch}
            for evidence_type in retrieval_intent.selected_evidence_types:
                for item in retrieved.get(evidence_type, []):
                    paper_id = str(item.get("paper_id") or "")
                    paper = batch_papers.get(paper_id)
                    if paper is None:
                        continue
                    normalized = _normalize_safe_candidate(evidence_type, item, paper)
                    if normalized is None:
                        continue
                    candidate_state[paper_id]["safe_candidate_count"] += 1
                    if not _is_relevant(normalized):
                        continue
                    candidate_state[paper_id]["raw_relevant_candidate_count"] += 1
                    key = normalized.pop("_dedup_key")
                    existing = candidates_by_key.get(key)
                    if existing is None or _candidate_sort_key(normalized) < _candidate_sort_key(existing):
                        candidates_by_key[key] = normalized

        candidates_by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates_by_key.values():
            candidates_by_paper[candidate["source_paper_id"]].append(candidate)
            candidate_state[candidate["source_paper_id"]]["relevant_candidate_count"] += 1
        for items in candidates_by_paper.values():
            items.sort(key=_candidate_sort_key)

        batch_allocations = _allocate_batch_budgets(
            paper_batches,
            evidence_budget=limits["evidence_budget"],
            max_evidence_per_paper=limits["max_evidence_per_paper"],
        )
        selected, final_batch_allocations = _fair_select(
            paper_batches=paper_batches,
            batch_allocations=batch_allocations,
            candidates_by_paper=candidates_by_paper,
            evidence_budget=limits["evidence_budget"],
            max_evidence_per_paper=limits["max_evidence_per_paper"],
            dft_cap=dft_cap,
        )
        selected_ids = {item["evidence_id"] for item in selected}
        selected_counts = Counter(item["source_paper_id"] for item in selected)
        relevant_total = len(candidates_by_key)
        unselected_relevant_count = max(0, relevant_total - len(selected_ids))

        coverage_by_paper = _build_coverage(
            scope=scope,
            candidate_state=candidate_state,
            selected_counts=selected_counts,
        )
        omitted_counts = Counter(
            row["status"] for row in coverage_by_paper if row["status"] != "represented"
        )
        coverage_complete = bool(scope["valid_papers"]) and not scope["unknown_paper_ids"] and all(
            row["status"] == "represented" for row in coverage_by_paper
        ) and unselected_relevant_count == 0

        comparison_groups, comparison_warnings = _build_evidence_groups(
            selected,
            max_sources_per_claim=limits["max_sources_per_claim"],
            comparison_mode=retrieval_intent.intent == "comparison",
        )
        warnings = _build_warnings(
            retrieval_intent=retrieval_intent,
            scope=scope,
            coverage_complete=coverage_complete,
            omitted_counts=omitted_counts,
            unselected_relevant_count=unselected_relevant_count,
            selected=selected,
            dft_cap=dft_cap,
            requires_batched_synthesis=len(paper_batches) > 1,
        )
        warnings.extend(comparison_warnings)

        batches = _build_batches(
            paper_batches=paper_batches,
            initial_batch_allocations=batch_allocations,
            batch_allocations=final_batch_allocations,
            selected=selected,
            coverage_by_paper=coverage_by_paper,
            evidence_budget=limits["evidence_budget"],
        )
        fingerprint = _plan_fingerprint(
            query=query,
            scope=scope,
            retrieval_intent=retrieval_intent,
            requested_sections=requested_sections,
            limits=limits,
            dft_cap=dft_cap,
            selected=selected,
        )

        return {
            "schema_version": SCHEMA_VERSION,
            "plan_id": f"mpep_{fingerprint[:24]}",
            "plan_fingerprint": fingerprint,
            "query": query,
            **retrieval_intent.as_dict(),
            "paper_scope": {
                "requested_paper_count": scope["requested_paper_count"],
                "unique_requested_paper_count": len(scope["unique_paper_ids"]),
                "valid_paper_count": len(scope["valid_papers"]),
                "represented_paper_count": sum(
                    1 for row in coverage_by_paper if row["status"] == "represented"
                ),
                "unknown_paper_ids": scope["unknown_paper_ids"],
                "duplicate_paper_ids": scope["duplicate_paper_ids"],
                "valid_papers": scope["valid_papers"],
            },
            "requested_paper_count": scope["requested_paper_count"],
            "valid_paper_count": len(scope["valid_papers"]),
            "represented_paper_count": sum(
                1 for row in coverage_by_paper if row["status"] == "represented"
            ),
            "requires_batched_synthesis": len(paper_batches) > 1,
            "batch_size": limits["batch_size"],
            "batches": batches,
            "budgets": {
                "evidence_budget": limits["evidence_budget"],
                "used": len(selected),
                "remaining": max(0, limits["evidence_budget"] - len(selected)),
                "per_paper": limits["max_evidence_per_paper"],
                "per_claim": limits["max_sources_per_claim"],
                "per_theme": MAX_EVIDENCE_PER_THEME,
                "dft_cap": dft_cap,
                "dft_used": sum(1 for item in selected if item["evidence_type"] == "dft_results"),
                "candidate_pool_per_type": limits["candidate_pool_per_type"],
            },
            "selected_evidence": selected,
            "claim_evidence_matrix": comparison_groups,
            "evidence_groups": comparison_groups,
            "coverage": {
                "coverage_complete": coverage_complete,
                "classification_scope": "bounded_safe_candidate_pool",
                "by_paper": coverage_by_paper,
                "omitted_counts": dict(sorted(omitted_counts.items())),
                "unselected_relevant_evidence_count": unselected_relevant_count,
            },
            "warnings": warnings,
            "collection": {
                "strategy": "bounded_batch_retrieval",
                "retrieval_call_count": retrieval_calls,
                "candidate_pool_per_type": limits["candidate_pool_per_type"],
                "minimum_relevance_score": MIN_RELEVANCE_SCORE,
                "full_context_used": False,
            },
            "database_writes": False,
            "read_only": {
                "database_writes": False,
                "persistence": "none",
                "full_text_context_loaded": False,
            },
        }

    def _resolve_paper_scope(self, paper_ids: Iterable[str | UUID]) -> dict[str, Any]:
        requested = list(paper_ids or [])
        if not requested:
            raise ValueError("paper_ids must contain at least one paper UUID")
        if len(requested) > MAX_PAPERS:
            raise ValueError(f"paper_ids may contain at most {MAX_PAPERS} entries")

        unique: list[str] = []
        duplicate: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for raw in requested:
            try:
                normalized = str(UUID(str(raw)))
            except (TypeError, ValueError, AttributeError):
                invalid.append(str(raw))
                continue
            if normalized in seen:
                duplicate.append(normalized)
                continue
            seen.add(normalized)
            unique.append(normalized)
        if invalid:
            raise ValueError(f"Invalid paper_ids: {invalid}")

        rows = list(
            self.session.scalars(
                select(Paper).where(Paper.id.in_([UUID(item) for item in unique]))
            ).all()
        )
        by_id = {str(row.id): row for row in rows}
        valid_papers = [
            {
                "paper_id": paper_id,
                "paper_code": by_id[paper_id].paper_code,
                "doi": by_id[paper_id].doi,
                "title": by_id[paper_id].title,
            }
            for paper_id in unique
            if paper_id in by_id
        ]
        return {
            "requested_paper_count": len(requested),
            "unique_paper_ids": unique,
            "duplicate_paper_ids": duplicate,
            "unknown_paper_ids": [paper_id for paper_id in unique if paper_id not in by_id],
            "valid_papers": valid_papers,
        }


def _validate_limits(
    *,
    evidence_budget: int,
    batch_size: int,
    max_evidence_per_paper: int,
    max_sources_per_claim: int,
    candidate_pool_per_type: int,
) -> dict[str, int]:
    values = {
        "evidence_budget": _bounded_int(
            "evidence_budget", evidence_budget, MIN_EVIDENCE_BUDGET, MAX_EVIDENCE_BUDGET
        ),
        "batch_size": _bounded_int("batch_size", batch_size, 1, MAX_BATCH_SIZE),
        "max_evidence_per_paper": _bounded_int(
            "max_evidence_per_paper", max_evidence_per_paper, 1, MAX_EVIDENCE_PER_PAPER
        ),
        "max_sources_per_claim": _bounded_int(
            "max_sources_per_claim",
            max_sources_per_claim,
            MIN_SOURCES_PER_CLAIM,
            MAX_SOURCES_PER_CLAIM,
        ),
        "candidate_pool_per_type": _bounded_int(
            "candidate_pool_per_type",
            candidate_pool_per_type,
            1,
            MAX_CANDIDATE_POOL_PER_TYPE,
        ),
    }
    return values


def _bounded_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _resolve_dft_cap(
    intent: RetrievalIntent,
    *,
    mode: str | None,
    requested_sections: list[str] | None,
    evidence_budget: int,
) -> int:
    if not intent.dft_included:
        return 0
    normalized_mode = str(mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    normalized_sections = {
        str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
        for item in (requested_sections or [])
    }
    cap = (
        ELEVATED_DFT_CAP
        if normalized_mode == "dft_quantitative" or "dft_results" in normalized_sections
        else DEFAULT_DFT_CAP
    )
    return min(cap, evidence_budget)


def _chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _normalize_safe_candidate(
    evidence_type: str,
    item: dict[str, Any],
    paper: dict[str, Any],
) -> dict[str, Any] | None:
    raw_can_write = item.get("can_use_for_writing")
    raw_can_cite = item.get("can_use_for_citation")
    if raw_can_write is None and raw_can_cite is None:
        can_use_for_writing = True
        can_use_for_citation = True
    elif raw_can_write is None:
        can_use_for_citation = raw_can_cite is True
        can_use_for_writing = can_use_for_citation
    elif raw_can_cite is None:
        can_use_for_writing = raw_can_write is True
        can_use_for_citation = can_use_for_writing
    else:
        can_use_for_writing = raw_can_write is True
        can_use_for_citation = raw_can_cite is True
    if not can_use_for_writing and not can_use_for_citation:
        return None
    review_status = str(
        item.get("review_status")
        or item.get("review_gate_status")
        or item.get("provenance_level")
        or item.get("retrieval_tier")
        or ""
    ).strip()
    gate_status = str(
        item.get("review_gate_status")
        or item.get("provenance_level")
        or item.get("retrieval_tier")
        or review_status
    ).strip()
    status_text = " ".join(
        str(value or "").strip().lower()
        for value in (
            item.get("candidate_status"),
            review_status,
            gate_status,
            item.get("retrieval_tier"),
        )
    )
    if not gate_status or any(marker in status_text for marker in _UNSAFE_STATUS_MARKERS):
        return None

    object_id = str(item.get("object_id") or item.get("source_id") or "").strip()
    excerpt = str(item.get("evidence_text") or item.get("text") or "").strip()
    if not object_id or not excerpt:
        return None

    locator = _normalized_locator(item)
    if not _locator_is_usable(locator):
        return None

    score_breakdown = item.get("score_breakdown") if isinstance(item.get("score_breakdown"), dict) else {}
    score = _as_float(score_breakdown.get("hybrid"), default=_as_float(item.get("score")))
    property_name = (
        item.get("property_type")
        or item.get("metric_name")
        or ("capacity" if item.get("capacity_value") is not None else None)
    )
    value = item.get("value")
    unit = item.get("unit")
    if value is None and item.get("capacity_value") is not None:
        value = item.get("capacity_value")
        unit = unit or "mAh/g"
    context = _numeric_context(evidence_type, item)
    source_paper_id = paper["paper_id"]
    evidence_id = f"{evidence_type}:{source_paper_id}:{object_id}"
    normalized = {
        "evidence_id": evidence_id,
        "source_paper_id": source_paper_id,
        "paper_id": source_paper_id,
        "paper_code": paper.get("paper_code"),
        "doi": paper.get("doi"),
        "evidence_type": evidence_type,
        "source_type": item.get("source_type") or item.get("type") or evidence_type,
        "object_id": object_id,
        "source_id": str(item.get("source_id") or object_id),
        "page": _first_not_none(item.get("page"), locator.get("page"), item.get("page_start")),
        "page_start": _first_not_none(item.get("page_start"), locator.get("page_start"), locator.get("page")),
        "page_end": _first_not_none(item.get("page_end"), locator.get("page_end"), locator.get("page")),
        "evidence_locator": locator,
        "review_status": review_status,
        "gate_status": gate_status,
        "can_use_for_writing": can_use_for_writing,
        "can_use_for_citation": can_use_for_citation,
        "score": round(score, 6),
        "score_breakdown": {
            "lexical": round(_as_float(score_breakdown.get("lexical")), 6),
            "semantic": round(_as_float(score_breakdown.get("semantic")), 6),
            "hybrid": round(score, 6),
        },
        "excerpt": excerpt[:MAX_EXCERPT_CHARS],
        "property": property_name,
        "value": value,
        "unit": unit,
        "context": context,
    }
    normalized["_dedup_key"] = _dedup_key(normalized, item)
    return normalized


def _normalized_locator(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("evidence_locator") if isinstance(item.get("evidence_locator"), dict) else {}
    locator = _json_safe(dict(raw))
    locator.setdefault("page", _first_not_none(item.get("page"), item.get("page_start")))
    locator.setdefault("page_start", item.get("page_start"))
    locator.setdefault("page_end", item.get("page_end"))
    if item.get("section_title") and not locator.get("section"):
        locator["section"] = item.get("section_title")
    if item.get("source_figure") and not locator.get("figure"):
        locator["figure"] = item.get("source_figure")
    locator["locator_status"] = (
        locator.get("locator_status")
        or item.get("locator_status")
        or ("exact_page" if locator.get("page") is not None else None)
    )
    return {key: value for key, value in locator.items() if value is not None}


def _locator_is_usable(locator: dict[str, Any]) -> bool:
    return any(
        locator.get(key) is not None
        for key in ("page", "page_start", "page_end", "bbox", "figure_id")
    )


def _is_relevant(candidate: dict[str, Any]) -> bool:
    breakdown = candidate.get("score_breakdown") or {}
    score = _as_float(candidate.get("score"))
    lexical = _as_float(breakdown.get("lexical"))
    semantic = _as_float(breakdown.get("semantic"))
    signal = max(lexical, semantic)
    if signal <= 0 and score > 0:
        signal = score
    return score >= MIN_RELEVANCE_SCORE and signal >= MIN_RELEVANCE_SCORE


def _candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -_as_float(item.get("score")),
        _TYPE_ORDER.get(str(item.get("evidence_type")), 999),
        str(item.get("evidence_id") or ""),
    )


def _dedup_key(candidate: dict[str, Any], item: dict[str, Any]) -> str:
    if candidate["evidence_type"] != "dft_results":
        return candidate["evidence_id"]
    material = item.get("material_identity") if isinstance(item.get("material_identity"), dict) else {}
    payload = {
        "paper_id": candidate["source_paper_id"],
        "material": {
            "catalyst_sample_id": material.get("catalyst_sample_id"),
            "name": material.get("name"),
            "coordination": material.get("coordination"),
            "support": material.get("support"),
        },
        "property": candidate.get("property"),
        "adsorbate": item.get("adsorbate"),
        "reaction": item.get("reaction_step"),
        "value": candidate.get("value"),
        "unit": candidate.get("unit"),
        "locator": candidate.get("evidence_locator"),
    }
    return "dft_semantic:" + _stable_hash(payload)


def _numeric_context(evidence_type: str, item: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {
        "evidence_class": (
            "computational"
            if evidence_type == "dft_results"
            else "experimental"
            if evidence_type in {"electrochemical_performance", "figure_data_points"}
            else "descriptive"
        ),
    }
    for key in (
        "adsorbate",
        "reaction_step",
        "energy_type",
        "conditions",
        "rate",
        "cycle_number",
        "sulfur_loading_mg_cm2",
        "sample_label",
        "material_identity",
    ):
        value = item.get(key)
        if value is not None and value != "":
            context[key] = _json_safe(value)
    return context


def _allocate_batch_budgets(
    paper_batches: list[list[dict[str, Any]]],
    *,
    evidence_budget: int,
    max_evidence_per_paper: int,
) -> list[int]:
    allocations = [0 for _ in paper_batches]
    capacities = [len(batch) * max_evidence_per_paper for batch in paper_batches]
    remaining = evidence_budget
    while remaining > 0:
        progressed = False
        for index, capacity in enumerate(capacities):
            if remaining <= 0:
                break
            if allocations[index] >= capacity:
                continue
            allocations[index] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break
    return allocations


def _fair_select(
    *,
    paper_batches: list[list[dict[str, Any]]],
    batch_allocations: list[int],
    candidates_by_paper: dict[str, list[dict[str, Any]]],
    evidence_budget: int,
    max_evidence_per_paper: int,
    dft_cap: int,
) -> tuple[list[dict[str, Any]], list[int]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    per_paper: Counter[str] = Counter()
    per_type: Counter[str] = Counter()
    batch_used = [0 for _ in paper_batches]
    batch_capacities = [len(batch) * max_evidence_per_paper for batch in paper_batches]
    dft_used = 0
    max_batch_length = max((len(batch) for batch in paper_batches), default=0)
    redistribution_enabled = False

    while len(selected) < evidence_budget:
        progressed = False
        for paper_offset in range(max_batch_length):
            for batch_index, batch in enumerate(paper_batches):
                if len(selected) >= evidence_budget:
                    break
                batch_limit = (
                    batch_capacities[batch_index]
                    if redistribution_enabled
                    else batch_allocations[batch_index]
                )
                if batch_used[batch_index] >= batch_limit:
                    continue
                if paper_offset >= len(batch):
                    continue
                paper_id = batch[paper_offset]["paper_id"]
                if per_paper[paper_id] >= max_evidence_per_paper:
                    continue
                available = [
                    item
                    for item in candidates_by_paper.get(paper_id, [])
                    if item["evidence_id"] not in selected_ids
                    and (
                        item["evidence_type"] != "dft_results"
                        or dft_used < dft_cap
                    )
                ]
                if not available:
                    continue
                choice = min(
                    available,
                    key=lambda item: (
                        per_type[item["evidence_type"]],
                        *_candidate_sort_key(item),
                    ),
                )
                selected.append(choice)
                selected_ids.add(choice["evidence_id"])
                per_paper[paper_id] += 1
                per_type[choice["evidence_type"]] += 1
                batch_used[batch_index] += 1
                if choice["evidence_type"] == "dft_results":
                    dft_used += 1
                progressed = True
        if not progressed:
            if not redistribution_enabled and len(selected) < evidence_budget:
                redistribution_enabled = True
                continue
            break
    return selected, batch_used


def _build_coverage(
    *,
    scope: dict[str, Any],
    candidate_state: dict[str, dict[str, int]],
    selected_counts: Counter[str],
) -> list[dict[str, Any]]:
    coverage: list[dict[str, Any]] = []
    for paper in scope["valid_papers"]:
        paper_id = paper["paper_id"]
        state = candidate_state[paper_id]
        selected_count = selected_counts[paper_id]
        if selected_count:
            status = "represented"
            confidence = "high"
            rationale = "At least one relevant safety-gated evidence item was selected."
        elif state["relevant_candidate_count"]:
            status = "budget_exhausted"
            confidence = "high"
            rationale = "Relevant safety-gated evidence was observed but selection limits prevented inclusion."
        elif state["safe_candidate_count"]:
            status = "not_relevant"
            confidence = "medium"
            rationale = "Safety-gated candidates were observed but did not meet the relevance threshold."
        else:
            status = "no_safe_evidence"
            confidence = "bounded_pool_only"
            rationale = (
                "No safety-gated candidate was observed in the bounded batch candidate pool; "
                "this is not a claim that the database contains no safe evidence."
            )
        coverage.append(
            {
                **paper,
                "status": status,
                "classification_confidence": confidence,
                "classification_rationale": rationale,
                "safe_candidate_count": state["safe_candidate_count"],
                "relevant_candidate_count": state["relevant_candidate_count"],
                "raw_relevant_candidate_count": state["raw_relevant_candidate_count"],
                "selected_evidence_count": selected_count,
            }
        )
    for paper_id in scope["unknown_paper_ids"]:
        coverage.append(
            {
                "paper_id": paper_id,
                "paper_code": None,
                "doi": None,
                "title": None,
                "status": "not_found",
                "classification_confidence": "high",
                "classification_rationale": "No Paper row matched the requested UUID.",
                "safe_candidate_count": 0,
                "relevant_candidate_count": 0,
                "raw_relevant_candidate_count": 0,
                "selected_evidence_count": 0,
            }
        )
    return coverage


def _build_evidence_groups(
    selected: list[dict[str, Any]],
    *,
    max_sources_per_claim: int,
    comparison_mode: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if comparison_mode:
        numeric_families: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for item in selected:
            family = _comparison_family(item)
            if family is None:
                grouped[_theme_key(item)].append(item)
                continue
            compatibility_key = _comparison_compatibility_key(item)
            numeric_families[family][compatibility_key].append(item)
        for family, partitions in sorted(numeric_families.items()):
            if len(partitions) > 1:
                warnings.append(
                    {
                        "code": "comparison_incompatible_contexts",
                        "message": (
                            f"Evidence for {family} has incompatible units or contexts and was split "
                            "into separate comparison groups; no automatic high/low conclusion is allowed."
                        ),
                        "details": {"family": family, "partition_count": len(partitions)},
                    }
                )
            for compatibility_key, items in sorted(partitions.items()):
                grouped[f"{family}|{compatibility_key}"].extend(items)
    else:
        for item in selected:
            grouped[_theme_key(item)].append(item)

    matrix: list[dict[str, Any]] = []
    for theme, items in sorted(grouped.items()):
        ordered = sorted(items, key=_candidate_sort_key)
        for chunk_index in range(0, len(ordered), max_sources_per_claim):
            chunk = ordered[chunk_index:chunk_index + max_sources_per_claim]
            theme_slot = chunk_index // max_sources_per_claim + 1
            source_ids = list(dict.fromkeys(item["source_paper_id"] for item in chunk))
            numeric = all(item.get("value") is not None and item.get("unit") for item in chunk)
            comparable = bool(
                comparison_mode
                and numeric
                and len(source_ids) >= 2
                and len({_comparison_compatibility_key(item) for item in chunk}) == 1
            )
            group_key = {
                "theme": theme,
                "chunk": theme_slot,
                "evidence_ids": [item["evidence_id"] for item in chunk],
            }
            matrix.append(
                {
                    "group_id": f"group_{_stable_hash(group_key)[:16]}",
                    "theme": f"{theme}#{theme_slot}",
                    "theme_family": theme,
                    "evidence_ids": group_key["evidence_ids"],
                    "source_paper_ids": source_ids,
                    "source_count": len(source_ids),
                    "comparison_allowed": comparable,
                    "automatic_conclusion_allowed": False,
                    "comparison_basis": (
                        {
                            "property": chunk[0].get("property"),
                            "unit": chunk[0].get("unit"),
                            "context": chunk[0].get("context"),
                        }
                        if comparable
                        else None
                    ),
                    "planning_instruction": (
                        "Draft only an attributed synthesis supported by these evidence IDs; "
                        "do not invent a conclusion or detach numeric values from their source objects."
                    ),
                }
            )
    return matrix, warnings


def _comparison_family(item: dict[str, Any]) -> str | None:
    if item.get("value") is None or not item.get("property"):
        return None
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    subject = context.get("adsorbate") or context.get("sample_label") or ""
    return ":".join(
        filter(
            None,
            [
                str(item.get("evidence_type") or ""),
                _normalized_text(item.get("property")),
                _normalized_text(subject),
            ],
        )
    )


def _comparison_compatibility_key(item: dict[str, Any]) -> str:
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    relevant_context = {
        key: context.get(key)
        for key in (
            "evidence_class",
            "reaction_step",
            "energy_type",
            "conditions",
            "rate",
            "cycle_number",
            "sulfur_loading_mg_cm2",
        )
        if context.get(key) is not None
    }
    return _stable_hash(
        {
            "unit": _normalized_text(item.get("unit")),
            "context": relevant_context,
        }
    )[:16]


def _theme_key(item: dict[str, Any]) -> str:
    property_name = _normalized_text(item.get("property"))
    return ":".join(
        filter(
            None,
            [
                str(item.get("evidence_type") or "evidence"),
                property_name,
            ],
        )
    )


def _build_warnings(
    *,
    retrieval_intent: RetrievalIntent,
    scope: dict[str, Any],
    coverage_complete: bool,
    omitted_counts: Counter[str],
    unselected_relevant_count: int,
    selected: list[dict[str, Any]],
    dft_cap: int,
    requires_batched_synthesis: bool,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if requires_batched_synthesis:
        warnings.append(
            {
                "code": "batched_synthesis_required",
                "message": "Process one batch context at a time; do not place all papers or evidence into one prompt.",
            }
        )
    if not coverage_complete:
        warnings.append(
            {
                "code": "coverage_incomplete",
                "message": (
                    "Coverage is incomplete. The synthesis must not claim systematic, comprehensive, "
                    "or exhaustive coverage."
                ),
                "details": {"omitted_counts": dict(sorted(omitted_counts.items()))},
            }
        )
    if scope["unknown_paper_ids"]:
        warnings.append(
            {
                "code": "unknown_papers",
                "message": "Some requested paper UUIDs were not found.",
                "details": {"paper_ids": scope["unknown_paper_ids"]},
            }
        )
    if scope["duplicate_paper_ids"]:
        warnings.append(
            {
                "code": "duplicate_papers_removed",
                "message": "Duplicate paper UUIDs were removed while preserving first occurrence order.",
                "details": {"paper_ids": scope["duplicate_paper_ids"]},
            }
        )
    if unselected_relevant_count:
        warnings.append(
            {
                "code": "selection_truncated",
                "message": "Relevant safety-gated evidence remained outside the plan due to bounded selection limits.",
                "details": {"unselected_relevant_evidence_count": unselected_relevant_count},
            }
        )
    if not retrieval_intent.dft_included:
        warnings.append(
            {
                "code": "dft_not_enabled",
                "message": "DFT evidence was not enabled by the retrieval intent and must not be introduced.",
            }
        )
    else:
        dft_used = sum(1 for item in selected if item["evidence_type"] == "dft_results")
        if dft_cap and dft_used >= dft_cap:
            warnings.append(
                {
                    "code": "dft_cap_reached",
                    "message": "The selected DFT evidence reached the deterministic DFT cap.",
                    "details": {"dft_cap": dft_cap},
                }
            )
    return warnings


def _build_batches(
    *,
    paper_batches: list[list[dict[str, Any]]],
    initial_batch_allocations: list[int],
    batch_allocations: list[int],
    selected: list[dict[str, Any]],
    coverage_by_paper: list[dict[str, Any]],
    evidence_budget: int,
) -> list[dict[str, Any]]:
    coverage_map = {row["paper_id"]: row for row in coverage_by_paper}
    batches: list[dict[str, Any]] = []
    for index, papers in enumerate(paper_batches):
        paper_ids = [paper["paper_id"] for paper in papers]
        selected_ids = [
            item["evidence_id"] for item in selected if item["source_paper_id"] in set(paper_ids)
        ]
        status_counts = Counter(coverage_map[paper_id]["status"] for paper_id in paper_ids)
        batch_identity = {
            "batch_index": index + 1,
            "paper_ids": paper_ids,
        }
        batches.append(
            {
                "batch_id": f"batch_{index + 1:03d}_{_stable_hash(batch_identity)[:10]}",
                "batch_index": index + 1,
                "paper_ids": paper_ids,
                "paper_codes": [paper.get("paper_code") for paper in papers],
                "selected_evidence_ids": selected_ids,
                "budget": {
                    "initial_allocated": initial_batch_allocations[index],
                    "allocated": batch_allocations[index],
                    "reallocated_delta": (
                        batch_allocations[index] - initial_batch_allocations[index]
                    ),
                    "used": len(selected_ids),
                    "remaining": max(0, batch_allocations[index] - len(selected_ids)),
                    "global_evidence_budget": evidence_budget,
                },
                "coverage_summary": dict(sorted(status_counts.items())),
            }
        )
    return batches


def _plan_fingerprint(
    *,
    query: str,
    scope: dict[str, Any],
    retrieval_intent: RetrievalIntent,
    requested_sections: list[str] | None,
    limits: dict[str, int],
    dft_cap: int,
    selected: list[dict[str, Any]],
) -> str:
    evidence_snapshots = []
    for item in selected:
        evidence_snapshots.append(
            {
                "evidence_id": item["evidence_id"],
                "review_status": item.get("review_status"),
                "gate_status": item.get("gate_status"),
                "locator": item.get("evidence_locator"),
                "property": item.get("property"),
                "value": item.get("value"),
                "unit": item.get("unit"),
                "context": item.get("context"),
                "excerpt_sha256": hashlib.sha256(
                    str(item.get("excerpt") or "").encode("utf-8")
                ).hexdigest(),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "query": _normalized_text(query),
        "paper_ids": scope["unique_paper_ids"],
        "paper_metadata": scope["valid_papers"],
        "retrieval_intent": retrieval_intent.as_dict(),
        "requested_sections": [
            _normalized_text(item) for item in (requested_sections or [])
        ],
        "limits": limits,
        "dft_cap": dft_cap,
        "selected_evidence_snapshots": evidence_snapshots,
    }
    return _stable_hash(payload)


def _stable_hash(value: Any) -> str:
    serialized = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _as_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_not_none(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)
