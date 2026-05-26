from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import AuditLog, DFTResult, Paper, PaperSection
from app.db.session import get_engine
from app.services.paper_reprocessing import PaperReprocessingService
from app.utils.active_database import activate_active_library_database, require_active_library_sqlite
from app.utils.review_safety import is_export_eligible_extraction
from scripts.audit_ai_workflow_boundary import build_audit


KEYWORD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("activation_barrier", re.compile(r"(activation\s+barrier|reaction\s+barrier|energy\s+barrier|e\s*a\b|e_a\b|ea\b)", re.IGNORECASE)),
    ("adsorption_energy", re.compile(r"(adsorption\s+energy|binding\s+energy|e[_\-\s]*ads|e[_\-\s]*bind)", re.IGNORECASE)),
    ("free_energy", re.compile(r"(delta\s*g|Δg|gibbs\s+free\s+energy|free\s+energy)", re.IGNORECASE)),
    ("electronic_structure", re.compile(r"(d-?band\s+center|density\s+of\s+states|\bdos\b|pdos|bader\s+charge|charge\s+transfer)", re.IGNORECASE)),
    ("electrochemical", re.compile(r"(capacity|cycling|overpotential|tafel|eis|cv|lsv|coulombic)", re.IGNORECASE)),
    ("mechanism", re.compile(r"(li2s|lips|polysulfide|sulfur\s+reduction|conversion|nucleation|decomposition)", re.IGNORECASE)),
]

SNIPPET_WINDOW = 90


def _iter_papers(session: Session, paper_ids: list[UUID] | None = None) -> list[Paper]:
    stmt = select(Paper).order_by(Paper.created_at.asc())
    if paper_ids:
        stmt = stmt.where(Paper.id.in_(paper_ids))
    return list(session.scalars(stmt).all())


def _has_markdown_or_sections(session: Session, paper: Paper) -> bool:
    if paper.markdown_path:
        return True
    return bool(
        session.scalar(
            select(func.count())
            .select_from(PaperSection)
            .where(PaperSection.paper_id == paper.id)
        )
        or 0
    )


def _document_text(document: Any) -> str:
    parts = [getattr(document, "abstract", "") or "", getattr(document, "markdown", "") or ""]
    for section in getattr(document, "sections", []) or []:
        parts.append(getattr(section, "text", "") or "")
    return "\n".join(part for part in parts if part)


