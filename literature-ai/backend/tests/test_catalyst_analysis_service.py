from __future__ import annotations

from collections import Counter
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.models import CatalystSample, DFTResult, Paper
from app.main import app
from app.services.catalyst_analysis_service import (
    CatalystAnalysisService,
    _ReadyRow,
    _bond_atom_pair,
    _candidate_groups,
    _field_matches,
    _is_li2s_barrier,
    _is_li2s_charge_transfer,
    _pair_analysis_record_exclusion,
    _pairing_contexts_compatible,
    _resolve_group,
    _stats,
)


pytestmark = pytest.mark.no_test_database


def _ready(
    *,
    catalyst: CatalystSample,
    paper: Paper,
    property_type: str,
    value: float,
    adsorbate: str | None = None,
    reaction_step: str | None = None,
    atom_pair: str | None = None,
    record_id=None,
    setting_id: str = "setting-1",
    active_site_instance_key: str | None = None,
    site_label: str | None = None,
    property_context: dict | None = None,
):
    subject = {"canonical_atom_pair": atom_pair} if atom_pair else {}
    if active_site_instance_key is not None:
        subject["active_site_instance_key"] = active_site_instance_key
    if site_label is not None:
        subject["site_label"] = site_label
    if property_context is not None:
        subject["property_context"] = property_context
    row = DFTResult(
        id=record_id or uuid4(),
        paper_id=paper.id,
        catalyst_sample_id=catalyst.id,
        property_type=property_type,
        value=value,
        unit="eV" if "bond" not in property_type else "angstrom",
        adsorbate=adsorbate,
        reaction_step=reaction_step,
        evidence_text=reaction_step,
        identity_version=2,
        identity_payload={
            "identity_version": 2,
            "subject": subject,
        },
    )
    record = {
        "record_id": str(row.id),
        "is_ml_ready": True,
        "target": {"normalized_value": value, "normalized_unit": row.unit},
        "linked_dft_setting": {"dft_setting_id": setting_id, "functional": "PBE"},
    }
    return _ReadyRow(row=row, paper=paper, record=record, catalyst=catalyst)


def _paper_and_catalyst(index: int):
    paper = Paper(id=uuid4(), paper_code=f"B{index:04d}", title=f"Paper {index}", doi=f"10.1000/{index}")
    catalyst = CatalystSample(id=uuid4(), paper_id=paper.id, name=f"Catalyst {index}")
    return paper, catalyst


def test_analysis_fields_api_uses_public_dissociation_key():
    payload = TestClient(app).get("/api/visuals/analysis-fields").json()
    fields = {item["field"]: item for item in payload["fields"]}
    assert "li2s_dissociation_barrier" in fields
    assert "li2s_decomposition_barrier" not in fields
    assert fields["li2s_dissociation_barrier"]["unit"] == "eV"
    assert fields["li2s_dissociation_barrier"]["type"] == "number"


def test_pair_analysis_accepts_reviewed_legacy_numeric_target_without_generic_descriptor_readiness():
    paper, catalyst = _paper_and_catalyst(99)
    ready = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="adsorption_energy",
        value=-2.4,
        adsorbate="Li2S4",
    )
    ready.row.identity_version = None
    ready.record["is_ml_ready"] = False
    ready.record["target"]["normalization_status"] = "normalized"
    ready.record["setting_link_status"] = "clear_primary"
    assert _pair_analysis_record_exclusion(ready.row, ready.record) is None

    ready.record["setting_link_status"] = "ambiguous"
    assert _pair_analysis_record_exclusion(ready.row, ready.record) == "missing_or_ambiguous_calculation_context"
    ready.record["setting_link_status"] = "clear_primary"
    ready.record["target"]["normalization_status"] = "unsupported_unit"
    assert _pair_analysis_record_exclusion(ready.row, ready.record) == "pair_analysis_target_not_normalized"


