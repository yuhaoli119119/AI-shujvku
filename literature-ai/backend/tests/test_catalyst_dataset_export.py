from __future__ import annotations

from collections import Counter
import csv
import io
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.models import CatalystSample, DFTResult, Paper
from app.db.session import get_db_session
from app.main import app
from app.services.catalyst_analysis_service import (
    CATALYST_WIDE_COLUMNS,
    CATALYST_WIDE_SCHEMA_VERSION,
    CatalystAnalysisService,
    _ReadyRow,
)


pytestmark = pytest.mark.no_test_database


EXPECTED_COLUMNS = [
    "catalyst_name",
    "catalyst_sample_id",
    "paper_code",
    "paper_id",
    "doi",
    "catalyst_type",
    "metal_centers",
    "coordination",
    "support",
    "functional",
    "s8_adsorption_energy",
    "li2s8_adsorption_energy",
    "li2s6_adsorption_energy",
    "li2s4_adsorption_energy",
    "li2s2_adsorption_energy",
    "li2s_adsorption_energy",
    "li2s_dissociation_barrier",
    "li2s_bader_charge_transfer",
    "li1_s_bond_length",
    "li2_s_bond_length",
    "li_s_bond_max",
    "d_band_center",
    "rds_delta_g",
]


def _paper_catalyst(index: int = 1, *, name: str | None = None):
    paper = Paper(
        id=uuid4(),
        paper_code=f"B{index:04d}",
        title=f"Paper {index}",
        doi=f"10.1000/{index}",
        library_name="测试库",
    )
    catalyst = CatalystSample(
        id=uuid4(),
        paper_id=paper.id,
        name=name or f"Catalyst {index}",
        catalyst_type="SAC",
        metal_centers=["Ni", "Fe"],
        coordination="M-N4",
        support="carbon, sheet",
    )
    return paper, catalyst


def _ready(
    *,
    paper: Paper,
    catalyst: CatalystSample,
    property_type: str,
    value: float,
    adsorbate: str | None = None,
    reaction_step: str | None = None,
    atom_pair: str | None = None,
    setting_id: str = "setting-A",
    functional: str = "PBE",
    site: str = "site-A",
    site_label: str = "bridge",
):
    subject = {
        "active_site_instance_key": site,
        "site_label": site_label,
    }
    if atom_pair:
        subject["canonical_atom_pair"] = atom_pair
    row = DFTResult(
        id=uuid4(),
        paper_id=paper.id,
        catalyst_sample_id=catalyst.id,
        property_type=property_type,
        value=value,
        unit="angstrom" if "bond" in property_type else ("e" if "charge" in property_type else "eV"),
        adsorbate=adsorbate,
        reaction_step=reaction_step,
        evidence_text=reaction_step,
        identity_version=2,
        identity_payload={"identity_version": 2, "subject": subject},
    )
    record = {
        "record_id": str(row.id),
        "is_ml_ready": True,
        "setting_link_status": "clear_primary",
        "target": {"normalized_value": value, "normalized_unit": row.unit},
        "linked_dft_setting": {
            "dft_setting_id": setting_id,
            "functional": functional,
        },
    }
    return _ReadyRow(row=row, paper=paper, record=record, catalyst=catalyst)


def _service_payload(ready: list[_ReadyRow], library_name: str = "测试库"):
    service = CatalystAnalysisService(None)
    service._load_ready_rows = lambda _library: (
        ready,
        Counter({"missing_catalyst_sample_id": 1}),
        {
            "total_dft_rows": len(ready) + 1,
            "exportable_dft_rows": len(ready),
            "v2_row_ready_numeric_rows": len(ready),
            "distinct_exportable_catalysts": len({str(item.catalyst.id) for item in ready}),
        },
    )
    return service, service.catalyst_dataset(library_name)


