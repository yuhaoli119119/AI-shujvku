from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from tools.ml_baseline_srr_lis import run_baseline


CSV_COLUMNS = [
    "record_id",
    "paper_id",
    "title",
    "year",
    "catalyst_name",
    "catalyst_type",
    "metal_centers",
    "coordination",
    "support",
    "reaction_type",
    "task_profile",
    "canonical_property_type",
    "normalized_value",
    "normalized_unit",
    "raw_value",
    "raw_unit",
    "adsorbate",
    "intermediate",
    "reaction_step",
    "dft_software",
    "dft_functional",
    "evidence_text",
    "page_locators",
    "label_ready",
    "tabular_ml_ready",
    "label_blockers",
    "feature_blockers",
    "split_paper_id",
    "split_catalyst_family",
    "reaction_profile_version",
    "task_profile_version",
]


def _row(
    record_id: str,
    paper_id: str,
    value: float,
    *,
    ready: bool = True,
    target: str = "adsorption_energy",
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "paper_id": paper_id,
        "title": f"Paper {paper_id}",
        "year": 2025,
        "catalyst_name": f"Fe-N-C {paper_id}",
        "catalyst_type": "single_atom",
        "metal_centers": '["Fe"]',
        "coordination": "Fe-N4",
        "support": "carbon",
        "reaction_type": "SRR_LiS",
        "task_profile": f"SRR_LiS:{target}",
        "canonical_property_type": target,
        "normalized_value": value,
        "normalized_unit": "eV",
        "raw_value": value,
        "raw_unit": "eV",
        "adsorbate": "Li2S4",
        "intermediate": "Li2S4",
        "reaction_step": "Li2S4 adsorption",
        "dft_software": "VASP",
        "dft_functional": "PBE",
        "evidence_text": "not used as a feature",
        "page_locators": "[4]",
        "label_ready": "true" if ready else "false",
        "tabular_ml_ready": "true" if ready else "false",
        "label_blockers": "[]" if ready else '["missing_result_setting_link"]',
        "feature_blockers": "[]" if ready else '["missing_coordination"]',
        "split_paper_id": paper_id,
        "split_catalyst_family": f"family={paper_id}",
        "reaction_profile_version": "reaction_profiles_v1",
        "task_profile_version": "tabular_task_profiles_v1",
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_baseline_loads_fixture_csv_and_uses_group_split(tmp_path):
    csv_path = _write_csv(
        tmp_path / "dataset.csv",
        [
            _row("r1", "paper-a", -1.0),
            _row("r2", "paper-a", -1.1),
            _row("r3", "paper-b", -1.5),
            _row("r4", "paper-c", -2.0),
        ],
    )

    result = run_baseline(csv_path, target="adsorption_energy")

    assert result["status"] == "ok"
    assert result["target"] == "adsorption_energy"
    assert result["n_rows"] == 4
    assert result["n_train"] == 3
    assert result["n_test"] == 1
    assert result["split_key"] == "split_paper_id"
    assert result["baseline_mae"] is not None
    assert result["ridge_mae"] is not None
    assert set(result["train_groups"]).isdisjoint(result["test_groups"])
    assert result["test_groups"] == ["paper-c"]
    assert "title" not in result["feature_columns"]
    assert "evidence_text" not in result["feature_columns"]
    assert "catalyst_type" in result["feature_columns"]
    assert "dft_functional" in result["feature_columns"]


def test_baseline_returns_insufficient_for_small_group_count(tmp_path):
    csv_path = _write_csv(
        tmp_path / "small.csv",
        [
            _row("r1", "paper-a", -1.0),
            _row("r2", "paper-a", -1.1),
        ],
    )

    result = run_baseline(csv_path, target="adsorption_energy")

    assert result["status"] == "insufficient"
    assert result["n_rows"] == 2
    assert result["n_train"] == 0
    assert result["n_test"] == 0
    assert result["baseline_mae"] is None
    assert result["ridge_mae"] is None
    assert "insufficient_split_groups" in result["warnings"]


def test_baseline_filters_blocked_rows_and_target_mismatch(tmp_path):
    csv_path = _write_csv(
        tmp_path / "filtered.csv",
        [
            _row("ready-a", "paper-a", -1.0),
            _row("ready-b", "paper-b", -1.4),
            _row("ready-c", "paper-c", -1.8),
            _row("blocked", "paper-d", -9.9, ready=False),
            _row("wrong-target", "paper-e", 0.7, target="reaction_barrier"),
        ],
    )

    result = run_baseline(csv_path, target="adsorption_energy")

    assert result["status"] == "ok"
    assert result["n_rows"] == 3
    assert result["n_train"] == 2
    assert result["n_test"] == 1
    assert "filtered_rows:2" in result["warnings"]
    assert "paper-d" not in result["train_groups"] + result["test_groups"]
    assert "paper-e" not in result["train_groups"] + result["test_groups"]


def test_baseline_cli_outputs_json(tmp_path):
    csv_path = _write_csv(
        tmp_path / "cli.csv",
        [
            _row("r1", "paper-a", -1.0),
            _row("r2", "paper-b", -1.5),
            _row("r3", "paper-c", -2.0),
        ],
    )
    script = Path(__file__).resolve().parents[1] / "tools" / "ml_baseline_srr_lis.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--csv", str(csv_path), "--target", "adsorption_energy"],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["target"] == "adsorption_energy"
    assert payload["split_key"] == "split_paper_id"