def _keyword_snippets(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    snippets: list[dict[str, Any]] = []
    matched_labels: list[str] = []
    seen: set[tuple[str, str]] = set()
    normalized = re.sub(r"\s+", " ", text)
    for label, pattern in KEYWORD_RULES:
        for match in pattern.finditer(normalized):
            matched_labels.append(label)
            start = max(0, match.start() - SNIPPET_WINDOW)
            end = min(len(normalized), match.end() + SNIPPET_WINDOW)
            snippet = normalized[start:end].strip()
            key = (label, snippet[:160])
            if key in seen:
                continue
            seen.add(key)
            snippets.append({"label": label, "snippet": snippet})
    return snippets, sorted(set(matched_labels))


def _extract_paper_coverage(session: Session, service: PaperReprocessingService, paper: Paper) -> dict[str, Any]:
    base = {
        "paper_id": str(paper.id),
        "title": paper.title,
        "has_markdown_or_sections": _has_markdown_or_sections(session, paper),
        "extractable": False,
        "property_types": [],
        "dft_results_count": 0,
        "electrochemical_items_count": 0,
        "keyword_labels": [],
        "candidate_evidence_snippets": [],
        "failure_reasons": [],
    }
    try:
        document = service._rebuild_document(paper)
    except Exception as exc:
        base["failure_reasons"] = [f"rebuild_error:{type(exc).__name__}"]
        return base

    text = _document_text(document)
    snippets, labels = _keyword_snippets(text)
    base["keyword_labels"] = labels
    base["candidate_evidence_snippets"] = snippets[:12]

    if not text.strip():
        base["failure_reasons"] = ["missing_text_artifact"]
        return base
    if not snippets:
        base["failure_reasons"] = ["missing_dft_keywords"]
        return base

    dft_results = service.pipeline.dft_results_extractor.extract(document)
    electrochemical_items = service.pipeline.electrochemical_extractor.extract(document)
    property_types = sorted({str(item.get("category") or "") for item in dft_results if item.get("category")})

    base["dft_results_count"] = len(dft_results)
    base["electrochemical_items_count"] = len(electrochemical_items)
    base["property_types"] = property_types
    base["extractable"] = bool(dft_results)
    if not dft_results:
        base["failure_reasons"] = ["no_supported_dft_pattern"]
        return base

    base["sample_results"] = [
        {
            "property_type": item.get("category"),
            "adsorbate": item.get("adsorbate"),
            "value": item.get("value"),
            "unit": item.get("unit"),
            "evidence_text": item.get("evidence_text"),
            "page": (item.get("source_location") or {}).get("page"),
            "bbox": (item.get("source_location") or {}).get("bbox"),
        }
        for item in dft_results[:5]
    ]
    return base


def build_coverage_report(session: Session, *, paper_ids: list[UUID] | None = None) -> dict[str, Any]:
    settings = get_settings()
    service = PaperReprocessingService(session, settings)
    papers = _iter_papers(session, paper_ids)
    per_paper = [_extract_paper_coverage(session, service, paper) for paper in papers]

    failure_counts = Counter(reason for row in per_paper for reason in row.get("failure_reasons", []))
    property_types = sorted({ptype for row in per_paper for ptype in row.get("property_types", [])})

    active_info = require_active_library_sqlite()
    return {
        "active_library": active_info["active_library"],
        "db_kind": active_info["db_kind"],
        "active_library_db_path_match": active_info["matches_active_library_db_path"],
        "effective_active_library_db_path_match": active_info.get("effective_matches_active_library_db_path"),
        "active_library_db_path": active_info["active_library_db_path"],
        "effective_db_path": active_info.get("effective_db_path"),
        "effective_storage_root": active_info.get("effective_storage_root"),
        "recovered_from_candidate_scan": active_info.get("recovered_from_candidate_scan"),
        "papers_total": len(papers),
        "papers_with_markdown_or_sections": sum(1 for row in per_paper if row["has_markdown_or_sections"]),
        "papers_with_dft_keywords": sum(1 for row in per_paper if row["keyword_labels"]),
        "candidate_evidence_snippets_count": sum(len(row["candidate_evidence_snippets"]) for row in per_paper),
        "extractable_dft_results_count": sum(row["dft_results_count"] for row in per_paper),
        "extractable_papers_count": sum(1 for row in per_paper if row["extractable"]),
        "extractable_property_types": property_types,
        "extraction_fail_reasons": dict(sorted(failure_counts.items())),
        "papers": per_paper,
    }


def _apply_preview(report: dict[str, Any]) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for paper in report["papers"]:
        for item in paper.get("sample_results", [])[:3]:
            previews.append(
                {
                    "paper_id": paper["paper_id"],
                    "title": paper["title"],
                    "property_type": item.get("property_type"),
                    "adsorbate": item.get("adsorbate"),
                    "value": item.get("value"),
                    "unit": item.get("unit"),
                    "evidence_text_summary": str(item.get("evidence_text") or "")[:160],
                }
            )
    return previews


def apply_real_extraction(
    session: Session,
    *,
    paper_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    service = PaperReprocessingService(session, settings)
    results: list[dict[str, Any]] = []

    for paper in _iter_papers(session, paper_ids):
        coverage = _extract_paper_coverage(session, service, paper)
        if not coverage["extractable"]:
            continue
        document = service._rebuild_document(paper)
        summary = service.pipeline.replace_stage2(paper, document)
        inserted_rows = list(session.scalars(select(DFTResult).where(DFTResult.paper_id == paper.id)).all())
        session.add(
            AuditLog(
                paper_id=paper.id,
                action="d2_real_extraction_apply",
                source="script",
                target_type="paper",
                target_id=str(paper.id),
                payload={
                    "summary": summary,
                    "preview": _apply_preview({"papers": [coverage]}),
                },
            )
        )
        session.flush()
        results.append(
            {
                "paper_id": str(paper.id),
                "title": paper.title,
                "summary": summary,
                "dft_results_after_apply": len(inserted_rows),
                "property_types_after_apply": sorted({row.property_type or "" for row in inserted_rows if row.property_type}),
                "preview": _apply_preview({"papers": [coverage]}),
            }
        )
    session.commit()
    return {
        "applied": bool(results),
        "applied_papers": results,
        "cleanup_strategy": "Rows are tagged in audit_logs.action=d2_real_extraction_apply; default mode is dry-run with no writes.",
    }


def run_gate(*, paper_ids: list[UUID] | None = None, apply: bool = False) -> dict[str, Any]:
    activation_info = activate_active_library_database()
    active_info = require_active_library_sqlite()
    settings = get_settings()
    engine = get_engine(settings.database_url)

    with Session(engine, autoflush=False, future=True) as session:
        before_gate = build_audit(session)
        coverage = build_coverage_report(session, paper_ids=paper_ids)
        apply_preview = _apply_preview(coverage)

    if not apply:
        with Session(engine, autoflush=False, future=True) as session:
            after_gate = build_audit(session)
        return {
            "mode": "dry_run",
            "activation_database": activation_info,
            "active_database": active_info,
            "coverage": coverage,
            "apply_preview": apply_preview,
            "apply_executed": False,
            "before_gate_audit": before_gate,
            "after_gate_audit": after_gate,
            "export_writing_gate_unchanged": before_gate == after_gate,
            "page_bbox_highlight_policy": {
                "page": "preserve real page only",
                "bbox": "preserve real bbox only",
                "when_page_missing": "locator stays text_only or missing_page",
                "pdf_highlight": "never fabricated",
            },
            "persistence_strategy": {
                "default_mode": "dry_run",
                "apply_flag_required": True,
                "cleanup_strategy": "No cleanup needed in dry-run. Apply mode tags writes with audit_logs.action=d2_real_extraction_apply.",
            },
        }

    with Session(engine, autoflush=False, future=True) as session:
        applied = apply_real_extraction(session, paper_ids=paper_ids)
        after_apply_gate = build_audit(session)
        post_apply_coverage = build_coverage_report(session, paper_ids=paper_ids)
    return {
        "mode": "apply",
        "activation_database": activation_info,
        "active_database": active_info,
        "coverage": coverage,
        "apply_preview": apply_preview,
        "apply_executed": True,
        "apply_result": applied,
        "before_gate_audit": before_gate,
        "after_gate_audit": after_apply_gate,
        "post_apply_coverage": post_apply_coverage,
        "page_bbox_highlight_policy": {
            "page": "preserve real page only",
            "bbox": "preserve real bbox only",
            "when_page_missing": "locator stays text_only or missing_page",
            "pdf_highlight": "never fabricated",
        },
        "persistence_strategy": {
            "default_mode": "dry_run",
            "apply_flag_required": True,
            "cleanup_strategy": applied["cleanup_strategy"],
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D2-4 real extraction coverage and controlled persistence gate.")
    parser.add_argument("--paper-id", action="append", default=[], help="Optional paper UUID; may be passed multiple times.")
    parser.add_argument("--apply", action="store_true", help="Persist real extraction into the active library.")
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass
    args = _parse_args()
    paper_ids = [UUID(value) for value in args.paper_id] if args.paper_id else None
    report = run_gate(paper_ids=paper_ids, apply=args.apply)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"mode={report['mode']}")
        print(f"active_library={report['coverage']['active_library']}")
        print(f"db_kind={report['coverage']['db_kind']}")
        print(f"effective_db_path={report['coverage']['effective_db_path']}")
        print(f"papers_total={report['coverage']['papers_total']}")
        print(f"extractable_papers_count={report['coverage']['extractable_papers_count']}")
        print(f"extractable_dft_results_count={report['coverage']['extractable_dft_results_count']}")
        print(f"extractable_property_types={report['coverage']['extractable_property_types']}")
        print(f"extraction_fail_reasons={report['coverage']['extraction_fail_reasons']}")
        print(f"apply_preview={report['apply_preview']}")
        print(f"apply_executed={report['apply_executed']}")
        print(f"export_writing_gate_unchanged={report.get('export_writing_gate_unchanged')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
