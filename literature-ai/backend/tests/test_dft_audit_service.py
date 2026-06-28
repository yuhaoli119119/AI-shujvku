from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import Base, DFTResult, Paper, PaperFigure, PaperTable
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.dft_rescan_policy import (
    build_dft_dedupe_signature,
    finalize_rescan_summary,
    is_dft_method_only_reaction_step,
    normalize_dft_reaction_step_for_identity,
    summarize_rescan_progress,
)


def _make_session() -> Session:
    engine = create_engine(os.environ["LITAI_TEST_DATABASE_URL"], future=True)
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


def test_dft_audit_treats_all_rejected_candidates_as_review_complete():
    session = _make_session()
    try:
        paper = Paper(title="Rejected DFT paper", pdf_path="rejected.pdf", workflow_status="Initial_Parsed")
        session.add(paper)
        session.flush()
        session.add(
            PaperTable(
                paper_id=paper.id,
                caption="DFT adsorption energy table.",
                markdown_content="| Intermediate | E_ads / eV |\n| --- | --- |\n| O | -1.10 |\n| OH | -0.82 |",
                page=4,
            )
        )
        session.add_all(
            [
                DFTResult(
                    paper_id=paper.id,
                    adsorbate="O",
                    property_type="adsorption_energy",
                    value=-1.10,
                    unit="eV",
                    candidate_status="Rejected",
                ),
                DFTResult(
                    paper_id=paper.id,
                    adsorbate="OH",
                    property_type="adsorption_energy",
                    value=-0.82,
                    unit="eV",
                    candidate_status="Rejected",
                ),
            ]
        )
        session.commit()

        audit = DFTCompletenessAuditor(session).audit_paper(paper.id, blocked_count=0)

        assert audit["coverage_status"] == "Human_Complete"
        assert audit["suspected_missing_count"] == 0
        assert audit["rescan_recommended"] is False
        assert audit["rescan_stop_reason"] == "all_candidates_rejected"
        assert audit["low_recall_warning"] is False
        assert audit["ide_ai_review_recommended"] is False
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


def test_dft_dedupe_signature_does_not_treat_method_as_reaction_step_identity():
    assert is_dft_method_only_reaction_step("DFT-D2 GGA-PBE") is True
    assert normalize_dft_reaction_step_for_identity("DFT-D2 GGA-PBE") == ""
    assert is_dft_method_only_reaction_step("Li2S adsorption on WN4@G side") is False

    without_step = build_dft_dedupe_signature(
        {
            "paper_id": "paper-1",
            "corrected_value": {
                "material": "WN4@G/TiS2",
                "adsorbate": "Li2S",
                "property_type": "adsorption_energy",
                "value": -5.21,
                "unit": "eV",
            },
        }
    )
    method_step = build_dft_dedupe_signature(
        {
            "paper_id": "paper-1",
            "corrected_value": {
                "material": "WN4@G/TiS2",
                "adsorbate": "Li2S",
                "property_type": "adsorption_energy",
                "reaction_step": "DFT-D2 GGA-PBE",
                "value": -5.21,
                "unit": "eV",
            },
        }
    )
    specific_step = build_dft_dedupe_signature(
        {
            "paper_id": "paper-1",
            "corrected_value": {
                "material": "WN4@G/TiS2",
                "adsorbate": "Li2S",
                "property_type": "adsorption_energy",
                "reaction_step": "Li2S adsorption on WN4@G side",
                "value": -5.21,
                "unit": "eV",
            },
        }
    )

    assert method_step == without_step
    assert specific_step != method_step


def test_dft_dedupe_signature_merges_generic_adsorption_step_aliases():
    base = {
        "paper_id": "paper-1",
        "corrected_value": {
            "material": "Fe-GDY",
            "adsorbate": "Li2S4",
            "property_type": "adsorption_energy",
            "value": "-1.100",
            "unit": "ev",
        },
    }

    signatures = {
        build_dft_dedupe_signature(
            {**base, "corrected_value": {**base["corrected_value"], "reaction_step": reaction_step}}
        )
        for reaction_step in ("adsorption", "Li2S4 adsorption", "adsorption of Li2S4")
    }
    on_same_material = build_dft_dedupe_signature(
        {**base, "corrected_value": {**base["corrected_value"], "reaction_step": "Li2S4 adsorption on Fe-GDY"}}
    )
    site_specific = build_dft_dedupe_signature(
        {
            **base,
            "corrected_value": {
                **base["corrected_value"],
                "reaction_step": "Li2S4 adsorption on Fe-GDY bridge site",
            },
        }
    )

    assert len(signatures) == 1
    assert on_same_material in signatures
    assert site_specific not in signatures


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