def test_li2s_barrier_and_charge_rules_are_species_specific():
    paper, catalyst = _paper_and_catalyst(1)
    generic = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="reaction_barrier",
        value=1.7,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation path",
    )
    bond = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="bond_length",
        value=2.1,
        adsorbate="Li2S",
        atom_pair="li1-s",
    )
    li2s_charge = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="bader_charge_transfer",
        value=-0.4,
        adsorbate="Li2S",
    )
    s8_charge = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="bader_charge_transfer",
        value=-0.3,
        adsorbate="S8",
    )
    assert _is_li2s_barrier(generic.row)
    assert not _is_li2s_barrier(bond.row)
    assert _is_li2s_charge_transfer(li2s_charge.row)
    assert not _is_li2s_charge_transfer(s8_charge.row)
    assert _bond_atom_pair(bond.row) == "li1_s"


def test_barrier_max_bond_max_and_duplicate_provenance_are_deterministic():
    paper, catalyst = _paper_and_catalyst(1)
    barrier_a = _ready(catalyst=catalyst, paper=paper, property_type="reaction_barrier", value=1.2, adsorbate="Li2S", reaction_step="Li2S dissociation path A")
    barrier_b = _ready(catalyst=catalyst, paper=paper, property_type="li2s_decomposition_barrier", value=1.8, adsorbate="Li2S", reaction_step="Li2S dissociation path B")
    li1 = _ready(catalyst=catalyst, paper=paper, property_type="bond_length", value=2.0, adsorbate="Li2S", atom_pair="li1-s")
    li2 = _ready(catalyst=catalyst, paper=paper, property_type="bond_length", value=2.4, adsorbate="Li2S", atom_pair="li2-s")
    groups = _candidate_groups("li2s_dissociation_barrier", [barrier_a, barrier_b])
    assert len(groups) == 1
    assert _field_matches("li2s_dissociation_barrier", barrier_a)
    selected, reason, _ = _resolve_group("li2s_dissociation_barrier", groups[0])
    assert reason is None
    assert selected["value"] == 1.8
    candidates_by_path = {item["reaction_step"]: item for item in selected["candidates"]}
    assert candidates_by_path["Li2S dissociation path A"]["property_type"] == "reaction_barrier"
    assert candidates_by_path["Li2S dissociation path A"]["paper"]["paper_id"] == str(paper.id)
    assert candidates_by_path["Li2S dissociation path A"]["selected_for_summary"] is False
    assert candidates_by_path["Li2S dissociation path A"]["selected_for_regression"] is False
    assert candidates_by_path["Li2S dissociation path B"]["selected_for_summary"] is True
    assert candidates_by_path["Li2S dissociation path B"]["selected_for_regression"] is True
    max_groups = _candidate_groups("li_s_bond_max", [li1, li2])
    assert len(max_groups) == 1
    assert max_groups[0][0].value == 2.4
    assert set(max_groups[0][0].source_ids) == {str(li1.row.id), str(li2.row.id)}


