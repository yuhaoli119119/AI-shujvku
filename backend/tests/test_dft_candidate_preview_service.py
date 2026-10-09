from __future__ import annotations

from uuid import uuid4

import pytest

from app.db.models import AuditLog
from app.services.dft_candidate_preview_service import DFTCandidatePreviewService


pytestmark = pytest.mark.no_test_database


class ReadOnlySessionGuard:
    def add(self, *_args, **_kwargs):
        raise AssertionError("preview must not add rows")

    def delete(self, *_args, **_kwargs):
        raise AssertionError("preview must not delete rows")

    def flush(self, *_args, **_kwargs):
        raise AssertionError("preview must not flush")

    def commit(self, *_args, **_kwargs):
        raise AssertionError("preview must not commit")


def _candidate(
    *,
    source_type: str,
    source_paper_id: str,
    value: float = -1.26,
    site_label: str = "hollow",
    material_identity: str | None = "ZnO",
    evidence_text: str | None = None,
    **extra,
):
    return {
        "source_type": source_type,
        "source_paper_id": source_paper_id,
        "source_record_id": str(uuid4()),
        "evidence_text": evidence_text or f"O2 adsorption energy is {value} eV.",
        "material_identity": material_identity,
        "adsorbate": "O2",
        "property_type": "adsorption_energy",
        "raw_value": value,
        "raw_unit": "eV",
        "site_label": site_label,
        "confidence": 0.9,
        **extra,
    }


def test_exact_stage2_figure_and_table_candidates_form_one_multi_evidence_observation():
    paper_id = uuid4()
    service = DFTCandidatePreviewService(ReadOnlySessionGuard())
    preview = service.preview_candidates(
        paper_id=paper_id,
        paper_code="P0010",
        candidates=[
            _candidate(
                source_type="stage2",
                source_paper_id=str(paper_id),
                evidence_text="正文给出 O2 吸附能 -1.26 eV。",
                page=7,
            ),
            _candidate(
                source_type="stage2",
                source_paper_id=str(paper_id),
                evidence_text="Table 2: O2 hollow -1.26 eV.",
                page=8,
                table_id="table-2",
                row=3,
                column=2,
            ),
            _candidate(
                source_type="figure_review",
                source_paper_id=str(paper_id),
                evidence_text="Fig. 5c labels -1.26 eV.",
                page=8,
                figure_label="Fig. 5c",
            ),
        ],
    )

    assert preview["read_only"] is True
    assert preview["summary"] == {
        "total_candidates": 3,
        "scientific_observations": 1,
        "exact_clusters": 1,
        "multi_evidence_clusters": 1,
        "possible_conflicts": 0,
        "incomplete_identity": 0,
        "distinct_observations": 1,
    }
    assert preview["source_counts"] == {"stage2": 2, "figure_review": 1, "si": 0}
    assert len(preview["clusters"]) == 1
    assert preview["clusters"][0]["cluster_type"] == "EXACT_OBSERVATION"
    assert preview["clusters"][0]["candidate_count"] == 3
    assert preview["clusters"][0]["evidence_count"] == 3


def test_distinct_sites_separate_while_same_subject_different_values_flag_conflict():
    paper_id = uuid4()
    service = DFTCandidatePreviewService(ReadOnlySessionGuard())
    preview = service.preview_candidates(
        paper_id=paper_id,
        paper_code="P0011",
        candidates=[
            _candidate(source_type="stage2", source_paper_id=str(paper_id), site_label="hollow", value=-1.26),
            _candidate(source_type="figure_review", source_paper_id=str(paper_id), site_label="hollow", value=-1.62),
            _candidate(source_type="stage2", source_paper_id=str(paper_id), site_label="top", value=-0.75),
        ],
    )

    conflict_clusters = [cluster for cluster in preview["clusters"] if cluster["possible_conflict"]]
    top_clusters = [
        cluster for cluster in preview["clusters"] if cluster["scientific_fields"]["site_label"] == "top"
    ]
    assert preview["summary"]["scientific_observations"] == 3
    assert preview["summary"]["possible_conflicts"] == 1
    assert len(conflict_clusters) == 2
    assert {cluster["cluster_type"] for cluster in conflict_clusters} == {
        "SAME_SUBJECT_DIFFERENT_VALUE"
    }
    assert sum(cluster["candidate_count"] for cluster in conflict_clusters) == 2
    assert len(top_clusters) == 1
    assert top_clusters[0]["cluster_type"] == "DISTINCT_SUBJECT"
    assert all(cluster["possible_rounding"] is False for cluster in preview["clusters"])


def test_incomplete_candidate_stays_isolated_and_si_keeps_source_paper_identity():
    main_paper_id = uuid4()
    si_paper_id = uuid4()
    service = DFTCandidatePreviewService(ReadOnlySessionGuard())
    preview = service.preview_candidates(
        paper_id=main_paper_id,
        paper_code="P0012",
        candidates=[
            _candidate(source_type="stage2", source_paper_id=str(main_paper_id)),
            _candidate(source_type="si", source_paper_id=str(si_paper_id)),
            _candidate(
                source_type="figure_review",
                source_paper_id=str(main_paper_id),
                material_identity=None,
            ),
        ],
    )

    exact = [cluster for cluster in preview["clusters"] if cluster["cluster_type"] == "EXACT_OBSERVATION"]
    incomplete = [
        cluster for cluster in preview["clusters"] if cluster["cluster_type"] == "INCOMPLETE_IDENTITY"
    ]
    assert len(exact) == 1
    assert exact[0]["candidate_count"] == 2
    assert {item["source_paper_id"] for item in exact[0]["candidates"]} == {
        str(main_paper_id),
        str(si_paper_id),
    }
    assert len(incomplete) == 1
    assert incomplete[0]["candidate_count"] == 1
    assert "missing_material_identity" in incomplete[0]["identity_warnings"]
    assert preview["summary"]["incomplete_identity"] == 1


def test_completed_review_selector_ignores_stale_and_external_run_audits():
    current_fingerprint = "c" * 64
    stale = AuditLog(
        id=uuid4(),
        action="offline_evidence_review_applied",
        source="test",
        payload={
            "stage_status": "completed",
            "completed_snapshot_fingerprint": "a" * 64,
            "dft_evidence_candidates": [{"value": -9.99}],
        },
    )
    external_run = AuditLog(
        id=uuid4(),
        action="offline_evidence_review_applied",
        source="test",
        payload={
            "stage_status": "completed",
            "run_id": str(uuid4()),
            "completed_snapshot_fingerprint": current_fingerprint,
            "dft_evidence_candidates": [{"value": -8.88}],
        },
    )
    current = AuditLog(
        id=uuid4(),
        action="offline_evidence_review_applied",
        source="test",
        payload={
            "stage_status": "completed",
            "completed_snapshot_fingerprint": current_fingerprint,
            "dft_evidence_candidates": [{"value": -1.26}],
        },
    )

    selected = DFTCandidatePreviewService._select_current_completed_review_payload(
        [stale, external_run, current],
        completed_snapshot_fingerprint=current_fingerprint,
    )

    assert selected is not None
    assert selected["dft_evidence_candidates"] == [{"value": -1.26}]
    assert selected["review_run_id"] == str(current.id)
