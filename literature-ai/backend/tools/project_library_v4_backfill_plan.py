from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


REQUIRED_SAMPLE_FIELDS = (
    "li2s_adsorption",
    "li2s_barrier",
    "rds",
    "bader_or_charge_transfer",
    "dac_metal_metal_distance",
)

LI2S_BARRIER_SUBTYPES = {
    "li2s_decomposition_barrier",
    "li2s_deposition_barrier",
    "li2s_nucleation_barrier",
    "migration_barrier",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Li-S SAC/DAC project-library v4 sample backfill plan."
    )
    parser.add_argument("--context-key", default="li_s_sac_dac")
    parser.add_argument("--library-name", default=None)
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--limit-examples", type=int, default=25)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "exports" / "project_library_v4_backfill_plan.json",
    )
    return parser.parse_args()


def _normalize_api_base_url(value: str) -> str:
    return str(value or "http://127.0.0.1:8000").rstrip("/")


def _api_execution_mode(api_base_url: str) -> str:
    parsed = urllib.parse.urlparse(_normalize_api_base_url(api_base_url))
    hostname = (parsed.hostname or "").lower()
    return "local_api" if hostname in {"127.0.0.1", "localhost", "::1"} else "server_api"


def _bundles_url(*, api_base_url: str, context_key: str, library_name: str | None) -> str:
    params = {"context_key": context_key}
    if library_name:
        params["library_name"] = library_name
    return (
        f"{_normalize_api_base_url(api_base_url)}/api/dft/project-library-bundles?"
        + urllib.parse.urlencode(params)
    )


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as response:
        text = response.read().decode("utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object from {url}")
    return payload


def _token(value: Any) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip().lower()).strip("_")


def _all_properties(instance: dict[str, Any]) -> list[dict[str, Any]]:
    properties = instance.get("properties") or {}
    rows: list[dict[str, Any]] = []
    for group_name in (
        "adsorbate_properties",
        "reaction_step_properties",
        "electronic_properties",
        "structure_properties",
        "other_properties",
    ):
        rows.extend(properties.get(group_name) or [])
    return rows


def _sample_gaps(*, catalyst: dict[str, Any], instance: dict[str, Any]) -> list[str]:
    props = _all_properties(instance)
    gaps: list[str] = []
    has_li2s_adsorption = any(
        prop.get("canonical_property_type") == "adsorption_energy"
        and prop.get("canonical_adsorbate") == "Li2S"
        for prop in props
    )
    has_li2s_barrier = any(prop.get("property_subtype") in LI2S_BARRIER_SUBTYPES for prop in props)
    has_rds = any("rds" in _token(prop.get("reaction_step")) for prop in props)
    has_bader_or_charge = any(
        prop.get("canonical_property_type") in {"bader_charge", "charge_transfer"}
        or prop.get("bader_charge_M1") is not None
        or prop.get("bader_charge_M2") is not None
        or prop.get("charge_transfer_e") is not None
        for prop in props
    )
    has_metal_metal_distance = any(prop.get("metal_metal_distance_A") is not None for prop in props)
    if not has_li2s_adsorption:
        gaps.append("li2s_adsorption")
    if not has_li2s_barrier:
        gaps.append("li2s_barrier")
    if not has_rds:
        gaps.append("rds")
    if not has_bader_or_charge:
        gaps.append("bader_or_charge_transfer")
    if catalyst.get("catalyst_scope") == "DAC" and not has_metal_metal_distance:
        gaps.append("dac_metal_metal_distance")
    return gaps


