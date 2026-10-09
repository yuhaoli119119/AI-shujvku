import pytest

from app.evaluation import ocr_error_rates, retrieval_metrics, table_metrics


def test_retrieval_metrics_fixture():
    result = retrieval_metrics(["a", "x", "b", "y"], {"a", "b", "c"}, k=3)

    assert result["recall@3"] == pytest.approx(2 / 3)
    assert result["precision@3"] == pytest.approx(2 / 3)
    assert result["mrr"] == 1.0
    assert 0.0 < result["ndcg@3"] <= 1.0


def test_retrieval_metrics_stably_deduplicates_relevant_ids():
    result = retrieval_metrics(["a", "a", "a"], {"a"}, k=3)

    assert result == {
        "recall@3": 1.0,
        "precision@3": pytest.approx(1 / 3),
        "mrr": 1.0,
        "ndcg@3": 1.0,
    }


def test_retrieval_metrics_deduplicates_irrelevant_ids_before_ranking():
    result = retrieval_metrics(["x", "x", "a"], {"a"}, k=3)

    assert result["recall@3"] == 1.0
    assert result["precision@3"] == pytest.approx(1 / 3)
    assert result["mrr"] == pytest.approx(1 / 2)


def test_retrieval_metrics_short_results_use_fixed_k_denominator():
    result = retrieval_metrics(["a"], {"a", "b"}, k=4)

    assert result["recall@4"] == pytest.approx(1 / 2)
    assert result["precision@4"] == pytest.approx(1 / 4)


def test_retrieval_metrics_empty_relevant_set_is_all_zero():
    result = retrieval_metrics(["a", "b"], set(), k=2)

    assert all(value == 0.0 for value in result.values())


def test_retrieval_metrics_rejects_non_positive_k_and_stays_bounded():
    with pytest.raises(ValueError, match="k must be positive"):
        retrieval_metrics(["a"], {"a"}, k=0)
    with pytest.raises(ValueError, match="k must be positive"):
        retrieval_metrics(["a"], {"a"}, k=-1)

    result = retrieval_metrics(["a", "a", "x", "b", "b"], {"a", "b"}, k=4)
    assert all(0.0 <= value <= 1.0 for value in result.values())


def test_ocr_error_rate_fixture():
    result = ocr_error_rates("alpha beta", "alpha zeta")

    assert result["cer"] == pytest.approx(1 / 10)
    assert result["wer"] == pytest.approx(1 / 2)


def test_table_metric_fixture():
    result = table_metrics(
        [["Material", "Value"], ["Fe-N-C", "1.2"], ["Co-N-C", "0.9"]],
        [["Material", "Value"], ["Fe-N-C", "1.2"], ["Co-N-C", "0.8"]],
    )

    assert result["cell_exact"] == pytest.approx(5 / 6)
    assert result["cell_f1"] == pytest.approx(5 / 6)
    assert result["row_coverage"] == pytest.approx(2 / 3)
