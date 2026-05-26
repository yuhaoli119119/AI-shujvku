from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    DFTResult,
    EvidenceSpan,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    WritingCard,
)
from app.db.session import get_engine
from app.schemas.extraction import ExtractionFieldReviewSaveItem, ExtractionReviewMarkVerifiedRequest
from app.services.extraction_review_service import ExtractionReviewService
from app.utils.active_database import activate_active_library_database, require_active_library_sqlite
from app.utils.review_safety import has_required_evidence_reference, is_export_eligible_extraction, writing_card_gate


def _status_counts(session: Session, model: Any, attr: Any) -> dict[str, int]:
    rows = session.execute(select(attr, func.count()).select_from(model).group_by(attr)).all()
    return {str(status or "missing"): int(count) for status, count in rows}


def build_audit(session: Session) -> dict[str, Any]:
    dft_rows = session.scalars(select(DFTResult)).all()
    dft_gate_results = [is_export_eligible_extraction(session, row, target_type="dft_results") for row in dft_rows]
    dft_reasons = Counter(reason for result in dft_gate_results for reason in result.reasons)

    writing_cards = session.scalars(select(WritingCard)).all()
    writing_gates = [writing_card_gate(card) for card in writing_cards]
    writing_reasons = Counter(reason for result in writing_gates for reason in result.blocked_reasons)

    verified_reviews = session.scalars(
        select(ExtractionFieldReview).where(ExtractionFieldReview.reviewer_status == "verified")
    ).all()
    verified_missing_evidence_text = sum(1 for review in verified_reviews if not (review.evidence_text or "").strip())

    return {
        "external_analysis_runs": session.scalar(select(func.count()).select_from(ExternalAnalysisRun)) or 0,
        "external_analysis_candidates": session.scalar(select(func.count()).select_from(ExternalAnalysisCandidate)) or 0,
        "external_candidate_status_counts": _status_counts(session, ExternalAnalysisCandidate, ExternalAnalysisCandidate.status),
        "external_candidate_type_counts": _status_counts(
            session, ExternalAnalysisCandidate, ExternalAnalysisCandidate.candidate_type
        ),
        "external_candidates_missing_evidence_payload": session.scalar(
            select(func.count())
            .select_from(ExternalAnalysisCandidate)
            .where(ExternalAnalysisCandidate.evidence_payload.is_(None))
        )
        or 0,
        "review_status_counts": _status_counts(session, ExtractionFieldReview, ExtractionFieldReview.reviewer_status),
        "review_resolution_status_counts": _status_counts(
            session, ExtractionFieldReview, ExtractionFieldReview.target_resolution_status
        ),
        "verified_reviews": len(verified_reviews),
        "verified_reviews_missing_evidence_text": verified_missing_evidence_text,
        "dft_export_total_candidates": len(dft_gate_results),
        "dft_export_safe_eligible": sum(1 for result in dft_gate_results if result.eligible),
        "dft_export_blocked": sum(1 for result in dft_gate_results if not result.eligible),
        "dft_export_blocked_reasons": dict(sorted(dft_reasons.items())),
        "writing_cards_total": len(writing_gates),
        "writing_cards_safe_usable": sum(1 for result in writing_gates if result.can_use_for_writing),
        "writing_cards_blocked_reasons": dict(sorted(writing_reasons.items())),
    }


def _pick_e2e_target(session: Session) -> tuple[DFTResult | None, dict[str, Any]]:
    rows = session.scalars(
        select(DFTResult).where(DFTResult.evidence_text.is_not(None), DFTResult.evidence_text != "").limit(100)
    ).all()
    for row in rows:
        if not has_required_evidence_reference(
            session,
            paper_id=row.paper_id,
            target_type="dft_results",
            target_id=row.id,
        ):
            continue
        existing = session.scalar(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.paper_id == row.paper_id,
                ExtractionFieldReview.target_type == "dft_results",
                ExtractionFieldReview.target_id == str(row.id),
                ExtractionFieldReview.field_name == "value",
                ExtractionFieldReview.reviewer_status == "verified",
            )
        )
        if existing is None:
            return row, {"seed_created": False, "seed_reason": "existing_dft_result_with_evidence"}
    return None, {"seed_created": False, "seed_reason": "no_existing_dft_result_with_required_evidence"}


def _create_seed_target(session: Session) -> tuple[DFTResult, DFTResult, dict[str, Any]]:
    paper = Paper(
        title="D2_E2E_TEST rollback-only extraction seed",
        pdf_path="D2_E2E_TEST_no_pdf_page_or_bbox.pdf",
        authors=["D2_E2E_TEST"],
        journal="D2_E2E_TEST",
        year=2026,
    )
    session.add(paper)
    session.flush()

    evidence_text = (
        "D2_E2E_TEST rollback-only evidence text for active-library extraction gate; "
        "this is not a PDF exact locator and has no page or bbox."
    )
    target = DFTResult(
        paper_id=paper.id,
        adsorbate="D2_E2E_TEST_Li2S4",
        property_type="D2_E2E_TEST_adsorption_energy",
        value=-1.23,
        unit="eV",
        reaction_step="D2_E2E_TEST_seed",
        evidence_text=evidence_text,
        confidence=1.0,
    )
    unsafe_target = DFTResult(
        paper_id=paper.id,
        adsorbate="D2_E2E_TEST_UNSAFE",
        property_type="D2_E2E_TEST_missing_review",
        value=-9.99,
        unit="eV",
        reaction_step="D2_E2E_TEST_seed_unsafe",
        evidence_text=evidence_text,
        confidence=1.0,
    )
    session.add_all([target, unsafe_target])
    session.flush()

    span = EvidenceSpan(
        paper_id=paper.id,
        object_type="dft_results",
        object_id=str(target.id),
        text=evidence_text,
        page=None,
        confidence=1.0,
    )
    session.add(span)
    session.flush()
    return target, unsafe_target, {
        "seed_created": True,
        "seed_cleaned_by_rollback": True,
        "seed_reason": "created_rollback_only_d2_e2e_test_seed",
        "seed_paper_id": str(paper.id),
        "seed_target_id": str(target.id),
        "seed_unsafe_target_id": str(unsafe_target.id),
        "seed_evidence_span_id": str(span.id),
        "seed_marker": "D2_E2E_TEST",
        "seed_page": None,
        "seed_bbox": None,
    }


