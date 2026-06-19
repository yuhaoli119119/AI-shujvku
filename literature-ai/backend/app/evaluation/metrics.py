from __future__ import annotations

from collections import Counter
import math
import re
from typing import Hashable, Sequence


def retrieval_metrics(
    ranked_ids: Sequence[Hashable],
    relevant_ids: set[Hashable],
    *,
    k: int,
) -> dict[str, float]:
    """Compute binary IR metrics after stable first-occurrence de-duplication.

    Precision@K always uses ``k`` as its denominator, so fewer than ``k``
    unique returned items leave the remaining ranks as non-relevant slots.
    An empty relevant set returns zero for every metric. ``k <= 0`` is invalid.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    top_k: list[Hashable] = []
    seen: set[Hashable] = set()
    for item in ranked_ids:
        if item in seen:
            continue
        seen.add(item)
        top_k.append(item)
        if len(top_k) >= k:
            break
    hits = [1 if item in relevant_ids else 0 for item in top_k]
    hit_count = sum(hits)
    recall = hit_count / len(relevant_ids) if relevant_ids else 0.0
    precision = hit_count / k
    first_rank = next((index for index, hit in enumerate(hits, start=1) if hit), None)
    mrr = 1.0 / first_rank if first_rank else 0.0
    dcg = sum(hit / math.log2(index + 1) for index, hit in enumerate(hits, start=1))
    ideal_hits = min(len(relevant_ids), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return {
        f"recall@{k}": recall,
        f"precision@{k}": precision,
        "mrr": mrr,
        f"ndcg@{k}": dcg / idcg if idcg else 0.0,
    }


def _edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for ref_index, ref_item in enumerate(reference, start=1):
        current = [ref_index]
        for hyp_index, hyp_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[hyp_index] + 1,
                    previous[hyp_index - 1] + (ref_item != hyp_item),
                )
            )
        previous = current
    return previous[-1]


def ocr_error_rates(reference: str, hypothesis: str) -> dict[str, float]:
    """Return standard character and whitespace-token word error rates."""
    ref_chars = list(reference)
    hyp_chars = list(hypothesis)
    ref_words = re.findall(r"\S+", reference)
    hyp_words = re.findall(r"\S+", hypothesis)
    return {
        "cer": _edit_distance(ref_chars, hyp_chars) / max(len(ref_chars), 1),
        "wer": _edit_distance(ref_words, hyp_words) / max(len(ref_words), 1),
    }


def table_metrics(reference: Sequence[Sequence[str]], hypothesis: Sequence[Sequence[str]]) -> dict[str, float]:
    """Compute position-aware cell exactness, cell-value F1 and exact-row coverage."""
    reference_cells = [str(cell).strip() for row in reference for cell in row]
    hypothesis_cells = [str(cell).strip() for row in hypothesis for cell in row]
    aligned_total = max(len(reference_cells), len(hypothesis_cells), 1)
    aligned_matches = sum(left == right for left, right in zip(reference_cells, hypothesis_cells))

    reference_counts = Counter(reference_cells)
    hypothesis_counts = Counter(hypothesis_cells)
    shared = sum((reference_counts & hypothesis_counts).values())
    precision = shared / max(len(hypothesis_cells), 1)
    recall = shared / max(len(reference_cells), 1)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    reference_rows = Counter(tuple(str(cell).strip() for cell in row) for row in reference)
    hypothesis_rows = Counter(tuple(str(cell).strip() for cell in row) for row in hypothesis)
    shared_rows = sum((reference_rows & hypothesis_rows).values())
    return {
        "cell_exact": aligned_matches / aligned_total,
        "cell_precision": precision,
        "cell_recall": recall,
        "cell_f1": f1,
        "row_coverage": shared_rows / max(len(reference), 1),
    }
