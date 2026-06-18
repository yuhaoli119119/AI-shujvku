from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings
from app.db.models import CatalystSample, DFTResult, Paper
from app.db.session import get_engine
from app.services.dft_export_service import build_dft_csv_rows, build_dft_ml_dataset
from app.utils.active_database import get_active_database_info
from app.utils.review_safety import bulk_export_gate_results, has_required_material_identity


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit DFT rows that lack direct material/structure identity binding."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of example rows to include per category.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path.",
    )
    return parser.parse_args()


def _catalyst_payload(catalyst: CatalystSample) -> dict[str, Any]:
    return {
        "id": str(catalyst.id),
        "name": catalyst.name,
        "catalyst_type": catalyst.catalyst_type,
        "metal_centers": catalyst.metal_centers,
        "coordination": catalyst.coordination,
        "support": catalyst.support,
    }


def _row_payload(
    row: DFTResult,
    paper: Paper,
    *,
    gate: Any,
    paper_catalysts: list[CatalystSample],
) -> dict[str, Any]:
    return {
        "record_id": str(row.id),
        "paper_id": str(paper.id),
        "title": paper.title,
        "year": paper.year,
        "doi": paper.doi,
        "property_type": row.property_type,
        "value": row.value,
        "unit": row.unit,
        "adsorbate": row.adsorbate,
        "reaction_step": row.reaction_step,
        "source_section": row.source_section,
        "source_figure": row.source_figure,
        "candidate_status": row.candidate_status,
        "review_status": getattr(gate, "review_status", None),
        "provenance_level": getattr(gate, "provenance_level", None),
        "locator_status": getattr(gate, "locator_status", None),
        "blocked_reasons": list(getattr(gate, "reasons", ()) or ()),
        "paper_catalyst_count": len(paper_catalysts),
        "paper_catalysts": [_catalyst_payload(catalyst) for catalyst in paper_catalysts[:5]],
    }


def build_report(*, example_limit: int) -> dict[str, Any]:
    settings = get_settings()
    active_db_info = get_active_database_info()
    engine = get_engine(settings.database_url)
    report: dict[str, Any] = {
        "active_database": active_db_info,
        "database_url": settings.database_url,
    }

    with Session(engine, future=True) as session:
        pairs = session.execute(select(DFTResult, Paper).join(Paper, DFTResult.paper_id == Paper.id)).all()
        dft_rows = [row for row, _paper in pairs]
        gate_by_id = bulk_export_gate_results(session, dft_rows, target_type="dft_results")

        paper_ids = {paper.id for _row, paper in pairs}
        catalysts = (
            session.scalars(select(CatalystSample).where(CatalystSample.paper_id.in_(paper_ids))).all()
            if paper_ids
            else []
        )
        catalysts_by_paper: dict[str, list[CatalystSample]] = defaultdict(list)
        for catalyst in catalysts:
            catalysts_by_paper[str(catalyst.paper_id)].append(catalyst)

        missing_identity_examples: list[dict[str, Any]] = []
        old_export_risk_examples: list[dict[str, Any]] = []
        fallback_risk_examples: list[dict[str, Any]] = []
        per_paper_missing_identity = Counter()
        per_property_missing_identity = Counter()
        missing_identity_total = 0
        missing_direct_link_total = 0
        old_export_risk_total = 0
        fallback_risk_total = 0
        fallback_high_risk_total = 0

        for row, paper in pairs:
            gate = gate_by_id.get(str(row.id))
            paper_catalysts = catalysts_by_paper.get(str(paper.id), [])
            if has_required_material_identity(session, row):
                continue

            missing_identity_total += 1
            per_paper_missing_identity[paper.title or str(paper.id)] += 1
            per_property_missing_identity[row.property_type or "unknown"] += 1

            payload = _row_payload(row, paper, gate=gate, paper_catalysts=paper_catalysts)
            if len(missing_identity_examples) < example_limit:
                missing_identity_examples.append(payload)

            if row.catalyst_sample_id is None:
                missing_direct_link_total += 1

            reasons_wo_material = [reason for reason in payload["blocked_reasons"] if reason != "missing_material_identity"]
            if not reasons_wo_material:
                old_export_risk_total += 1
                if len(old_export_risk_examples) < example_limit:
                    old_export_risk_examples.append(payload)

            if row.catalyst_sample_id is None and paper_catalysts:
                fallback_risk_total += 1
                risk_level = "high" if len(paper_catalysts) > 1 else "medium"
                if risk_level == "high":
                    fallback_high_risk_total += 1
                if len(fallback_risk_examples) < example_limit:
                    risk_payload = dict(payload)
                    risk_payload["fallback_risk_level"] = risk_level
                    fallback_risk_examples.append(risk_payload)

        blank_catalyst_samples = []
        blank_catalyst_total = 0
        for catalyst in catalysts:
            if any(
                (
                    bool((catalyst.name or "").strip()),
                    bool((catalyst.catalyst_type or "").strip()),
                    bool(catalyst.metal_centers),
                    bool((catalyst.coordination or "").strip()),
                    bool((catalyst.support or "").strip()),
                )
            ):
                continue
            blank_catalyst_total += 1
            if len(blank_catalyst_samples) < example_limit:
                paper = next((paper for _row, paper in pairs if paper.id == catalyst.paper_id), None)
                blank_catalyst_samples.append(
                    {
                        "paper_id": str(catalyst.paper_id),
                        "paper_title": paper.title if paper else None,
                        "catalyst": _catalyst_payload(catalyst),
                    }
                )

        csv_export, csv_gate_summary = build_dft_csv_rows(session)
        ml_dataset = build_dft_ml_dataset(session)

    report.update(
        {
            "total_dft_results": len(dft_rows),
            "missing_material_identity_total": missing_identity_total,
            "missing_direct_catalyst_link_total": missing_direct_link_total,
            "would_have_been_exportable_before_fix_total": old_export_risk_total,
            "fallback_binding_risk_total": fallback_risk_total,
            "fallback_binding_high_risk_total": fallback_high_risk_total,
            "blank_identity_catalyst_samples_total": blank_catalyst_total,
            "top_papers_missing_identity": per_paper_missing_identity.most_common(10),
            "missing_identity_by_property_type": per_property_missing_identity.most_common(10),
            "csv_export_gate_summary": csv_gate_summary,
            "csv_export_body_has_data_rows": bool(csv_export.strip().splitlines()[1:]),
            "ml_dataset_metadata": ml_dataset.get("metadata"),
            "examples_missing_identity": missing_identity_examples,
            "examples_old_export_risk": old_export_risk_examples,
            "examples_fallback_risk": fallback_risk_examples,
            "examples_blank_identity_catalyst_samples": blank_catalyst_samples,
        }
    )
    return report


def main() -> int:
    args = parse_args()
    report = build_report(example_limit=max(1, args.limit))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
