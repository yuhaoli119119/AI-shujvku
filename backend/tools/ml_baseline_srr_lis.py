from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


TARGET_ALIASES = {
    "adsorption_energy": "adsorption_energy",
    "srr_lis:adsorption_energy": "adsorption_energy",
    "reaction_barrier": "reaction_barrier",
    "srr_lis:reaction_barrier": "reaction_barrier",
    "rds_gibbs_free_energy": "gibbs_free_energy_change",
    "srr_lis:rds_gibbs_free_energy": "gibbs_free_energy_change",
}
FEATURE_COLUMNS = (
    "catalyst_type",
    "metal_centers",
    "coordination",
    "support",
    "intermediate",
    "reaction_step",
    "dft_functional",
)
SPLIT_KEY = "split_paper_id"


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _bool_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.lower().isin({"1", "true", "yes", "y"})


def _target_property(target: str) -> str:
    key = _normalize_token(target)
    if key not in TARGET_ALIASES:
        raise ValueError(f"Unsupported target: {target!r}")
    return TARGET_ALIASES[key]


def _deterministic_group_split(groups: Sequence[str]) -> tuple[set[str], set[str]]:
    ordered = sorted({str(group) for group in groups if str(group).strip()})
    if len(ordered) < 2:
        return set(), set()
    test_count = max(1, len(ordered) // 3)
    test_groups = set(ordered[-test_count:])
    train_groups = set(ordered[:-test_count])
    return train_groups, test_groups


def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)))


def _one_hot_features(train: pd.DataFrame, test: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[str]]:
    train_features = train.loc[:, FEATURE_COLUMNS].fillna("__missing__").astype(str)
    test_features = test.loc[:, FEATURE_COLUMNS].fillna("__missing__").astype(str)
    encoded_train = pd.get_dummies(train_features, columns=list(FEATURE_COLUMNS), dtype=float)
    encoded_test = pd.get_dummies(test_features, columns=list(FEATURE_COLUMNS), dtype=float)
    encoded_train, encoded_test = encoded_train.align(encoded_test, join="left", axis=1, fill_value=0.0)
    return encoded_train.to_numpy(float), encoded_test.to_numpy(float), list(encoded_train.columns)


def _ridge_predict(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    *,
    alpha: float = 1.0,
) -> np.ndarray:
    train_design = np.column_stack([np.ones(len(x_train)), x_train])
    test_design = np.column_stack([np.ones(len(x_test)), x_test])
    penalty = np.eye(train_design.shape[1]) * alpha
    penalty[0, 0] = 0.0
    weights = np.linalg.pinv(train_design.T @ train_design + penalty) @ train_design.T @ y_train
    return test_design @ weights


def run_baseline(csv_path: str | Path, *, target: str) -> dict[str, Any]:
    target_property = _target_property(target)
    warnings: list[str] = []
    csv_path = Path(csv_path)
    frame = pd.read_csv(csv_path)

    required_columns = {
        "label_ready",
        "tabular_ml_ready",
        "canonical_property_type",
        "normalized_value",
        SPLIT_KEY,
        *FEATURE_COLUMNS,
    }
    missing_columns = sorted(required_columns - set(frame.columns))
    if missing_columns:
        return {
            "status": "skipped",
            "target": target_property,
            "n_rows": 0,
            "n_train": 0,
            "n_test": 0,
            "split_key": SPLIT_KEY,
            "feature_columns": list(FEATURE_COLUMNS),
            "baseline_mae": None,
            "ridge_mae": None,
            "warnings": [f"missing_columns:{','.join(missing_columns)}"],
        }

    filtered = frame[
        _bool_series(frame["label_ready"])
        & _bool_series(frame["tabular_ml_ready"])
        & (frame["canonical_property_type"].astype(str) == target_property)
    ].copy()
    filtered["normalized_value"] = pd.to_numeric(filtered["normalized_value"], errors="coerce")
    filtered = filtered.dropna(subset=["normalized_value", SPLIT_KEY])
    if len(filtered) != len(frame):
        warnings.append(f"filtered_rows:{len(frame) - len(filtered)}")

    train_groups, test_groups = _deterministic_group_split(filtered[SPLIT_KEY].astype(str).tolist())
    if not train_groups or not test_groups:
        warnings.append("insufficient_split_groups")
        return {
            "status": "insufficient",
            "target": target_property,
            "n_rows": int(len(filtered)),
            "n_train": 0,
            "n_test": 0,
            "split_key": SPLIT_KEY,
            "feature_columns": list(FEATURE_COLUMNS),
            "baseline_mae": None,
            "ridge_mae": None,
            "warnings": warnings,
            "train_groups": [],
            "test_groups": sorted(test_groups),
        }

    train = filtered[filtered[SPLIT_KEY].astype(str).isin(train_groups)].copy()
    test = filtered[filtered[SPLIT_KEY].astype(str).isin(test_groups)].copy()
    if train.empty or test.empty:
        warnings.append("insufficient_train_or_test_rows")
        return {
            "status": "insufficient",
            "target": target_property,
            "n_rows": int(len(filtered)),
            "n_train": int(len(train)),
            "n_test": int(len(test)),
            "split_key": SPLIT_KEY,
            "feature_columns": list(FEATURE_COLUMNS),
            "baseline_mae": None,
            "ridge_mae": None,
            "warnings": warnings,
            "train_groups": sorted(train_groups),
            "test_groups": sorted(test_groups),
        }

    y_train = train["normalized_value"].to_numpy(float)
    y_test = test["normalized_value"].to_numpy(float)
    mean_prediction = np.full(shape=len(test), fill_value=float(np.mean(y_train)))
    baseline_mae = _mae(y_test, mean_prediction)

    ridge_mae: float | None = None
    try:
        x_train, x_test, _expanded_columns = _one_hot_features(train, test)
        ridge_prediction = _ridge_predict(x_train, y_train, x_test)
        ridge_mae = _mae(y_test, ridge_prediction)
    except (ValueError, np.linalg.LinAlgError) as exc:
        warnings.append(f"ridge_skipped:{exc.__class__.__name__}")

    return {
        "status": "ok",
        "target": target_property,
        "n_rows": int(len(filtered)),
        "n_train": int(len(train)),
        "n_test": int(len(test)),
        "split_key": SPLIT_KEY,
        "feature_columns": list(FEATURE_COLUMNS),
        "baseline_mae": baseline_mae,
        "ridge_mae": ridge_mae,
        "warnings": warnings,
        "train_groups": sorted(train_groups),
        "test_groups": sorted(test_groups),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a minimal SRR_LiS tabular ML baseline from v3 CSV export.")
    parser.add_argument("--csv", required=True, type=Path, help="Path to /api/dft/ml-dataset-v3.csv output.")
    parser.add_argument(
        "--target",
        required=True,
        choices=sorted(TARGET_ALIASES),
        help="Task target to model, for example adsorption_energy.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_baseline(args.csv, target=args.target)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