def test_fixed_wide_columns_conflicts_duplicates_and_csv_are_deterministic():
    paper, catalyst = _paper_catalyst(name='催化剂, "甲"')
    adsorption = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="adsorption_energy",
        value=-1.25,
        adsorbate="Li2S",
    )
    duplicate_a = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="adsorption_energy",
        value=-0.5,
        adsorbate="S8",
    )
    duplicate_b = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="adsorption_energy",
        value=-0.5,
        adsorbate="S8",
    )
    conflict_a = _ready(paper=paper, catalyst=catalyst, property_type="d_band_center", value=-1.0)
    conflict_b = _ready(paper=paper, catalyst=catalyst, property_type="d_band_center", value=-1.4)

    service, payload = _service_payload(
        [adsorption, duplicate_a, duplicate_b, conflict_a, conflict_b]
    )
    assert list(CATALYST_WIDE_COLUMNS) == EXPECTED_COLUMNS
    assert payload["columns"] == EXPECTED_COLUMNS
    assert len(payload["field_definitions"]) == 23
    assert payload["schema_version"] == CATALYST_WIDE_SCHEMA_VERSION
    assert payload["library_name"] == "测试库"
    assert payload["row_count"] == payload["manifest"]["row_count"] == 1
    assert payload["paper_count"] == payload["manifest"]["paper_count"] == 1
    assert list(payload["rows"][0]) == EXPECTED_COLUMNS

    row = payload["rows"][0]
    assert row["catalyst_sample_id"] == str(catalyst.id)
    assert row["li2s_adsorption_energy"] == -1.25
    assert row["d_band_center"] is None
    assert row["metal_centers"] == ["Fe", "Ni"]

    fields = payload["manifest"]["catalysts"][str(catalyst.id)]["fields"]
    assert fields["d_band_center"]["conflict"] is True
    assert fields["d_band_center"]["exclusion_reason"] == "conflicting_values"
    assert {candidate["value"] for candidate in fields["d_band_center"]["candidates"]} == {-1.0, -1.4}
    assert fields["s8_adsorption_energy"]["selection_reason"] == "deduplicated_equal_values"
    assert set(fields["s8_adsorption_energy"]["source_record_ids"]) == {
        str(duplicate_a.row.id),
        str(duplicate_b.row.id),
    }
    assert "missing_fields_left_blank" in payload["warnings"]
    assert "conflicting_fields_left_blank" in payload["warnings"]

    csv_text, csv_payload = service.catalyst_dataset_csv("测试库")
    assert csv_text.startswith("\ufeff")
    parsed = list(csv.DictReader(io.StringIO(csv_text.removeprefix("\ufeff"))))
    assert parsed[0]["catalyst_name"] == '催化剂, "甲"'
    assert parsed[0]["support"] == "carbon, sheet"
    assert parsed[0]["metal_centers"] == '["Fe","Ni"]'
    assert parsed[0]["d_band_center"] == ""
    assert list(parsed[0]) == EXPECTED_COLUMNS
    assert csv_payload["row_count"] == payload["row_count"]
    assert csv_payload["paper_count"] == payload["paper_count"]
    assert csv_payload["library_name"] == payload["library_name"]


def test_each_row_is_one_catalyst_and_plain_missing_values_are_not_conflicts():
    paper_a, catalyst_a = _paper_catalyst(10)
    paper_b, catalyst_b = _paper_catalyst(11)
    only_a = _ready(
        paper=paper_a,
        catalyst=catalyst_a,
        property_type="d_band_center",
        value=-1.1,
    )
    only_b = _ready(
        paper=paper_b,
        catalyst=catalyst_b,
        property_type="adsorption_energy",
        value=-2.2,
        adsorbate="Li2S",
    )

    _service, payload = _service_payload([only_a, only_b])
    assert payload["row_count"] == 2
    assert payload["paper_count"] == 2
    rows = {row["catalyst_sample_id"]: row for row in payload["rows"]}
    assert rows[str(catalyst_a.id)]["d_band_center"] == -1.1
    assert rows[str(catalyst_a.id)]["li2s_adsorption_energy"] is None
    assert rows[str(catalyst_b.id)]["d_band_center"] is None
    assert rows[str(catalyst_b.id)]["li2s_adsorption_energy"] == -2.2
    assert "missing_fields_left_blank" in payload["warnings"]
    assert "conflicting_fields_left_blank" not in payload["warnings"]
    assert "incompatible_row_contexts" not in payload["warnings"]


def test_barrier_bonds_and_li2s_bader_keep_species_and_path_provenance():
    paper, catalyst = _paper_catalyst(2)
    path_a = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="reaction_barrier",
        value=0.8,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation path A",
    )
    path_b = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="li2s_decomposition_barrier",
        value=1.2,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation path B",
    )
    li1 = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="bond_length",
        value=2.1,
        adsorbate="Li2S",
        atom_pair="Li1-S",
        reaction_step="optimized Li1-S geometry",
    )
    li2 = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="bond_length",
        value=2.5,
        adsorbate="Li2S",
        atom_pair="Li2-S",
        reaction_step="separate Li2-S geometry label",
    )
    li2s_charge = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="bader_charge_transfer",
        value=0.42,
        adsorbate="Li2S",
    )
    other_charge = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="bader_charge_transfer",
        value=9.9,
        adsorbate="S8",
    )

    _service, payload = _service_payload([path_a, path_b, li1, li2, li2s_charge, other_charge])
    row = payload["rows"][0]
    assert row["li2s_dissociation_barrier"] == 1.2
    assert row["li1_s_bond_length"] == 2.1
    assert row["li2_s_bond_length"] == 2.5
    assert row["li_s_bond_max"] == 2.5
    assert row["li2s_bader_charge_transfer"] == 0.42

    fields = payload["manifest"]["catalysts"][str(catalyst.id)]["fields"]
    paths = fields["li2s_dissociation_barrier"]["candidates"]
    assert {candidate["reaction_step"] for candidate in paths} == {
        "Li2S dissociation path A",
        "Li2S dissociation path B",
    }
    assert next(candidate for candidate in paths if candidate["value"] == 1.2)["selected_for_summary"] is True
    assert next(candidate for candidate in paths if candidate["value"] == 0.8)["selected_for_summary"] is False
    assert {candidate["source_record_id"] for candidate in fields["li2s_bader_charge_transfer"]["candidates"]} == {
        str(li2s_charge.row.id)
    }
    assert set(fields["li_s_bond_max"]["source_record_ids"]) == {
        str(li1.row.id),
        str(li2.row.id),
    }
    assert fields["li_s_bond_max"]["selection_reason"] == "maximum_of_selected_li_s_bonds"