def test_barrier_paths_group_by_pairing_context_and_select_maximum():
    paper, catalyst = _paper_and_catalyst(2)
    path_a = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="reaction_barrier",
        value=1.2,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation A",
        active_site_instance_key="site-1",
        site_label="bridge",
        property_context={"pathway": "pathway A", "initial_state": "Li2S", "transition_state": "TS-A", "final_state": "2LiS"},
    )
    path_b = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="reaction_barrier",
        value=1.8,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation B",
        active_site_instance_key="site-1",
        site_label="bridge",
        property_context={"pathway": "pathway B", "initial_state": "Li2S", "transition_state": "TS-B", "final_state": "2LiS"},
    )
    groups = _candidate_groups("li2s_dissociation_barrier", [path_a, path_b])
    assert len(groups) == 1
    selected, reason, candidates = _resolve_group("li2s_dissociation_barrier", groups[0])
    assert reason is None
    assert selected["value"] == 1.8
    assert selected["context"] == {
        "dft_setting_id": "setting-1",
        "functional": "PBE",
        "active_site_instance_key": "site-1",
        "site_label": "bridge",
    }
    by_path = {candidate["pathway"]: candidate for candidate in candidates}
    assert by_path["pathway A"]["selected_for_summary"] is False
    assert by_path["pathway A"]["selected_for_regression"] is False
    assert by_path["pathway B"]["selected_for_summary"] is True
    assert by_path["pathway B"]["selected_for_regression"] is True
    assert by_path["pathway A"]["context"]["transition_state"] == "TS-A"

    different_setting = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="reaction_barrier",
        value=2.0,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation C",
        setting_id="setting-2",
        active_site_instance_key="site-1",
        site_label="bridge",
        property_context={"pathway": "pathway C"},
    )
    different_site = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="reaction_barrier",
        value=2.1,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation D",
        active_site_instance_key="site-2",
        site_label="top",
        property_context={"pathway": "pathway D"},
    )
    assert len(_candidate_groups("li2s_dissociation_barrier", [path_a, different_setting])) == 2
    assert len(_candidate_groups("li2s_dissociation_barrier", [path_a, different_site])) == 2


def test_correlation_pairs_only_same_catalyst_and_keeps_conflicts_out():
    ready = []
    exclusions = Counter({"safety_gate:missing_review": 1, "identity_v2_required": 1})
    for index, (x_value, y_value) in enumerate(((1.0, 2.0), (2.0, 4.0), (3.0, 6.0)), 1):
        paper, catalyst = _paper_and_catalyst(index)
        ready.extend(
            [
                _ready(catalyst=catalyst, paper=paper, property_type="d_band_center", value=x_value),
                _ready(catalyst=catalyst, paper=paper, property_type="adsorption_energy", value=y_value, adsorbate="Li2S"),
            ]
        )
    missing_x_paper, missing_x_catalyst = _paper_and_catalyst(9)
    missing_y_paper, missing_y_catalyst = _paper_and_catalyst(10)
    ready.append(_ready(catalyst=missing_x_catalyst, paper=missing_x_paper, property_type="d_band_center", value=9.0))
    ready.append(_ready(catalyst=missing_y_catalyst, paper=missing_y_paper, property_type="adsorption_energy", value=10.0, adsorbate="Li2S"))
    conflict_paper, conflict_catalyst = _paper_and_catalyst(11)
    ready.extend(
        [
            _ready(catalyst=conflict_catalyst, paper=conflict_paper, property_type="d_band_center", value=1.0),
            _ready(catalyst=conflict_catalyst, paper=conflict_paper, property_type="d_band_center", value=2.0),
            _ready(catalyst=conflict_catalyst, paper=conflict_paper, property_type="adsorption_energy", value=3.0, adsorbate="Li2S"),
        ]
    )

    service = CatalystAnalysisService(None)
    service._load_pair_analysis_rows = lambda _library: (ready, exclusions, {})
    payload = service.correlation(library_name="unit", x_field="d_band_center", y_field="li2s_adsorption_energy", min_n=3)
    assert payload["n_catalysts"] == 3
    assert payload["n_papers"] == 3
    assert payload["statistics"]["pearson"] == 1.0
    assert payload["statistics"]["spearman"] == 1.0
    assert payload["statistics"]["r_squared"] == 1.0
    assert payload["statistics"]["slope"] == 2.0
    assert payload["statistics"]["intercept"] == 0.0
    assert payload["excluded_reasons"]["missing_x_field_value"] == 1
    assert payload["excluded_reasons"]["missing_y_field_value"] == 1
    assert payload["excluded_reasons"].get("context_mismatch", 0) == 0
    assert payload["excluded_reasons"]["conflicting_values"] == 1
    assert payload["min_n"] == 3
    assert payload["excluded_reasons_are_overlapping"] is True
    small_payload = service.correlation(library_name="unit", x_field="d_band_center", y_field="li2s_adsorption_energy", min_n=4)
    assert small_payload["n_catalysts"] == 3
    assert small_payload["n_papers"] == 3
    assert "min_n_not_reached" in small_payload["warnings"]
    assert small_payload["statistics"]["pearson"] is None