def run_e2e_rollback(session: Session, *, seed_if_needed: bool = False) -> dict[str, Any]:
    target, seed_info = _pick_e2e_target(session)
    candidate_exists = (session.scalar(select(func.count()).select_from(ExternalAnalysisCandidate)) or 0) > 0
    if target is None:
        if not seed_if_needed:
            return {
                "status": "skipped",
                "reason": "no dft_results row with evidence_text, evidence_span, and no existing verified value review",
                "external_or_extraction_result_exists": bool(candidate_exists or session.scalar(select(func.count()).select_from(DFTResult))),
                **seed_info,
            }
        target, unsafe_target, seed_info = _create_seed_target(session)
    else:
        unsafe_target = DFTResult(
            id=uuid4(),
            paper_id=target.paper_id,
            evidence_text=target.evidence_text,
        )

    service = ExtractionReviewService(session)
    save_result = service.save_reviews(
        target.paper_id,
        [
            ExtractionFieldReviewSaveItem(
                target_type="dft_results",
                target_id=str(target.id),
                field_name="value",
                original_value=target.value,
                reviewed_value=target.value,
                unit=target.unit,
                evidence_text=target.evidence_text,
                reviewer_status="corrected",
                reviewer="d2_rollback_probe",
                reviewer_note="D2-1 rollback-only corrected review probe",
            )
        ],
    )
    corrected_status = save_result[0].reviewer_status if save_result else "missing"
    marked = service.mark_verified(
        target.paper_id,
        ExtractionReviewMarkVerifiedRequest(
            target_type="dft_results",
            target_id=str(target.id),
            field_names=["value"],
            reviewer="d2_rollback_probe",
            reviewer_note="D2-1 rollback-only verified review probe",
        ),
    )
    gate = is_export_eligible_extraction(session, target, target_type="dft_results")
    unsafe_probe = is_export_eligible_extraction(session, unsafe_target, target_type="dft_results")
    audit_after_gate = build_audit(session)
    return {
        "status": "passed",
        "rolled_back": True,
        **seed_info,
        "paper_id": str(target.paper_id),
        "target_id": str(target.id),
        "unsafe_target_id": str(unsafe_target.id),
        "external_or_extraction_result_exists": True,
        "corrected_review_status_after_save": corrected_status,
        "mark_verified_status": marked[0].reviewer_status if marked else "missing",
        "mark_verified_safe_flag": marked[0].verified if marked else False,
        "export_gate_safe_verified": gate.eligible,
        "export_gate_reasons": list(gate.reasons),
        "unsafe_data_blocked": not unsafe_probe.eligible,
        "unsafe_gate_reasons": list(unsafe_probe.reasons),
        "safe_eligible_count": audit_after_gate["dft_export_safe_eligible"],
        "blocked_count": audit_after_gate["dft_export_blocked"],
        "blocked_reasons": audit_after_gate["dft_export_blocked_reasons"],
        "writing_cards_safe_usable": audit_after_gate["writing_cards_safe_usable"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit D2-1 AI candidate/review/export/writing safety boundaries.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument("--e2e-rollback", action="store_true", help="Run rollback-only review/verify/export gate probe.")
    parser.add_argument(
        "--seed-if-needed",
        action="store_true",
        help="Create D2_E2E_TEST rollback-only Paper/DFTResult/EvidenceSpan when no existing target is available.",
    )
    args = parser.parse_args()

    activate_active_library_database()
    db_info = require_active_library_sqlite()
    settings = get_settings()
    engine = get_engine(settings.database_url)
    with Session(engine, autoflush=False, future=True) as session:
        report = {
            "active_database": db_info,
            "audit": build_audit(session),
            "e2e_rollback": None,
        }
    if args.e2e_rollback:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, autoflush=False, future=True) as session:
                    report["e2e_rollback"] = run_e2e_rollback(session, seed_if_needed=args.seed_if_needed)
            finally:
                transaction.rollback()
        with Session(engine, autoflush=False, future=True) as session:
            report["post_e2e_audit"] = build_audit(session)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return

    print("D2-1 AI Workflow Boundary Audit")
    active = report["active_database"]
    print(f"active_db_kind={active['db_kind']}")
    print(f"active_db_path={active['db_path']}")
    print(f"active_library={active['active_library']}")
    print(f"is_active_library_sqlite={active['is_active_library_sqlite']}")
    for key, value in report["audit"].items():
        print(f"{key}={value}")
    if report["e2e_rollback"] is not None:
        print(f"e2e_rollback={report['e2e_rollback']}")


if __name__ == "__main__":
    main()
