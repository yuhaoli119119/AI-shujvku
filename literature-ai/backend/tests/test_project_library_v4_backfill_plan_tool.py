from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "project_library_v4_backfill_plan.py"

pytestmark = pytest.mark.no_test_database


def _load_tool_module():
    spec = importlib.util.spec_from_file_location("project_library_v4_backfill_plan", TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_plan_is_read_only_and_counts_sample_gaps():
    tool = _load_tool_module()
    bundle_payload = {
        "context_key": "li_s_sac_dac",
        "library_name": "锂硫双原子",
        "bundles": [
            {
                "paper_id": "paper-1",
                "paper_title": "Complete DAC sample",
                "catalyst_sample": {
                    "catalyst_sample_id": "cat-1",
                    "name": "FeCo-NC",
                    "catalyst_scope": "DAC",
                },
                "active_site_instances": [
                    {
                        "active_site_instance_key": "site-1",
                        "active_site_ref": {
                            "site_label": "M1-M2",
                            "structure_context": "FeCo-N6 relaxed structure",
                        },
                        "binding_source": "evidence_payload",
                        "properties": {
                            "adsorbate_properties": [
                                {
                                    "canonical_property_type": "adsorption_energy",
                                    "canonical_adsorbate": "Li2S",
                                }
                            ],
                            "reaction_step_properties": [
                                {
                                    "property_subtype": "li2s_decomposition_barrier",
                                    "reaction_step": "RDS Li2S decomposition",
                                    "metal_metal_distance_A": 2.4,
                                }
                            ],
                            "electronic_properties": [
                                {
                                    "canonical_property_type": "charge_transfer",
                                    "charge_transfer_e": -1.1,
                                }
                            ],
                        },
                    }
                ],
            },
            {
                "paper_id": "paper-2",
                "paper_title": "Missing fields sample",
                "catalyst_sample": {
                    "catalyst_sample_id": "cat-2",
                    "name": "Co-NC",
                    "catalyst_scope": "SAC",
                },
                "active_site_instances": [
                    {
                        "active_site_instance_key": "site-2",
                        "active_site_ref": {},
                        "binding_source": "generated",
                        "properties": {
                            "adsorbate_properties": [],
                            "reaction_step_properties": [],
                            "electronic_properties": [],
                        },
                    }
                ],
            },
        ],
    }

    report = tool.build_backfill_plan(
        bundle_payload,
        execution_mode="local_api",
        api_base_url="http://127.0.0.1:8000",
        limit_examples=10,
    )

    assert json.loads(json.dumps(report, ensure_ascii=False)) == report
    assert report["schema_version"] == "project_library_v4_backfill_plan_v1"
    assert report["read_only"] is True
    assert report["database_write_authority"] == "none"
    assert report["submit_endpoint_called"] is False
    assert report["extraction_apply_called"] is False
    assert report["counts"]["sample_count"] == 2
    assert report["counts"]["samples_with_any_gap"] == 1
    assert report["counts"]["missing_active_site_binding_count"] == 1
    assert report["counts"]["missing_structure_context_count"] == 1
    assert report["counts"]["missing_li2s_adsorption_count"] == 1
    assert report["counts"]["missing_li2s_barrier_count"] == 1
    assert report["counts"]["missing_rds_count"] == 1
    assert report["counts"]["missing_bader_or_charge_transfer_count"] == 1
    assert report["counts"]["missing_dac_metal_metal_distance_count"] == 0
    assert report["planned_sample_examples"][0]["active_site_instance_key"] == "site-2"
    assert report["planned_sample_examples"][0]["suggested_actions"][0]["requires_user_evidence"] is True

    csv_rows = tool.backfill_plan_csv_rows(report)
    assert [row["gap"] for row in csv_rows] == [
        "active_site_binding",
        "structure_context",
        "li2s_adsorption",
        "li2s_barrier",
        "rds",
        "bader_or_charge_transfer",
    ]
    assert csv_rows[0]["paper_id"] == "paper-2"
    assert csv_rows[0]["review_action"] == "bind_existing_dft_records"
    assert csv_rows[0]["active_site_context"]
    assert json.loads(csv_rows[0]["active_site_ref_patch"]) == {
        "active_site_context": "<evidence-backed active-site label>"
    }
    assert json.loads(csv_rows[1]["active_site_ref_patch"]) == {
        "structure_context": "<evidence-backed structure/model label>"
    }
    assert csv_rows[2]["property_type"] == "adsorption_energy"
    assert csv_rows[2]["adsorbate"] == "Li2S"
    assert csv_rows[0]["requires_user_evidence"] is True


def test_backfill_plan_writes_utf8_csv_queue(tmp_path):
    tool = _load_tool_module()
    report = {
        "planned_sample_examples": [
            {
                "paper_id": "paper-1",
                "title": "锂硫样本",
                "catalyst_sample_id": "cat-1",
                "catalyst_name": "FeCo-NC",
                "catalyst_scope": "DAC",
                "active_site_instance_key": "site-1",
                "binding_source": "evidence_payload",
                "suggested_actions": [
                    {
                        "gap": "bader_or_charge_transfer",
                        "submit_payload_hint": {
                            "property_type": "charge_transfer",
                            "energy_kind": "electronic_descriptor",
                            "unit": "e",
                        },
                        "requires_user_evidence": True,
                    }
                ],
            }
        ]
    }
    output = tmp_path / "queue.csv"

    tool.write_backfill_plan_csv(report, output)

    text = output.read_text(encoding="utf-8-sig")
    assert "锂硫样本" in text
    assert "charge_transfer" in text
    assert "bader_or_charge_transfer" in text
