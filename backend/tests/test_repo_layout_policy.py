from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.no_test_database

ROOT = Path(__file__).resolve().parents[3]


def test_legacy_root_artifact_directories_are_absent():
    legacy_paths = [
        "backups",
        "outputs",
        "test-artifacts",
        "literature-ai-security-backups",
    ]
    present = [relative for relative in legacy_paths if (ROOT / relative).exists()]
    assert not present, f"legacy root artifact paths still present: {present}"


def test_pdf_eval_fixtures_only_hold_reusable_inputs():
    fixture_dir = ROOT / "local" / "test-fixtures" / "pdf-eval"
    if not fixture_dir.exists():
        pytest.skip("pdf-eval fixture set is not present on this machine")

    entries = sorted(item.name for item in fixture_dir.iterdir())
    assert entries == ["pdfs"], f"unexpected pdf-eval fixture entries: {entries}"
    pdfs = sorted(item.name for item in (fixture_dir / "pdfs").glob("*.pdf"))
    assert pdfs, "expected reusable PDF inputs under local/test-fixtures/pdf-eval/pdfs"


def test_pdf_eval_runtime_snapshot_lives_under_test_runs():
    runtime_dir = ROOT / "local" / "test-runs" / "pdf-eval" / "legacy_snapshot"
    if not runtime_dir.exists():
        pytest.skip("pdf-eval runtime snapshot is not present on this machine")

    expected_entries = {"eval.sqlite", "ingestion_results.json", "storage"}
    actual_entries = {item.name for item in runtime_dir.iterdir()}
    missing = sorted(expected_entries - actual_entries)
    assert not missing, f"missing pdf-eval runtime snapshot entries: {missing}"
