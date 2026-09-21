"""Regression tests for the chart-completion authority and the DFT state machine.

A. the whole-paper chart stage is authoritative and the review-center list must
   be able to read it on first paint, even when the legacy
   ``manual_review_progress`` compatibility field is empty;
B. ``ai_verified_ml_ready`` requires reaction semantics, task profile and export
   gate -- "field verified" and "ML ready" are different claims.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import DFTResult, Paper
from app.db.session import get_db_session
from app.domain.reaction_taxonomy import normalize_reaction_type
from app.extractors.dft_results_extractor import (
    DFTResultsExtractor,
    _normalize_numeric_text,
    _resolve_adsorbate,
)
from app.main import app
from app.services.ai_verification_service import AIVerificationService
from app.services.dft_export_service import _v3_exclusion_reason
from app.services.dft_review_queue_service import DFTReviewQueueService
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService
from app.utils.dft_candidate_status import DFT_STATUS_PENDING
from app.utils.review_safety import ExportGateResult

# Candidate-only modules.  They do not exist on the pre-fix baseline, so they are
# imported defensively: the "baseline code + candidate tests" matrix cell then
# reports one failure per test instead of an import-time collection error.
try:
    from app.domain.capability_matrix import build_capability_matrix
    from app.extractors.dft_results_extractor import _number_is_fragment
    from app.domain.reaction_taxonomy import (
        MATERIAL_DIMENSION_TERMS,
        is_material_dimension_term,
    )
    from app.services import dft_ml_readiness
    from app.services.chart_review_authority import (
        ChartReviewAuthority,
        sync_figures_review_completed,
    )
    from app.services.dft_ml_readiness import evaluate_dft_record_ml_readiness
    from app.services.evidence_review_bundle_service import _normalize_progress_entry
    from app.utils.dft_candidate_status import (
        DFT_STATUS_FIELD_VERIFIED,
        DFT_STATUS_READY_AI,
    )

    CANDIDATE_MODULES_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - only on the baseline tree
    CANDIDATE_MODULES_ERROR = f"{type(exc).__name__}: {exc}"


@pytest.fixture(autouse=True)
def _require_candidate_modules():
    if CANDIDATE_MODULES_ERROR is not None:
        pytest.fail(f"candidate modules are missing on this tree ({CANDIDATE_MODULES_ERROR})")


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def stabilization_env(monkeypatch, tmp_path):
    """Isolated API + storage environment bound to the test database."""
    import os

    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from app.db.models import Base

    storage_root = Path(tmp_path) / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LITAI_DATABASE_URL", os.environ["LITAI_TEST_DATABASE_URL"])
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "true")
    monkeypatch.setenv("LITAI_AUTH_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
    Base.metadata.create_all(engine)
    factory = _sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        yield storage_root, factory, engine
    finally:
        app.dependency_overrides.clear()
        from app.db.session import _engines, _session_factories

        for cached in list(_engines.values()):
            cached.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()
        engine.dispose()


def _chart_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "stage_status": "completed",
        "unresolved_count": 0,
        "current_snapshot_fingerprint": "fp-current",
        "completed_snapshot_fingerprint": "fp-current",
        "figure_reading_coverage": {"completed": 2, "total": 2},
        "scope_completion": {
            "complete": True,
            "expected_figure_ids": ["fig-1", "fig-2"],
            "reviewed_figure_ids": ["fig-1", "fig-2"],
            "expected_table_ids": ["tab-1"],
            "reviewed_table_ids": ["tab-1"],
            "changed_figure_ids": [],
            "changed_table_ids": [],
            "missing_figure_ids": [],
            "missing_table_ids": [],
        },
    }
    task.update(overrides)
    return task


def _patch_task(monkeypatch, task: dict[str, Any]) -> None:
    monkeypatch.setattr(
        EvidenceReviewBundleService,
        "get_review_task",
        lambda self, paper_id: dict(task),
    )


# --------------------------------------------------------------------------- #
# A. chart completion authority
# --------------------------------------------------------------------------- #


def test_authoritative_chart_completed_with_empty_legacy_flag_is_complete(monkeypatch, stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        paper = Paper(title="B0091 shape", pdf_path="", authors=[])
        session.add(paper)
        session.commit()
        assert paper.comprehensive_analysis is None

        _patch_task(monkeypatch, _chart_task())
        stage = ChartReviewAuthority(session, get_settings()).read(paper.id)

        assert stage.stage_status == "completed"
        assert stage.figures_completed is True
        assert stage.payload["scope_type"] == "paper"
        assert stage.payload["run_id"] is None
        assert stage.payload["snapshot_fingerprint_matches"] is True
        # the compatibility field is still empty at this point -- that is the B0091 shape
        assert (session.get(Paper, paper.id).comprehensive_analysis or {}) == {}


def test_legacy_compatibility_flag_is_written_only_from_authoritative_completion(monkeypatch, stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        paper = Paper(title="B0091 shape", pdf_path="", authors=[])
        session.add(paper)
        session.commit()

        _patch_task(monkeypatch, _chart_task())
        task = EvidenceReviewBundleService(session, get_settings()).get_review_task(paper.id)
        stage = ChartReviewAuthority(session, get_settings()).project(task, paper_id=paper.id)
        written = sync_figures_review_completed(
            session,
            paper.id,
            reviewer="v2_reviewer",
            stage_status=stage.stage_status,
            unresolved_count=stage.payload["unresolved_count"],
            snapshot_fingerprint_matches=stage.payload["snapshot_fingerprint_matches"],
        )
        session.commit()
        assert written is True
        progress = (session.get(Paper, paper.id).comprehensive_analysis or {})["manual_review_progress"]
        assert progress["figures"]["completed"] is True
        assert progress["figures"]["updated_by"] == "v2_reviewer"


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"completed_snapshot_fingerprint": "fp-stale"}, "stale snapshot"),
        ({"unresolved_count": 2}, "unresolved actions"),
        ({"scope_completion": {"complete": False}}, "incomplete scope"),
        ({"stage_status": "in_progress"}, "still running"),
        ({"stage_status": "completed_with_issues", "unresolved_count": 1}, "issues left"),
    ],
)
def test_authoritative_stage_refuses_incomplete_shapes(monkeypatch, stabilization_env, overrides, reason):
    _, factory, _ = stabilization_env
    with factory() as session:
        paper = Paper(title=f"negative {reason}", pdf_path="", authors=[])
        session.add(paper)
        session.commit()

        _patch_task(monkeypatch, _chart_task(**overrides))
        stage = ChartReviewAuthority(session, get_settings()).read(paper.id)
        assert stage.figures_completed is False, reason
        assert stage.payload["dft_gate_allowed"] is False, reason

        written = sync_figures_review_completed(
            session,
            paper.id,
            reviewer="v2_reviewer",
            stage_status=stage.stage_status,
            unresolved_count=stage.payload["unresolved_count"],
            snapshot_fingerprint_matches=stage.payload["snapshot_fingerprint_matches"],
        )
        session.commit()
        assert written is False, reason
        assert (session.get(Paper, paper.id).comprehensive_analysis or {}) == {}


def test_external_analysis_run_partial_batch_cannot_complete_the_paper(stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        paper = Paper(title="partial run", pdf_path="", authors=[])
        session.add(paper)
        session.commit()

        # A run-scoped batch is not the whole paper, even when its own stage is completed.
        assert (
            sync_figures_review_completed(
                session,
                paper.id,
                reviewer="run_reviewer",
                stage_status="completed",
                unresolved_count=0,
                snapshot_fingerprint_matches=True,
                legacy_source_paper_scope=False,
            )
            is False
        )
        session.commit()
        assert (session.get(Paper, paper.id).comprehensive_analysis or {}) == {}

        # the legacy writer must propagate the same refusal
        EvidenceReviewBundleService(session, get_settings())._mark_figures_review_completed(
            paper.id,
            "run_reviewer",
            stage_status="completed",
            unresolved_count=0,
            snapshot_fingerprint_matches=True,
            legacy_source_paper_scope=False,
        )
        session.commit()
        assert (session.get(Paper, paper.id).comprehensive_analysis or {}) == {}


def test_legacy_flag_cannot_override_a_stronger_authoritative_state(monkeypatch, stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        paper = Paper(
            title="legacy flag set but snapshot stale",
            pdf_path="",
            authors=[],
            comprehensive_analysis={"manual_review_progress": {"figures": {"completed": True, "updated_by": "old"}}},
        )
        session.add(paper)
        session.commit()

        _patch_task(monkeypatch, _chart_task(completed_snapshot_fingerprint="fp-stale"))
        stage = ChartReviewAuthority(session, get_settings()).read(paper.id)
        # the compatibility field says True, the authority says the snapshot moved
        assert stage.figures_completed is False
        assert (session.get(Paper, paper.id).comprehensive_analysis["manual_review_progress"]["figures"]["completed"]) is True


def test_normalize_progress_entry_handles_both_legacy_shapes():
    assert _normalize_progress_entry({"figures": True}, "figures")["completed"] is True
    assert _normalize_progress_entry({"figures": {"completed": True}}, "figures")["completed"] is True
    assert _normalize_progress_entry({}, "figures")["completed"] is False


def test_review_center_chart_stages_endpoint_returns_authoritative_stage(monkeypatch, stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        papers = [Paper(title=f"paper {index}", pdf_path="", authors=[]) for index in range(3)]
        session.add_all(papers)
        session.commit()
        ids = [str(paper.id) for paper in papers]

    _patch_task(monkeypatch, _chart_task())
    client = TestClient(app)
    response = client.get("/api/workbench/review-center/chart-stages", params={"paper_ids": ",".join(ids)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scope_type"] == "paper"
    assert payload["max_batch"] == ChartReviewAuthority.MAX_BATCH
    assert set(payload["stages"]) == set(ids)
    for stage in payload["stages"].values():
        assert stage["figures_completed"] is True
        assert stage["stage_status"] == "completed"
        assert stage["run_id"] is None

    assert client.get("/api/workbench/review-center/chart-stages").status_code == 422
    bad = client.get("/api/workbench/review-center/chart-stages", params={"paper_ids": "not-a-uuid"})
    assert bad.status_code == 422

    too_many = ",".join(str(uuid4()) for _ in range(ChartReviewAuthority.MAX_BATCH + 1))
    oversized = client.get("/api/workbench/review-center/chart-stages", params={"paper_ids": too_many})
    assert oversized.status_code == 422
    assert "too_many_paper_ids" in oversized.json()["detail"]


def test_chart_stages_endpoint_deduplicates_and_drops_duplicate_ids(monkeypatch, stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        paper = Paper(title="dup", pdf_path="", authors=[])
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)
    _patch_task(monkeypatch, _chart_task())
    client = TestClient(app)
    response = client.get(
        "/api/workbench/review-center/chart-stages",
        params={"paper_ids": f"{paper_id},{paper_id}"},
    )
    assert response.status_code == 200
    assert list(response.json()["stages"]) == [paper_id]


# --------------------------------------------------------------- #
# D. domain model (capability matrix)
# --------------------------------------------------------------- #


def test_chart_stage_endpoint_degrades_one_reader_error(monkeypatch, stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        paper = Paper(title="reader failure", pdf_path="", authors=[])
        session.add(paper)
        session.commit()
        paper_id = str(paper.id)

    def fail_read(self, paper_id):
        raise RuntimeError("synthetic reader failure")

    monkeypatch.setattr(ChartReviewAuthority, "read", fail_read)
    client = TestClient(app)
    response = client.get(
        "/api/workbench/review-center/chart-stages",
        params={"paper_ids": paper_id},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stages"] == {}
    assert "RuntimeError" in payload["errors"][paper_id]


def test_graphdiyne_is_a_material_dimension_not_a_reaction_type():
    assert "graphdiyne" in MATERIAL_DIMENSION_TERMS
    assert is_material_dimension_term("graphdiyne") is True
    assert normalize_reaction_type("graphdiyne") == "UNKNOWN"
    assert normalize_reaction_type("Graphdiyne") == "UNKNOWN"
    assert normalize_reaction_type("SRR_LiS") == "SRR_LiS"
    matrix = build_capability_matrix()
    assert "graphdiyne" in matrix["materials"]["terms"]
    assert "graphdiyne" not in matrix["reactions"]


def test_capability_matrix_separates_production_from_experimental_reactions():
    matrix = build_capability_matrix()
    reactions = matrix["reactions"]
    assert reactions["SRR_LiS"]["status"] == "production"
    assert reactions["SRR_LiS"]["ml_export"] == "supported"
    assert reactions["SRR_LiS"]["task_profile_count"] > 0
    for reaction in ("HER", "OER", "ORR", "CO2RR"):
        entry = reactions[reaction]
        assert entry["status"] == "experimental", reaction
        assert entry["ml_export"] == "not_supported", reaction
        assert entry["task_profile_count"] == 0, reaction
    assert reactions["UNKNOWN"]["ml_export"] == "not_supported"


# --------------------------------------------------------------------------- #
# B. DFT ML-readiness hard gate
# --------------------------------------------------------------------------- #


ELIGIBLE_GATE = ExportGateResult(
    eligible=True,
    reasons=(),
    review_status="verified",
    review_gate_status="passed",
    provenance_level="authoritative",
    locator_status="exact_page",
)


def _eligible_gate(*args, **kwargs):
    return ELIGIBLE_GATE


@pytest.fixture
def eligible_export_gate(monkeypatch):
    """Pretend the evidence/export gate passed, so only reaction semantics can block."""
    monkeypatch.setattr(dft_ml_readiness, "is_export_eligible_extraction", _eligible_gate)


@pytest.fixture
def all_fields_accepted(monkeypatch):
    """Pretend every required field review reached its accepted terminal state."""
    monkeypatch.setattr(
        AIVerificationService,
        "dft_field_terminal_status",
        lambda self, **kwargs: "verified",
    )


def _dft_row(session: Session, **overrides: Any) -> DFTResult:
    paper = Paper(title="dft fixture", pdf_path="", authors=[])
    session.add(paper)
    session.flush()
    values: dict[str, Any] = {
        "paper_id": paper.id,
        "property_type": "reaction_barrier",
        "adsorbate": "S8",
        "value": 2.13,
        "unit": "eV",
        "reaction_type": "SRR_LiS",
        "reaction_validation_status": "out_of_scope",
        "candidate_status": DFT_STATUS_PENDING,
        "evidence_text": "the desorption process of a single sulfur atom needs a barrier of 2.13 eV",
        "evidence_payload": {"material_identity": "Co-N4"},
        "extraction_protocol_version": "system_candidate_dft_v1",
    }
    values.update(overrides)
    row = DFTResult(**values)
    session.add(row)
    session.flush()
    return row


def test_out_of_scope_record_with_every_field_accepted_is_not_ml_ready(
    stabilization_env, eligible_export_gate, all_fields_accepted
):
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session)
        assert row.reaction_validation_status == "out_of_scope"

        readiness = evaluate_dft_record_ml_readiness(session, row)
        assert readiness.ml_ready is False
        assert "reaction_validation_out_of_scope" in readiness.reasons

        status = AIVerificationService(session, get_settings()).sync_dft_record_candidate_status(row.paper_id, row)
        session.commit()
        assert status == DFT_STATUS_FIELD_VERIFIED
        assert session.get(DFTResult, row.id).candidate_status == DFT_STATUS_FIELD_VERIFIED
        assert session.get(DFTResult, row.id).candidate_status != DFT_STATUS_READY_AI


@pytest.mark.parametrize(
    "overrides,expected_reason",
    [
        ({"reaction_validation_status": "out_of_scope"}, "reaction_validation_out_of_scope"),
        ({"reaction_validation_status": None}, "reaction_validation_missing"),
        ({"reaction_validation_status": "pending"}, "reaction_validation_pending"),
        ({"reaction_type": None}, "unknown_reaction_type"),
        ({"reaction_type": "graphdiyne"}, "unknown_reaction_type"),
    ],
)
def test_reaction_semantics_block_ml_readiness(
    stabilization_env, eligible_export_gate, overrides, expected_reason
):
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session, **overrides)
        readiness = evaluate_dft_record_ml_readiness(session, row)
        assert readiness.ml_ready is False
        assert expected_reason in readiness.reasons, readiness.reasons


def test_ml_ready_still_requires_a_registered_task_profile(stabilization_env, eligible_export_gate):
    """A valid reaction is not enough when no ML task profile covers the property."""
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(
            session,
            property_type="sulfur_desorption_criterion",
            reaction_validation_status="valid",
        )
        readiness = evaluate_dft_record_ml_readiness(session, row)
        assert readiness.ml_ready is False
        assert "no_task_profile_for_property" in readiness.reasons


def test_rds_profile_semantics_match_v3_export(stabilization_env, eligible_export_gate):
    """A generic Gibbs value is not RDS-ready merely because its common gate passes."""
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(
            session,
            property_type="gibbs_free_energy_change",
            reaction_validation_status="valid",
            reaction_step="overall sulfur reduction free energy",
            evidence_text="The overall sulfur reduction free energy is 2.13 eV.",
        )
        readiness = evaluate_dft_record_ml_readiness(session, row)
        # The RDS exporter rejects this exact row, while the general reaction
        # free-energy matrix may still accept it.  The shared evaluator must pick
        # only a profile whose own V3 predicate accepts the row.
        assert _v3_exclusion_reason(
            row,
            "SRR_LiS:rds_gibbs_free_energy",
            "SRR_LiS",
            frozenset({"gibbs_free_energy_change"}),
        ) in {"missing_rds_semantics", "overall_srr_free_energy_not_rds"}
        assert readiness.ml_ready is True
        assert readiness.task_profile == "SRR_LiS:reaction_free_energy_matrix"


def test_stale_ml_ready_label_is_rederived_when_reaction_is_invalid(
    stabilization_env, eligible_export_gate, all_fields_accepted
):
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session, candidate_status=DFT_STATUS_READY_AI)
        status = AIVerificationService(session, get_settings()).sync_dft_record_candidate_status(row.paper_id, row)
        session.commit()
        assert status == DFT_STATUS_FIELD_VERIFIED
        assert session.get(DFTResult, row.id).candidate_status == DFT_STATUS_FIELD_VERIFIED


def test_accepted_fields_with_valid_reaction_are_still_promoted(stabilization_env, eligible_export_gate, all_fields_accepted):
    """The new gate must not block the legitimate case it is meant to allow."""
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session, reaction_validation_status="valid")
        status = AIVerificationService(session, get_settings()).sync_dft_record_candidate_status(row.paper_id, row)
        session.commit()
        assert status == DFT_STATUS_READY_AI
        assert session.get(DFTResult, row.id).candidate_status == DFT_STATUS_READY_AI


def test_human_chosen_ml_ready_outranks_recomputation(stabilization_env):
    """``ML_Ready`` is the human token and stays untouched; only the AI token is re-derived."""
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session, candidate_status="ML_Ready")
        status = AIVerificationService(session, get_settings()).sync_dft_record_candidate_status(row.paper_id, row)
        assert status == "ML_Ready"
        assert session.get(DFTResult, row.id).candidate_status == "ML_Ready"


@pytest.mark.parametrize(
    "locked_status",
    ["Rejected", "human_reviewed_needs_evidence", "ai_terminal_unusable", "ML_Ready"],
)
def test_repair_locked_statuses_survive_public_and_internal_recompute(stabilization_env, locked_status):
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session, candidate_status=locked_status)
        service = AIVerificationService(session, get_settings())
        assert service.sync_dft_record_candidate_status(row.paper_id, row) == locked_status
        service._sync_dft_record_closure(row.paper_id, row)
        session.flush()
        assert row.candidate_status == locked_status
        assert session.get(DFTResult, row.id).candidate_status == locked_status


def test_review_queue_overlay_hides_ml_ready_when_reaction_invalid(stabilization_env):
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session, candidate_status=DFT_STATUS_READY_AI)
        paper = session.get(Paper, row.paper_id)
        service = DFTReviewQueueService(session)
        payload = service._row_payload(row, paper, ELIGIBLE_GATE)
        assert payload["candidate_status"] == DFT_STATUS_FIELD_VERIFIED
        assert payload["candidate_status"] != "ML_Ready"
        assert payload["is_exportable"] is True

        row.reaction_validation_status = "valid"
        session.flush()
        payload = service._row_payload(row, paper, ELIGIBLE_GATE)
        assert payload["candidate_status"] == DFT_STATUS_READY_AI


def test_review_queue_overlay_keeps_blocked_from_export_taking_precedence(stabilization_env):
    _, factory, _ = stabilization_env
    blocked_gate = ExportGateResult(
        eligible=False,
        reasons=("missing_review",),
        review_status="missing",
        review_gate_status="blocked",
        provenance_level="none",
        locator_status="missing",
    )
    with factory() as session:
        row = _dft_row(session, candidate_status="ML_Ready")
        paper = session.get(Paper, row.paper_id)
        payload = DFTReviewQueueService(session)._row_payload(row, paper, blocked_gate)
        assert payload["candidate_status"] == "blocked_from_export"


def test_ml_readiness_and_v3_export_agree(stabilization_env, eligible_export_gate):
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(session)
        readiness = evaluate_dft_record_ml_readiness(session, row)
        reason = _v3_exclusion_reason(row, "SRR_LiS:reaction_barrier", "SRR_LiS", frozenset({"reaction_barrier"}))
        assert readiness.ml_ready is False
        assert reason == "reaction_validation_out_of_scope"

        row.reaction_validation_status = "valid"
        session.flush()
        readiness = evaluate_dft_record_ml_readiness(session, row)
        reason = _v3_exclusion_reason(row, "SRR_LiS:reaction_barrier", "SRR_LiS", frozenset({"reaction_barrier"}))
        assert readiness.ml_ready is True, readiness.reasons
        assert reason is None


def test_ml_readiness_and_v3_export_agree_on_rejected_property(stabilization_env, eligible_export_gate):
    """A property the reaction taxonomy rejects must be refused by both."""
    _, factory, _ = stabilization_env
    with factory() as session:
        row = _dft_row(
            session,
            property_type="sulfur_desorption_criterion",
            reaction_validation_status="valid",
        )
        readiness = evaluate_dft_record_ml_readiness(session, row)
        reason = _v3_exclusion_reason(
            row, "SRR_LiS:reaction_barrier", "SRR_LiS", frozenset({"reaction_barrier"})
        )
        assert readiness.ml_ready is False
        assert reason == "target_property_not_allowed"
