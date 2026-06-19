"""Offline evaluation helpers; this package is not wired into production flows."""

from app.evaluation.metrics import ocr_error_rates, retrieval_metrics, table_metrics

__all__ = ["ocr_error_rates", "retrieval_metrics", "table_metrics"]
