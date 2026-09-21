from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

import app.services.dft_review_bundle_service as bundle_module
from app.services.dft_review_bundle_service import DFTReviewBundleService


@pytest.mark.no_test_database
@pytest.mark.unit
@pytest.mark.parametrize(
    "v2_receipt, legacy_audit, stage, completed_fingerprint, scope_complete, expected",
    [
        (True, False, "completed", "current", True, 2),
        (False, True, "completed", "current", True, 2),
        (False, False, "completed", "current", True, 0),
        (True, False, "completed", "old", True, 0),
        (True, False, "completed", "current", False, 0),
        (True, False, "stale", "old", False, 0),
    ],
)
def test_paper_reviewed_aggregate_accepts_only_current_completed_v2_scope(
    monkeypatch, v2_receipt, legacy_audit, stage, completed_fingerprint, scope_complete, expected
):
    paper = SimpleNamespace(id=uuid4(), paper_code="T-V2-DFT")
    figure_id, table_id = uuid4(), uuid4()
    task = {
        "paper_id": str(paper.id),
        "paper_code": paper.paper_code,
        "scope_type": "paper",
        "stage_status": stage,
        "current_snapshot_fingerprint": "current",
        "completed_snapshot_fingerprint": completed_fingerprint,
        "unresolved_count": 0,
        "scope_completion": {"complete": scope_complete},
        "figures": [{"source_record_id": str(figure_id), "source_paper_id": str(paper.id)}],
        "tables": [{"source_record_id": str(table_id), "source_paper_id": str(paper.id)}],
    }

    class FakeChartReview:
        def get_review_scope_options(self, paper_id):
            assert paper_id == paper.id
            return {"paper_scope": task, "chart_runs": []}

        def _latest_review_audit(self, paper_id, *, run_id, actions):
            assert paper_id == paper.id and run_id is None
            return object() if legacy_audit else None

    monkeypatch.setattr(bundle_module, "EvidenceReviewBundleService", lambda *_: FakeChartReview())
    session = Mock()
    session.scalar.return_value = object() if v2_receipt else None
    aggregate = DFTReviewBundleService(session, None)._reviewed_evidence_aggregate(
        paper, source_by_id={paper.id: {"paper": paper}}
    )
    assert len(aggregate["objects"]) == expected
    assert len(aggregate["review_runs"]) == (1 if expected else 0)
    assert bool(aggregate["completed_snapshot_fingerprint"]) == bool(expected)
    if expected:
        assert aggregate["summary"]["reviewed_main_figures"] == 1
        assert aggregate["summary"]["reviewed_main_tables"] == 1

