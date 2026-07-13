from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import Base, DFTResult, Paper, PaperFigure, PaperTable
from app.services.dft_audit_service import DFTCompletenessAuditor
from app.services.dft_rescan_policy import (
    _row_signature,
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


def test_dft_dedupe_signature_uses_canonical_atom_pair_and_ignores_locator():
    base = {
        "paper_id": "paper-1",
        "corrected_value": {
            "material": "Fe-GDY",
            "property_type": "bond_length",
            "value": 2.1,
            "unit": "Å",
            "atom_pair": "Li1-S",
        },
        "evidence_location": {"page": 4, "table": "T1", "source_document_type": "main_text"},
    }
    reversed_alias = {
        **base,
        "corrected_value": {**base["corrected_value"], "atom_pair": None},
        "evidence_location": {
            "page": 9,
            "table": "T7",
            "source_document_type": "supplementary_information",
            "bond_pair": "S – Li1",
        },
    }
    different_site = {
        **base,
        "corrected_value": {**base["corrected_value"], "atom_pair": "Li2-S"},
    }

    assert build_dft_dedupe_signature(base) == build_dft_dedupe_signature(reversed_alias)
    assert build_dft_dedupe_signature(base) != build_dft_dedupe_signature(different_site)


def test_dft_dedupe_signature_refuses_stable_identity_without_required_atom_pair():
    payload = {
        "paper_id": "paper-1",
        "corrected_value": {
            "material": "Fe-GDY",
            "property_type": "bond_length",
            "value": 2.1,
            "unit": "Å",
        },
    }

    first = build_dft_dedupe_signature(payload)
    second = build_dft_dedupe_signature(payload)

    assert first.startswith("dft:non-deduplicable:missing_atom_pair_identity:")
    assert second.startswith("dft:non-deduplicable:missing_atom_pair_identity:")
    assert first != second


def test_row_signature_recomputes_valid_point_identity_instead_of_using_legacy_signature():
    historical_row = {
        "id": "historical-point-1",
        "paper_id": "paper-1",
        "adsorbate": "Li2S4",
        "property_type": "adsorption_energy",
        "reaction_step": "Li2S4 adsorption",
        "value": -1.1,
        "unit": "eV",
        "evidence_payload": {"material_identity": "Fe-GDY", "dedupe_signature": "legacy-point-signature"},
    }
    current_candidate = {
        "paper_id": "paper-1",
        "adsorbate": "Li2S4",
        "property_type": "adsorption_energy",
        "reaction_step": "Li2S4 adsorption",
        "value": "-1.1000",
        "unit": "ev",
        "evidence_payload": {"material_identity": "Fe-GDY"},
    }

    assert _row_signature(historical_row) != "legacy-point-signature"
    assert _row_signature(historical_row) == _row_signature(current_candidate)


def test_row_signature_recomputes_interval_identity_when_legacy_signatures_match():
    base = {
        "paper_id": "paper-1",
        "adsorbate": "Li2S4",
        "property_type": "pdos_overlap_energy_window",
        "value": -2.5,
        "value_kind": "energy_window",
        "unit": "eV",
        "evidence_payload": {"material_identity": "FePc@WS2", "dedupe_signature": "legacy-interval-signature"},
    }
    lower_interval = {**base, "id": "interval-1", "value_upper": -0.5}
    different_upper = {**base, "id": "interval-2", "value_upper": -0.4}
    same_interval_current = {
        **base,
        "id": "interval-3",
        "value_upper": "-0.5000",
        "evidence_payload": {"material_identity": "FePc@WS2"},
    }

    assert _row_signature(lower_interval) != "legacy-interval-signature"
    assert _row_signature(lower_interval) != _row_signature(different_upper)
    assert _row_signature(lower_interval) == _row_signature(same_interval_current)


def test_row_signature_missing_atom_pair_is_stable_per_row_and_never_merges_sources():
    base = {
        "paper_id": "paper-1",
        "property_type": "bond_length",
        "value": 2.1,
        "unit": "Å",
        "evidence_payload": {"material_identity": "Fe-GDY"},
    }
    first = {**base, "id": "missing-pair-row-1"}
    second = {**base, "id": "missing-pair-row-2", "evidence_payload": {"material_identity": "Fe-GDY", "source_candidate_id": "source-2"}}
    source_candidate_only = {
        **base,
        "evidence_payload": {"material_identity": "Fe-GDY", "source_candidate_id": "source-only-1"},
    }
    saved = {
        **base,
        "id": "missing-pair-row-3",
        "evidence_payload": {
            "material_identity": "Fe-GDY",
            "dedupe_signature": "dft:non-deduplicable:missing_atom_pair_identity:saved-row-3",
        },
    }

    assert _row_signature(first) == _row_signature(first)
    assert _row_signature(first) != _row_signature(second)
    assert _row_signature(source_candidate_only) == _row_signature(source_candidate_only)
    assert _row_signature(source_candidate_only) != _row_signature(first)
    assert _row_signature(saved) == "dft:non-deduplicable:missing_atom_pair_identity:saved-row-3"


def test_row_signature_does_not_let_legacy_signature_hide_conflicting_atom_pair_aliases():
    row = {
        "id": "conflicting-pair-row",
        "paper_id": "paper-1",
        "property_type": "bond_length",
        "value": 2.1,
        "unit": "Å",
        "evidence_payload": {
            "material_identity": "Fe-GDY",
            "atom_pair": "Li1-S",
            "bond_pair": "Li2-S",
            "dedupe_signature": "legacy-conflicting-signature",
        },
    }

    with pytest.raises(ValueError, match="conflicting_atom_pair_aliases"):
        _row_signature(row)


def test_rescan_progress_matches_legacy_row_with_current_interval_and_counts_new_upper_bound():
    legacy_row = {
        "id": "legacy-interval-row",
        "paper_id": "paper-1",
        "adsorbate": "Li2S4",
        "property_type": "pdos_overlap_energy_window",
        "value": -2.5,
        "value_upper": -0.5,
        "value_kind": "energy_window",
        "unit": "eV",
        "evidence_payload": {"material_identity": "FePc@WS2", "dedupe_signature": "legacy-interval-signature"},
    }
    identical_current = {
        **legacy_row,
        "id": "current-interval-row",
        "value_upper": "-0.5000",
        "evidence_payload": {"material_identity": "FePc@WS2"},
    }
    different_upper = {**identical_current, "id": "current-other-upper", "value_upper": -0.4}

    duplicate = summarize_rescan_progress([legacy_row], [identical_current], [], rescan_round=1)
    new = summarize_rescan_progress([legacy_row], [different_upper], [], rescan_round=1)

    assert duplicate["new_unique_count"] == 0
    assert duplicate["duplicate_count"] == 1
    assert new["new_unique_count"] == 1
    assert new["duplicate_count"] == 0


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