def test_cross_field_pairing_ignores_property_state_but_requires_setting_and_site():
    paper, catalyst = _paper_and_catalyst(20)
    adsorption = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="adsorption_energy",
        value=-1.0,
        adsorbate="Li2S",
        reaction_step="Li2S adsorption",
        active_site_instance_key="site-1",
        site_label="bridge",
    )
    barrier = _ready(
        catalyst=catalyst,
        paper=paper,
        property_type="reaction_barrier",
        value=1.5,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation",
        active_site_instance_key="site-1",
        site_label="bridge",
    )
    service = CatalystAnalysisService(None)
    service._load_pair_analysis_rows = lambda _library: ([adsorption, barrier], Counter(), {})
    payload = service.correlation(
        library_name="unit",
        x_field="li2s_adsorption_energy",
        y_field="li2s_dissociation_barrier",
        min_n=3,
    )
    assert payload["n_catalysts"] == 1
    assert payload["excluded_reasons"].get("context_mismatch", 0) == 0

    for changed in (
        {"setting_id": "setting-2", "active_site_instance_key": "site-1", "site_label": "bridge"},
        {"setting_id": "setting-1", "active_site_instance_key": "site-2", "site_label": "top"},
    ):
        incompatible_barrier = _ready(
            catalyst=catalyst,
            paper=paper,
            property_type="reaction_barrier",
            value=1.5,
            adsorbate="Li2S",
            reaction_step="Li2S dissociation",
            **changed,
        )
        service._load_pair_analysis_rows = lambda _library, incompatible_barrier=incompatible_barrier: (
            [adsorption, incompatible_barrier],
            Counter(),
            {},
        )
        incompatible = service.correlation(
            library_name="unit",
            x_field="li2s_adsorption_energy",
            y_field="li2s_dissociation_barrier",
            min_n=3,
        )
        assert incompatible["n_catalysts"] == 0
        assert incompatible["excluded_reasons"]["context_mismatch"] == 1


def test_equal_duplicates_dedupe_and_keep_all_source_ids():
    paper, catalyst = _paper_and_catalyst(30)
    first = _ready(catalyst=catalyst, paper=paper, property_type="d_band_center", value=1.0)
    duplicate = _ready(catalyst=catalyst, paper=paper, property_type="d_band_center", value=1.0)
    y_value = _ready(catalyst=catalyst, paper=paper, property_type="adsorption_energy", value=-1.0, adsorbate="Li2S")
    service = CatalystAnalysisService(None)
    service._load_pair_analysis_rows = lambda _library: ([first, duplicate, y_value], Counter(), {})
    payload = service.correlation(library_name="unit", x_field="d_band_center", y_field="li2s_adsorption_energy")
    assert payload["n_catalysts"] == 1
    point = payload["points"][0]
    assert set(point["x_source_record_ids"]) == {str(first.row.id), str(duplicate.row.id)}
    assert point["x"]["selection_reason"] == "deduplicated_equal_values"


