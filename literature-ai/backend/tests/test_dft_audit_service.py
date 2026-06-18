from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import Base, DFTResult, Paper, PaperFigure, PaperTable
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.dft_rescan_policy import (
    build_dft_dedupe_signature,
    finalize_rescan_summary,
    summarize_rescan_progress,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    return Session(engine)


def test_dft_audit_flags_low_recall_when_numeric_table_outnumbers_candidates():
    session = _make_session()
    try:
        paper = Paper(
            title="DFT table paper",
            pdf_path="table-paper.pdf",
            workflow_status="Initial_Parsed",
        )
        session.add(paper)
        session.flush()
        session.add(
            PaperTable(
                paper_id=paper.id,
                caption="Table 1. DFT calculated binding energies and Hubbard U values.",
                markdown_content="""
| Metal | E b /eV | μ B | Δz /Å | U |
| --- | --- | --- | --- | --- |
| Fe | -1.16 | 2.12 | 0.64 | 4.0 |
| Co | -1.32 | 1.88 | 0.58 | 3.3 |
| Ni | -0.92 | 0.00 | 0.41 | 6.4 |
| Ru | -2.21 | 1.01 | 0.52 | 2.0 |
""",
                page=3,
            )
        )
        session.add(
            DFTResult(
                paper_id=paper.id,
                property_type="overpotential",
                value=4.0,
                unit="V",
                candidate_status="system_candidate",
            )
        )
        session.commit()

        audit = DFTCompletenessAuditor(session).audit_paper(paper.id)

        assert audit["coverage_status"] == "Suspected_Missing"
        assert audit["low_recall_warning"] is True
        assert audit["llm_rescan_recommended"] is False
        assert audit["ide_ai_review_recommended"] is True
        assert audit["numeric_signal_summary"]["numeric_value_count"] == 16
        assert audit["suspected_missing_count"] >= 15
        assert audit["coverage_ratio"] < 0.7
        assert audit["rescan_recommended"] is True
        assert audit["rescan_next_status"] == "Needs_IDE_Rescan"
        assert audit["candidate_generation_policy"]["web_llm_extract"] == "disabled"
    finally:
        session.close()


def test_dft_audit_does_not_flag_when_candidates_cover_numeric_table():
    session = _make_session()
    try:
        paper = Paper(title="Covered DFT paper", pdf_path="covered.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        session.add(
            PaperTable(
                paper_id=paper.id,
                caption="DFT adsorption energy table.",
                markdown_content="""
| Intermediate | E_ads / eV |
| --- | --- |
| Li2S4 | -1.10 |
| Li2S6 | -0.82 |
""",
                page=4,
            )
        )
        for value in (-1.10, -0.82):
            session.add(
                DFTResult(
                    paper_id=paper.id,
                    adsorbate="Li2Sx",
                    property_type="adsorption_energy",
                    value=value,
                    unit="eV",
                    candidate_status="system_candidate",
                )
            )
        session.commit()

        audit = DFTCompletenessAuditor(session).audit_paper(paper.id)

        assert audit["numeric_signal_summary"]["numeric_value_count"] == 2
        assert audit["low_recall_warning"] is False
        assert audit["llm_rescan_recommended"] is False
        assert audit["coverage_status"] in {"Human_Complete", "Initial_Parsed"}
    finally:
        session.close()


def test_dft_audit_does_not_send_figure_only_numeric_signals_to_text_llm():
    session = _make_session()
    try:
        paper = Paper(title="Figure only DFT paper", pdf_path="figure-only.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        session.add(
            PaperFigure(
                paper_id=paper.id,
                figure_label="Figure 4",
                caption=(
                    "Figure 4. DFT adsorption energy chart shows E_ads values of -1.10 eV, "
                    "-0.82 eV, -1.44 eV, -0.63 eV, and -0.51 eV."
                ),
                page=6,
                image_path="figures/figure-4.png",
                figure_role="dft_chart",
            )
        )
        session.add(
            DFTResult(
                paper_id=paper.id,
                property_type="adsorption_energy",
                value=-1.10,
                unit="eV",
                candidate_status="system_candidate",
            )
        )
        session.commit()

        audit = DFTCompletenessAuditor(session).audit_paper(paper.id)

        assert audit["numeric_signal_summary"]["numeric_value_count"] >= 5
        assert audit["text_llm_numeric_signal_summary"]["numeric_value_count"] == 0
        assert audit["llm_rescan_recommended"] is False
        assert audit["candidate_generation_policy"]["image_or_chart_review"] == "requires_human_or_vlm_not_text_llm"
    finally:
        session.close()


def test_dft_dedupe_signature_merges_main_text_and_si_repeated_value():
    base = {
        "paper_id": "paper-1",
        "corrected_value": {
            "material": "Fe-GDY",
            "adsorbate": "O2",
            "property_type": "adsorption_energy",
            "reaction_step": "O2 adsorption",
            "value": "-1.100",
            "unit": "ev",
        },
    }

    main = build_dft_dedupe_signature(
        {**base, "evidence_location": {"source_document_type": "main_text", "page": 5, "table": "Table 2"}}
    )
    si = build_dft_dedupe_signature(
        {
            **base,
            "evidence_location": {
                "source_document_type": "supplementary_information",
                "page": 12,
                "table": "Table S3",
            },
            "corrected_value": {**base["corrected_value"], "value": -1.10, "unit": "eV"},
        }
    )
    supporting_ref = build_dft_dedupe_signature(
        {**base, "evidence_location": {"source_document_type": "supporting_reference", "page": 8}}
    )

    assert main == si
    assert supporting_ref != main


def test_rescan_policy_stops_low_progress_and_marks_human_check():
    previous = [
        {
            "paper_id": "paper-1",
            "adsorbate": "O2",
            "property_type": "adsorption_energy",
            "value": -1.10,
            "unit": "eV",
            "reaction_step": "O2 adsorption",
        }
    ]
    imported = previous + [
        {
            "paper_id": "paper-1",
            "adsorbate": "O2",
            "property_type": "adsorption_energy",
            "value": "-1.100",
            "unit": "ev",
            "reaction_step": "O2 adsorption",
        }
    ]
    summary = summarize_rescan_progress(
        previous,
        imported,
        [{"category": "duplicate"}, {"category": "axis_tick"}, {"category": "page_number"}],
        rescan_round=3,
    )
    final = finalize_rescan_summary(summary)

    assert final["new_unique_count"] == 0
    assert final["duplicate_count"] >= 2
    assert final["stop_reason"] == "max_rounds_reached"
    assert final["next_status"] == "Needs_Human_Check"


def test_dft_audit_flags_200_numeric_signals_with_50_unique_candidates():
    session = _make_session()
    try:
        paper = Paper(title="Large DFT table paper", pdf_path="large.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        rows = ["| Metal | E_ads / eV |"] + ["| --- | --- |"]
        rows.extend(f"| M{i} | -{i / 100:.2f} |" for i in range(200))
        session.add(
            PaperTable(
                paper_id=paper.id,
                caption="Table 1. DFT adsorption energy table.",
                markdown_content="\n".join(rows),
                page=3,
            )
        )
        for i in range(50):
            session.add(
                DFTResult(
                    paper_id=paper.id,
                    adsorbate=f"M{i}",
                    property_type="adsorption_energy",
                    value=-(i / 100),
                    unit="eV",
                    candidate_status="system_candidate",
                )
            )
        session.commit()

        audit = DFTCompletenessAuditor(session).audit_paper(paper.id)

        assert audit["coverage_status"] == "Suspected_Missing"
        assert audit["text_llm_numeric_signal_summary"]["numeric_value_count"] == 200
        assert audit["unique_candidate_count"] == 50
        assert audit["coverage_ratio"] == 0.25
        assert audit["ide_ai_review_recommended"] is True
    finally:
        session.close()