def test_incompatible_setting_or_site_never_forms_a_mixed_training_row():
    paper, catalyst = _paper_catalyst(3)
    adsorption = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="adsorption_energy",
        value=-1.1,
        adsorbate="Li2S",
        setting_id="setting-A",
        site="site-A",
        site_label="bridge",
    )
    barrier = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="reaction_barrier",
        value=0.9,
        adsorbate="Li2S",
        reaction_step="Li2S dissociation",
        setting_id="setting-B",
        site="site-B",
        site_label="top",
    )
    li1 = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="bond_length",
        value=2.0,
        adsorbate="Li2S",
        atom_pair="Li1-S",
        setting_id="setting-A",
        site="site-A",
    )
    li2 = _ready(
        paper=paper,
        catalyst=catalyst,
        property_type="bond_length",
        value=2.6,
        adsorbate="Li2S",
        atom_pair="Li2-S",
        setting_id="setting-B",
        site="site-B",
    )

    _service, payload = _service_payload([adsorption, barrier, li1, li2])
    row = payload["rows"][0]
    assert not (row["li2s_adsorption_energy"] is not None and row["li2s_dissociation_barrier"] is not None)
    assert row["li2s_adsorption_energy"] is None
    assert row["li2s_dissociation_barrier"] is None
    assert row["li_s_bond_max"] is None
    manifest = payload["manifest"]["catalysts"][str(catalyst.id)]
    assert manifest["fields"]["li2s_adsorption_energy"]["exclusion_reason"] == "incompatible_row_contexts"
    assert manifest["fields"]["li2s_dissociation_barrier"]["exclusion_reason"] == "incompatible_row_contexts"
    assert manifest["warnings"] == ["incompatible_row_contexts"]
    assert "incompatible_row_contexts" in payload["warnings"]


def test_api_contract_and_export_policy(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    payload = {
        "schema_version": CATALYST_WIDE_SCHEMA_VERSION,
        "library_name": "测试库",
        "columns": EXPECTED_COLUMNS,
        "field_definitions": [],
        "row_count": 0,
        "paper_count": 0,
        "rows": [],
        "manifest": {"row_count": 0, "paper_count": 0, "library_name": "测试库"},
        "warnings": [],
        "excluded": {},
    }

    def fake_json(self, library_name=None):
        calls.append(("json", library_name))
        return payload

    def fake_csv(self, library_name=None):
        calls.append(("csv", library_name))
        return "\ufeffcatalyst_name\r\n", payload

    def override_session():
        yield None

    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", "owner-secret")
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(CatalystAnalysisService, "catalyst_dataset", fake_json)
    monkeypatch.setattr(CatalystAnalysisService, "catalyst_dataset_csv", fake_csv)
    app.dependency_overrides[get_db_session] = override_session
    try:
        remote = TestClient(app, client=("192.168.1.20", 50000))
        blocked = remote.get("/api/dft/catalyst-dataset?library_name=%E6%B5%8B%E8%AF%95%E5%BA%93")
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "Exports are disabled by server policy"

        headers = {"X-LitAI-Owner-Token": "owner-secret"}
        json_response = remote.get(
            "/api/dft/catalyst-dataset",
            params={"library_name": "测试库"},
            headers=headers,
        )
        assert json_response.status_code == 200
        assert json_response.headers["content-type"].startswith("application/json")
        assert json_response.json()["schema_version"] == CATALYST_WIDE_SCHEMA_VERSION

        csv_response = remote.get(
            "/api/dft/catalyst-dataset.csv",
            params={"library_name": "测试库"},
            headers=headers,
        )
        assert csv_response.status_code == 200
        assert csv_response.headers["content-type"].startswith("text/csv")
        assert csv_response.headers["content-disposition"] == 'attachment; filename="dft_catalyst_dataset_v1.csv"'
        assert csv_response.content.startswith(b"\xef\xbb\xbf")
        assert calls == [("json", "测试库"), ("csv", "测试库")]

        def invalid_json(self, library_name=None):
            raise ValueError("invalid catalyst dataset scope")

        monkeypatch.setattr(CatalystAnalysisService, "catalyst_dataset", invalid_json)
        invalid = TestClient(app).get("/api/dft/catalyst-dataset")
        assert invalid.status_code == 422
        assert invalid.json()["detail"] == "invalid catalyst dataset scope"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