def _suggested_actions(gaps: list[str]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for gap in gaps:
        if gap == "li2s_adsorption":
            actions.append(
                {
                    "gap": gap,
                    "submit_payload_hint": {
                        "property_type": "adsorption_energy",
                        "adsorbate": "Li2S",
                        "energy_kind": "thermodynamic_energy",
                        "unit": "eV",
                    },
                    "requires_user_evidence": True,
                }
            )
        elif gap == "li2s_barrier":
            actions.append(
                {
                    "gap": gap,
                    "submit_payload_hint": {
                        "property_type": "reaction_barrier",
                        "adsorbate": "Li2S",
                        "energy_kind": "activation_barrier",
                        "unit": "eV",
                    },
                    "requires_user_evidence": True,
                }
            )
        elif gap == "rds":
            actions.append(
                {
                    "gap": gap,
                    "submit_payload_hint": {
                        "property_type": "gibbs_free_energy_change",
                        "energy_kind": "free_energy_change",
                        "unit": "eV",
                    },
                    "requires_user_evidence": True,
                }
            )
        elif gap == "bader_or_charge_transfer":
            actions.append(
                {
                    "gap": gap,
                    "submit_payload_hint": {
                        "property_type": "charge_transfer",
                        "energy_kind": "electronic_descriptor",
                        "unit": "e",
                    },
                    "requires_user_evidence": True,
                }
            )
        elif gap == "dac_metal_metal_distance":
            actions.append(
                {
                    "gap": gap,
                    "submit_payload_hint": {
                        "property_type": "structure_property",
                        "unit": "A",
                    },
                    "requires_user_evidence": True,
                }
            )
    return actions


def build_backfill_plan(
    bundle_payload: dict[str, Any],
    *,
    execution_mode: str,
    api_base_url: str,
    limit_examples: int,
) -> dict[str, Any]:
    gap_counts = Counter()
    sample_count = 0
    samples_with_any_gap = 0
    planned_samples: list[dict[str, Any]] = []
    max_examples = max(0, int(limit_examples))

    for bundle in bundle_payload.get("bundles", []):
        catalyst = bundle.get("catalyst_sample") or {}
        for instance in bundle.get("active_site_instances", []):
            sample_count += 1
            gaps = _sample_gaps(catalyst=catalyst, instance=instance)
            gap_counts.update(gaps)
            if gaps:
                samples_with_any_gap += 1
            if not gaps or len(planned_samples) >= max_examples:
                continue
            planned_samples.append(
                {
                    "paper_id": bundle.get("paper_id"),
                    "title": bundle.get("paper_title"),
                    "catalyst_sample_id": catalyst.get("catalyst_sample_id"),
                    "catalyst_name": catalyst.get("name"),
                    "catalyst_scope": catalyst.get("catalyst_scope"),
                    "active_site_instance_key": instance.get("active_site_instance_key"),
                    "active_site_ref": instance.get("active_site_ref") or {},
                    "binding_source": instance.get("binding_source"),
                    "gaps": gaps,
                    "suggested_actions": _suggested_actions(gaps),
                }
            )

    return {
        "schema_version": "project_library_v4_backfill_plan_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "database_write_authority": "none",
        "submit_endpoint_called": False,
        "extraction_apply_called": False,
        "execution_mode": execution_mode,
        "api_base_url": _normalize_api_base_url(api_base_url),
        "context_key": bundle_payload.get("context_key"),
        "library_name": bundle_payload.get("library_name"),
        "sample_unit": "active_site_instance",
        "counts": {
            "sample_count": sample_count,
            "samples_with_any_gap": samples_with_any_gap,
            **{f"missing_{key}_count": int(gap_counts.get(key, 0)) for key in REQUIRED_SAMPLE_FIELDS},
        },
        "gap_counts": dict(sorted(gap_counts.items())),
        "planned_sample_examples": planned_samples,
        "notes": [
            "This report is a read-only plan. It does not call user-submit or extraction apply.",
            "Every suggested action requires source_text evidence and user_submit_only write authority.",
        ],
    }


def main() -> int:
    args = parse_args()
    api_base_url = _normalize_api_base_url(args.api_base_url)
    url = _bundles_url(
        api_base_url=api_base_url,
        context_key=args.context_key,
        library_name=args.library_name,
    )
    bundle_payload = _fetch_json(url)
    report = build_backfill_plan(
        bundle_payload,
        execution_mode=_api_execution_mode(api_base_url),
        api_base_url=api_base_url,
        limit_examples=args.limit_examples,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(payload)
    print(f"\nWrote read-only backfill plan to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