def test_multiple_comparable_contexts_do_not_form_cartesian_product():
    paper, catalyst = _paper_and_catalyst(31)
    ready = [
        _ready(catalyst=catalyst, paper=paper, property_type="d_band_center", value=1.0, setting_id="setting-1"),
        _ready(catalyst=catalyst, paper=paper, property_type="d_band_center", value=2.0, setting_id="setting-2"),
        _ready(catalyst=catalyst, paper=paper, property_type="adsorption_energy", value=-1.0, adsorbate="Li2S", setting_id="setting-1"),
        _ready(catalyst=catalyst, paper=paper, property_type="adsorption_energy", value=-2.0, adsorbate="Li2S", setting_id="setting-2"),
    ]
    service = CatalystAnalysisService(None)
    service._load_pair_analysis_rows = lambda _library: (ready, Counter(), {})
    payload = service.correlation(library_name="unit", x_field="d_band_center", y_field="li2s_adsorption_energy")
    assert payload["n_catalysts"] == 0
    assert payload["excluded_reasons"]["multiple_comparable_contexts"] == 1
    assert len(payload["excluded_details"][0]["x_candidates"]) == 2
    assert len(payload["excluded_details"][0]["y_candidates"]) == 2


def test_correlation_reports_when_a_pair_uses_reviewed_legacy_identity_rows():
    paper, catalyst = _paper_and_catalyst(33)
    legacy_x = _ready(catalyst=catalyst, paper=paper, property_type="adsorption_energy", value=-2.0, adsorbate="Li2S")
    v2_y = _ready(catalyst=catalyst, paper=paper, property_type="adsorption_energy", value=-1.2, adsorbate="Li2S4")
    legacy_x.row.identity_version = None
    service = CatalystAnalysisService(None)
    service._load_pair_analysis_rows = lambda _library: ([legacy_x, v2_y], Counter(), {})

    payload = service.correlation(
        library_name="unit",
        x_field="li2s_adsorption_energy",
        y_field="li2s4_adsorption_energy",
    )

    assert payload["n_catalysts"] == 1
    assert payload["legacy_identity_point_count"] == 1
    assert payload["identity_v2_only_point_count"] == 0
    assert payload["points"][0]["uses_legacy_identity"] is True
    assert payload["points"][0]["legacy_identity_source_record_ids"] == [str(legacy_x.row.id)]
    assert payload["points"][0]["x"]["candidates"][0]["identity_version"] is None


def test_pairing_context_excludes_property_state_fields():
    assert _pairing_contexts_compatible(
        {
            "dft_setting_id": "setting-1",
            "active_site_instance_key": "site-1",
            "reaction_step": "Li2S adsorption",
            "initial_state": "adsorbed",
        },
        {
            "dft_setting_id": "setting-1",
            "active_site_instance_key": "site-1",
            "reaction_step": "Li2S dissociation",
            "transition_state": "saddle",
        },
    )


def test_metadata_analysis_field_is_rejected_as_nonnumeric():
    service = CatalystAnalysisService(None)
    with pytest.raises(ValueError, match="numeric"):
        service.correlation(library_name=None, x_field="catalyst_name", y_field="d_band_center")


def test_overview_keeps_exportable_and_correlation_ready_counts_distinct():
    paper, catalyst = _paper_and_catalyst(32)
    ready = [_ready(catalyst=catalyst, paper=paper, property_type="d_band_center", value=1.0)]
    service = CatalystAnalysisService(None)
    service._load_ready_rows = lambda _library: (
        ready,
        Counter(),
        {
            "total_dft_rows": 5,
            "exportable_dft_rows": 4,
            "v2_row_ready_numeric_rows": 1,
            "distinct_exportable_catalysts": 2,
        },
    )
    overview = service.overview_counts(None)
    assert overview["total_dft_rows"] == 5
    assert overview["exportable_dft_rows"] == 4
    assert overview["v2_row_ready_numeric_rows"] == 1
    assert overview["distinct_exportable_catalysts"] == 2
    assert overview["contributing_papers"] == 1


def test_invalid_analysis_field_is_clear():
    service = CatalystAnalysisService(None)
    with pytest.raises(ValueError, match="unknown x_field"):
        service.correlation(library_name=None, x_field="not_a_field", y_field="d_band_center")


def test_stats_returns_no_values_for_insufficient_points():
    assert _stats([]) == {"pearson": None, "spearman": None, "r_squared": None, "slope": None, "intercept": None}
