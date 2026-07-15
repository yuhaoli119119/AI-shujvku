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
):
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
            "subject": {"canonical_atom_pair": atom_pair} if atom_pair else {},
        },
    )
    record = {
        "record_id": str(row.id),
        "is_ml_ready": True,
        "target": {"normalized_value": value, "normalized_unit": row.unit},
        "linked_dft_setting": {"dft_setting_id": "setting-1", "functional": "PBE"},
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
    max_groups = _candidate_groups("li_s_bond_max", [li1, li2])
    assert len(max_groups) == 1
    assert max_groups[0][0].value == 2.4
    assert set(max_groups[0][0].source_ids) == {str(li1.row.id), str(li2.row.id)}


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
    service._load_ready_rows = lambda _library: (ready, exclusions, {})
    payload = service.correlation(library_name="unit", x_field="d_band_center", y_field="li2s_adsorption_energy", min_n=3)
    assert payload["n_catalysts"] == 3
    assert payload["n_papers"] == 3
    assert payload["statistics"]["pearson"] == 1.0
    assert payload["statistics"]["spearman"] == 1.0
    assert payload["statistics"]["r_squared"] == 1.0
    assert payload["statistics"]["slope"] == 2.0
    assert payload["statistics"]["intercept"] == 0.0
    assert payload["excluded_reasons"]["context_mismatch"] >= 2
    assert payload["excluded_reasons"]["conflicting_values"] == 1


def test_invalid_analysis_field_is_clear():
    service = CatalystAnalysisService(None)
    with pytest.raises(ValueError, match="unknown x_field"):
        service.correlation(library_name=None, x_field="not_a_field", y_field="d_band_center")


def test_stats_returns_no_values_for_insufficient_points():
    assert _stats([]) == {"pearson": None, "spearman": None, "r_squared": None, "slope": None, "intercept": None}
