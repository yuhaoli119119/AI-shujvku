from __future__ import annotations

import os

import asyncio
import csv
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.papers.aggregation import dft_dataset_quality, export_dft_dataset, export_dft_results_csv
from app.db.models import Base, CatalystSample, DFTResult, DFTSetting, EvidenceSpan, ExtractionFieldReview, Paper
from app.rag.eligibility import is_rag_eligible
from app.schemas.dft_export import DFTMLDatasetExportV2, select_training_records_v2
from app.services.dft_export_service import _has_recommended_ml_setting, _ml_readiness_score, build_dft_ml_dataset
from app.services.dft_review_service import DFTResultReviewService


def _session(tmp_path):
    db_url = os.environ["LITAI_TEST_DATABASE_URL"]
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, SessionLocal


def _paper(session: Session) -> Paper:
    paper = Paper(title="Export Gate Paper", pdf_path="paper.pdf", authors=["A"])
    session.add(paper)
    session.flush()
    return paper


def _catalyst(session: Session, paper: Paper) -> CatalystSample:
    catalyst = CatalystSample(
        paper_id=paper.id,
        name="Fe-N-C",
        catalyst_type="single_atom",
        metal_centers=["Fe"],
        coordination="Fe-N4",
        support="carbon",
    )
    session.add(catalyst)
    session.flush()
    return catalyst


def _dft(
    session: Session,
    paper: Paper,
    *,
    evidence_text: str | None = "Evidence text",
    with_catalyst: bool = True,
) -> DFTResult:
    catalyst = _catalyst(session, paper) if with_catalyst else None
    row = DFTResult(
        paper_id=paper.id,
        catalyst_sample_id=catalyst.id if catalyst else None,
        adsorbate="Li2S4",
        property_type="adsorption_energy",
        value=-1.23,
        unit="eV",
        confidence=0.9,
        evidence_text=evidence_text,
    )
    session.add(row)
    session.flush()
    return row


def _safe_review(session: Session, paper: Paper, row: DFTResult) -> ExtractionFieldReview:
    review = ExtractionFieldReview(
        paper_id=paper.id,
        target_type="dft_results",
        target_id=str(row.id),
        field_name="value",
        reviewer_status="verified",
        target_resolution_status="active",
        evidence_text=row.evidence_text,
    )
    session.add(review)
    session.flush()
    return review


def _evidence_ref(session: Session, paper: Paper, row: DFTResult, *, page: int | None = None) -> EvidenceSpan:
    span = EvidenceSpan(
        paper_id=paper.id,
        object_type="dft_results",
        object_id=str(row.id),
        text=row.evidence_text or "Evidence text",
        page=page,
    )
    session.add(span)
    session.flush()
    return span


async def _response_text(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8-sig")


def _export_rows(session: Session):
    response = asyncio.run(
        export_dft_results_csv(
            property_type=None,
            adsorbate=None,
            year_min=None,
            year_max=None,
            session=session,
        )
    )
    text = asyncio.run(_response_text(response))
    rows = list(csv.DictReader(io.StringIO(text)))
    return response, rows


def test_dft_export_default_excludes_missing_review(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _evidence_ref(session, paper, row, page=1)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d3-export-safety-gate"] == "safe_verified_with_required_evidence"
            assert response.headers["x-d3-export-count"] == "0"
            assert response.headers["x-d3-block-count"] == "1"
            assert response.headers["x-d1-exported-count"] == "0"
            assert response.headers["x-d1-blocked-count"] == "1"
            assert "missing_review" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_export_default_excludes_unsafe_review_statuses(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        unsafe_statuses = ["unknown", "stale", "ambiguous", "unresolved"]
        with SessionLocal() as session:
            paper = _paper(session)
            for status in unsafe_statuses:
                row = _dft(session, paper)
                _evidence_ref(session, paper, row)
                session.add(
                    ExtractionFieldReview(
                        paper_id=paper.id,
                        target_type="dft_results",
                        target_id=str(row.id),
                        field_name="value",
                        reviewer_status="verified" if status != "unknown" else "unknown",
                        target_resolution_status=status if status != "unknown" else "active",
                        evidence_text=row.evidence_text,
                    )
                )
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d1-blocked-count"] == str(len(unsafe_statuses))
            assert "unsafe_review" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_fast_mode_allows_verified_text_without_separate_evidence_reference(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            session.commit()

            response, rows = _export_rows(session)

            assert len(rows) == 1
            assert response.headers["x-d1-exported-count"] == "1"
            assert response.headers["x-d1-blocked-count"] == "0"
    finally:
        engine.dispose()


def test_dft_export_allows_safe_verified_with_evidence_text(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=1)
            session.commit()

            response, rows = _export_rows(session)

            assert response.headers["x-d1-exported-count"] == "1"
            assert response.headers["x-d1-blocked-count"] == "0"
            assert rows[0]["value"] == "-1.23"
            assert rows[0]["review_status"] == "verified"
            assert rows[0]["review_gate_status"] == "safe_verified"
    finally:
        engine.dispose()


def test_dft_export_excludes_missing_material_identity(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper, with_catalyst=False)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=1)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d1-exported-count"] == "0"
            assert response.headers["x-d1-blocked-count"] == "1"
            assert "missing_material_identity" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_export_default_excludes_missing_evidence_text(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper, evidence_text="")
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d3-block-count"] == "1"
            assert "missing_evidence_text" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_fast_mode_allows_text_evidence_without_fabricating_page_or_bbox(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=None)
            session.commit()

            response, rows = _export_rows(session)

            assert len(rows) == 1
            assert response.headers["x-d3-export-count"] == "1"
            assert response.headers["x-d3-block-count"] == "0"
            assert "unsafe_locator" not in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_export_accepts_safe_verified_imported_pdf_page_anchor(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="value",
                    reviewer_status="verified",
                    target_resolution_status="active",
                    evidence_text=row.evidence_text,
                    review_payload={
                        "imported_evidence_payload": {
                            "page": 6,
                            "section": "Results",
                            "quoted_text": "The adsorption energy is -1.23 eV.",
                        }
                    },
                )
            )
            _evidence_ref(session, paper, row, page=None)
            session.commit()

            response, rows = _export_rows(session)

            assert response.headers["x-d1-exported-count"] == "1"
            assert response.headers["x-d1-blocked-count"] == "0"
            assert rows[0]["locator_status"] == "exact_page"
    finally:
        engine.dispose()


def test_dft_review_verify_result_persists_imported_page_anchor_for_export(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _evidence_ref(session, paper, row, page=None)
            session.commit()

            DFTResultReviewService(session).verify_result(
                paper_id=paper.id,
                result_id=row.id,
                confirm_reviewed_against_pdf=True,
                reviewer="dual_ai_settlement",
                reviewer_note="Dual AI checked the PDF page.",
                field_names=["value"],
                evidence_payload={
                    "page": 6,
                    "section": "Results",
                    "quoted_text": "The adsorption energy is -1.23 eV.",
                },
            )

            response, rows = _export_rows(session)
            stored_row = session.get(DFTResult, row.id)

            assert response.headers["x-d1-exported-count"] == "1"
            assert stored_row.candidate_status == "ML_Ready"
            assert rows[0]["locator_status"] == "exact_page"
    finally:
        engine.dispose()


def test_dft_export_blocks_supporting_reference_rows_from_main_paper_export(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            row.evidence_payload = {
                "source_document_type": "supporting_reference",
                "borrowed_from_reference": True,
                "source_document_label": "Ref. 32",
            }
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=6)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d1-exported-count"] == "0"
            assert "supporting_reference_not_main_paper_data" in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_rejected_dft_with_historical_safe_review_is_not_export_or_rag_eligible(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            row.candidate_status = "Rejected"
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=6)
            session.commit()

            response, rows = _export_rows(session)
            payload = build_dft_ml_dataset(session)

            assert rows == []
            assert response.headers["x-d1-exported-count"] == "0"
            assert "target_rejected" in response.headers["x-d1-blocked-reasons"]
            assert payload["records"] == []
            assert payload["metadata"]["eligible_count"] == 0
            assert payload["metadata"]["blocked_reasons"]["target_rejected"] == 1
            assert is_rag_eligible(session, row, "dft_result") is False
    finally:
        engine.dispose()


def test_dft_export_rejects_imported_page_anchor_from_unsafe_review(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            session.add(
                ExtractionFieldReview(
                    paper_id=paper.id,
                    target_type="dft_results",
                    target_id=str(row.id),
                    field_name="value",
                    reviewer_status="verified",
                    target_resolution_status="stale",
                    evidence_text=row.evidence_text,
                    review_payload={
                        "imported_evidence_payload": {
                            "page": 6,
                            "quoted_text": "The adsorption energy is -1.23 eV.",
                        }
                    },
                )
            )
            _evidence_ref(session, paper, row, page=None)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert "unsafe_review" in response.headers["x-d1-blocked-reasons"]
            assert "unsafe_locator" not in response.headers["x-d1-blocked-reasons"]
    finally:
        engine.dispose()


def test_dft_ml_dataset_export_uses_same_safe_verified_gate(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            setting = DFTSetting(
                paper_id=paper.id,
                software="VASP",
                functional="PBE",
                cutoff_energy_ev=500,
                k_points="3x3x1",
                raw_json={"software": "VASP", "functional": "PBE"},
            )
            session.add(setting)
            safe_row = _dft(session, paper)
            safe_row.catalyst_sample_id = catalyst.id
            blocked_row = _dft(session, paper)
            _safe_review(session, paper, safe_row)
            _evidence_ref(session, paper, safe_row, page=1)
            _evidence_ref(session, paper, blocked_row, page=1)
            session.commit()

            payload = asyncio.run(
                export_dft_dataset(
                    property_type=None,
                    adsorbate=None,
                    year_min=None,
                    year_max=None,
                    session=session,
                )
            )

            assert payload["metadata"]["safety_gate"] == "safe_verified_with_required_evidence"
            assert payload["metadata"]["schema_version"] == "dft_results_ml_v2"
            assert payload["metadata"]["eligible_count"] == 1
            assert payload["metadata"]["blocked_count"] == 1
            assert payload["metadata"]["blocked_reasons"]["missing_review"] == 1
            assert len(payload["records"]) == 1
            record = payload["records"][0]
            assert record["record_id"] == str(safe_row.id)
            assert record["paper"]["paper_id"] == str(paper.id)
            assert record["target"]["value"] == -1.23
            assert record["target"]["unit"] == "eV"
            assert record["target"]["canonical_property_type"] == "adsorption_energy"
            assert record["target"]["property_family"] == "energetics"
            assert record["target"]["physical_dimension"] == "energy"
            assert record["target"]["normalized_value"] == -1.23
            assert record["target"]["normalized_unit"] == "eV"
            assert record["catalyst"]["name"] == "Fe-N-C"
            assert record["dft_settings"][0]["functional"] == "PBE"
            assert record["setting_link_status"] == "clear_primary"
            assert record["recommended_ml_setting_field"] == "linked_dft_setting"
            assert record["sample_context"]["instance_key"] == record["sample_context"]["sample_key"]
            assert record["provenance"]["review_gate_status"] == "safe_verified"
            assert record["provenance"]["locator_status"] == "exact_page"

            # Check normalization logic with other units
            safe_row_mev = _dft(session, paper)
            safe_row_mev.property_type = "reaction_barrier"
            safe_row_mev.value = 500
            safe_row_mev.unit = "meV"
            _safe_review(session, paper, safe_row_mev)
            _evidence_ref(session, paper, safe_row_mev, page=1)

            safe_row_kj = _dft(session, paper)
            safe_row_kj.property_type = "adsorption_energy"
            safe_row_kj.value = -96.485
            safe_row_kj.unit = "kJ/mol"
            _safe_review(session, paper, safe_row_kj)
            _evidence_ref(session, paper, safe_row_kj, page=1)

            session.commit()

            payload2 = asyncio.run(
                export_dft_dataset(
                    property_type=None,
                    adsorbate=None,
                    year_min=None,
                    year_max=None,
                    session=session,
                )
            )
            # Find the new records
            mev_record = next(r for r in payload2["records"] if r["record_id"] == str(safe_row_mev.id))
            assert mev_record["target"]["normalized_value"] == 0.5
            assert mev_record["target"]["normalized_unit"] == "eV"
            assert mev_record["target"]["canonical_property_type"] == "reaction_barrier"
            assert mev_record["target"]["property_subtype"] == "reaction_barrier"

            kj_record = next(r for r in payload2["records"] if r["record_id"] == str(safe_row_kj.id))
            assert kj_record["target"]["normalized_value"] == -1.0
            assert kj_record["target"]["normalized_unit"] == "eV"
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_aggregates_descriptors_and_special_barrier_taxonomy(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            session.add(
                CatalystSample(
                    paper_id=paper.id,
                    name="Ni-N-C",
                    catalyst_type="single_atom",
                    metal_centers=["Ni"],
                    coordination="Ni-N4",
                    support="carbon",
                )
            )
            session.add(
                DFTSetting(
                    paper_id=paper.id,
                    software="VASP",
                    functional="PBE",
                    raw_json={"scope": "paper"},
                )
            )

            target_row = _dft(session, paper)
            target_row.catalyst_sample_id = catalyst.id

            descriptor_row = _dft(session, paper)
            descriptor_row.catalyst_sample_id = catalyst.id
            descriptor_row.property_type = "d_band_center"
            descriptor_row.value = -1.75
            descriptor_row.unit = "eV"
            descriptor_row.adsorbate = None
            descriptor_row.evidence_payload = {"target_property_type": "adsorption_energy"}

            barrier_row = _dft(session, paper)
            barrier_row.catalyst_sample_id = catalyst.id
            barrier_row.property_type = "li2s_decomposition_barrier"
            barrier_row.value = 0.65
            barrier_row.unit = "eV"

            for row in (target_row, descriptor_row, barrier_row):
                _safe_review(session, paper, row)
                _evidence_ref(session, paper, row, page=2)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))

            adsorption = next(r for r in payload["records"] if r["record_id"] == str(target_row.id))
            descriptor = next(r for r in payload["records"] if r["record_id"] == str(descriptor_row.id))
            barrier = next(r for r in payload["records"] if r["record_id"] == str(barrier_row.id))

            assert adsorption["descriptor_fields"]["d_band_center"]["value"] == -1.75
            assert adsorption["descriptor_fields"]["d_band_center"]["unit"] == "eV"
            assert descriptor["target"]["property_family"] == "electronic_descriptor"
            assert descriptor["target"]["ml_role"] == "descriptor"
            assert adsorption["sample_context"]["target_context_key"] == "adsorption_energy"
            assert barrier["target"]["canonical_property_type"] == "reaction_barrier"
            assert barrier["target"]["normalized_property_type"] == "li2s_decomposition_barrier"
            assert barrier["target"]["property_subtype"] == "li2s_decomposition_barrier"
            assert barrier["target"]["physical_dimension"] == "energy"
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_marks_multiple_paper_settings_as_ambiguous(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=3)
            session.add_all(
                [
                    DFTSetting(paper_id=paper.id, software="VASP", functional="PBE", raw_json={"section": "Methods"}),
                    DFTSetting(paper_id=paper.id, software="QE", functional="PBE0", raw_json={"section": "Computational details"}),
                ]
            )
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))
            record = payload["records"][0]

            assert record["setting_link_status"] == "ambiguous"
            assert record["linked_dft_setting"] is None
            assert len(record["setting_link_candidates"]) == 2
            assert "ambiguous_result_setting_link" in record["ml_blockers"]
            assert record["is_ml_ready"] is False
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_normalizes_descriptor_energy_and_flags_basis_specific_energy_units(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            descriptor_row = _dft(session, paper)
            descriptor_row.property_type = "d_band_center"
            descriptor_row.value = -1500
            descriptor_row.unit = "meV"

            basis_row = _dft(session, paper)
            basis_row.property_type = "cohesive_energy"
            basis_row.value = -8.19
            basis_row.unit = "eV/atom"
            basis_row.adsorbate = "alpha-GDY"

            for row in (descriptor_row, basis_row):
                _safe_review(session, paper, row)
                _evidence_ref(session, paper, row, page=4)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))
            descriptor = next(r for r in payload["records"] if r["record_id"] == str(descriptor_row.id))
            basis = next(r for r in payload["records"] if r["record_id"] == str(basis_row.id))

            assert descriptor["target"]["normalized_value"] == -1.5
            assert descriptor["target"]["normalized_unit"] == "eV"
            assert basis["target"]["normalized_value"] is None
            assert basis["target"]["normalization_basis"] == "per_atom"
            assert "energy_basis_requires_explicit_modeling" in basis["ml_blockers"]
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_routes_non_numeric_claims_to_lm_records(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            row.property_type = "dos_claim"
            row.value = None
            row.unit = None
            row.evidence_text = "The DOS near the Fermi level is increased."
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=5)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))

            assert payload["records"] == []
            assert len(payload["lm_records"]) == 1
            lm_record = payload["lm_records"][0]
            assert lm_record["claim"]["canonical_property_type"] == "dos_claim"
            assert lm_record["claim"]["ml_role"] == "lm_auxiliary"
            assert lm_record["claim"]["evidence_text"] == "The DOS near the Fermi level is increased."
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_does_not_share_generic_descriptor_across_adsorbates(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            session.add(DFTSetting(paper_id=paper.id, software="VASP", functional="PBE"))

            li2s4_row = _dft(session, paper)
            li2s4_row.catalyst_sample_id = catalyst.id
            li2s4_row.adsorbate = "Li2S4"

            li2s6_row = _dft(session, paper)
            li2s6_row.catalyst_sample_id = catalyst.id
            li2s6_row.adsorbate = "Li2S6"

            descriptor_row = _dft(session, paper)
            descriptor_row.catalyst_sample_id = catalyst.id
            descriptor_row.property_type = "d_band_center"
            descriptor_row.value = -1.42
            descriptor_row.unit = "eV"
            descriptor_row.adsorbate = None

            for row in (li2s4_row, li2s6_row, descriptor_row):
                _safe_review(session, paper, row)
                _evidence_ref(session, paper, row, page=6)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))
            li2s4_record = next(r for r in payload["records"] if r["record_id"] == str(li2s4_row.id))
            li2s6_record = next(r for r in payload["records"] if r["record_id"] == str(li2s6_row.id))

            assert "d_band_center" not in li2s4_record["descriptor_fields"]
            assert "d_band_center" not in li2s6_record["descriptor_fields"]
            assert "descriptor_instance_ambiguous" in li2s4_record["ml_blockers"]
            assert "descriptor_instance_ambiguous" in li2s6_record["ml_blockers"]
            assert li2s4_record["is_ml_ready"] is False
            assert li2s6_record["is_ml_ready"] is False
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_instance_key_includes_surface_site_and_coverage_context(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            row = _dft(session, paper)
            row.catalyst_sample_id = catalyst.id
            row.source_section = "Surface energetics"
            row.evidence_payload = {
                "surface_facet": "001",
                "adsorption_site": "top",
                "coverage": "0.25 ML",
                "slab": "4-layer slab",
                "termination": "S-terminated",
                "structure_name": "MoS2",
            }
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=7)
            session.add(DFTSetting(paper_id=paper.id, software="VASP", functional="PBE"))
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))
            record = payload["records"][0]
            context = record["sample_context"]["instance_components"]

            assert context["surface_facet"] == "001"
            assert context["adsorption_site"] == "top"
            assert context["coverage"] == "0.25 ML"
            assert context["slab"] == "4-layer slab"
            assert context["termination"] == "S-terminated"
            assert context["structure_name"] == "MoS2"
            assert "facet=001" in record["sample_context"]["instance_key"]
            assert "site=top" in record["sample_context"]["instance_key"]
            assert "coverage=0.25_ml" in record["sample_context"]["instance_key"]
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_keeps_legacy_dft_settings_but_recommends_linked_setting(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=8)
            session.add(
                DFTSetting(
                    paper_id=paper.id,
                    software="VASP",
                    functional="PBE",
                    raw_json={"section": "Results", "target_property_type": "adsorption_energy", "adsorbate": "Li2S4"},
                )
            )
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))
            record = payload["records"][0]

            assert len(record["dft_settings"]) == 1
            assert len(record["paper_level_dft_settings"]) == 1
            assert record["linked_dft_setting"]["functional"] == "PBE"
            assert record["recommended_ml_setting_field"] == "linked_dft_setting"
            assert payload["metadata"]["ml_setting_field"] == "linked_dft_setting"
    finally:
        engine.dispose()


def test_dft_ml_readiness_score_penalizes_descriptor_instance_ambiguity():
    assert _ml_readiness_score([]) == 100
    assert _ml_readiness_score(["descriptor_instance_ambiguous"]) == 65


def test_recommended_ml_setting_helper_ignores_paper_level_settings_without_link():
    record = {
        "paper_level_dft_settings": [{"dft_setting_id": "paper-setting-1"}],
        "linked_dft_setting": None,
        "setting_link_status": "ambiguous",
    }
    assert _has_recommended_ml_setting(record) is False


def test_dft_ml_dataset_v2_contract_fields_exist_for_all_numeric_records(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            session.add(
                DFTSetting(
                    paper_id=paper.id,
                    software="VASP",
                    functional="PBE",
                    raw_json={"section": "Results", "target_property_type": "adsorption_energy", "adsorbate": "Li2S4"},
                )
            )

            target_row = _dft(session, paper)
            target_row.catalyst_sample_id = catalyst.id
            target_row.adsorbate = "Li2S4"
            target_row.confidence = 0.9

            descriptor_row = _dft(session, paper)
            descriptor_row.catalyst_sample_id = catalyst.id
            descriptor_row.property_type = "d_band_center"
            descriptor_row.value = -1.8
            descriptor_row.unit = "eV"
            descriptor_row.adsorbate = None
            descriptor_row.evidence_payload = {"target_property_type": "adsorption_energy"}
            descriptor_row.confidence = 0.9

            blocked_norm_row = _dft(session, paper)
            blocked_norm_row.catalyst_sample_id = catalyst.id
            blocked_norm_row.property_type = "cohesive_energy"
            blocked_norm_row.value = -8.19
            blocked_norm_row.unit = "eV/atom"
            blocked_norm_row.adsorbate = "alpha-GDY"
            blocked_norm_row.confidence = 0.9

            for row in (target_row, descriptor_row, blocked_norm_row):
                _safe_review(session, paper, row)
                _evidence_ref(session, paper, row, page=9)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))

            assert payload["metadata"]["schema_version"] == "dft_results_ml_v2"
            assert payload["metadata"]["ml_setting_field"] == "linked_dft_setting"
            assert payload["records"]
            for record in payload["records"]:
                target = record["target"]
                assert "canonical_property_type" in target
                assert "property_family" in target
                assert "property_subtype" in target
                assert "physical_dimension" in target
                assert "ml_role" in target
                assert "ml_blockers" in record
                assert "ml_readiness_score" in record
                assert "is_ml_ready" in record
                assert record["recommended_ml_setting_field"] == "linked_dft_setting"
                assert record["sample_context"]["instance_key"]
                if target["normalized_value"] is None or target["normalized_unit"] in {None, ""}:
                    assert record["ml_blockers"]
    finally:
        engine.dispose()


def test_dft_ml_dataset_auto_binds_unbound_row_from_evidence_identity(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            session.add(
                CatalystSample(
                    paper_id=paper.id,
                    name="Ni-N-C",
                    catalyst_type="single_atom",
                    metal_centers=["Ni"],
                    coordination="Ni-N4",
                    support="carbon",
                )
            )
            session.add(
                DFTSetting(
                    paper_id=paper.id,
                    software="VASP",
                    functional="PBE",
                    raw_json={"scope": "paper"},
                )
            )
            row = _dft(session, paper, with_catalyst=False)
            row.evidence_payload = {"material_identity": "Fe-N-C", "structure_name": "Fe-N-C"}
            row.confidence = 0.9
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=4)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))
            record = payload["records"][0]

            assert record["catalyst"]["name"] == "Fe-N-C"
            assert record["provenance"]["catalyst_binding_source"] == "auto_bound"
            assert record["sample_context"]["material_scope_key"].startswith("material=catalyst_sample_")
    finally:
        engine.dispose()


def test_dft_ml_dataset_single_candidate_fallback_binds_unbound_row(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            catalyst.name = "Only Catalyst"
            session.add(
                DFTSetting(
                    paper_id=paper.id,
                    software="VASP",
                    functional="PBE",
                    raw_json={"scope": "paper"},
                )
            )
            row = _dft(session, paper, with_catalyst=False)
            row.evidence_payload = {"material_identity": "Unclear alias"}
            row.confidence = 0.9
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=4)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session, min_confidence=0.0))
            record = payload["records"][0]

            assert record["catalyst"]["name"] == "Only Catalyst"
            assert record["provenance"]["catalyst_binding_source"] == "single_candidate_fallback"
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_readiness_does_not_treat_paper_level_settings_as_clear_setting(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=10)
            session.add_all(
                [
                    DFTSetting(paper_id=paper.id, software="VASP", functional="PBE", raw_json={"section": "Methods"}),
                    DFTSetting(paper_id=paper.id, software="QE", functional="PBE0", raw_json={"section": "Results"}),
                ]
            )
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session))
            record = payload["records"][0]

            assert record["paper_level_dft_settings"]
            assert record["linked_dft_setting"] is None
            assert record["setting_link_status"] == "ambiguous"
            assert _has_recommended_ml_setting(record) is False
            assert record["is_ml_ready"] is False
            assert "ambiguous_result_setting_link" in record["ml_blockers"]
    finally:
        engine.dispose()


def test_dft_ml_dataset_v2_payload_validates_against_pydantic_contract(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)
            session.add(
                DFTSetting(
                    paper_id=paper.id,
                    software="VASP",
                    functional="PBE",
                    raw_json={"section": "Results", "target_property_type": "adsorption_energy", "adsorbate": "Li2S4"},
                )
            )

            numeric_row = _dft(session, paper)
            numeric_row.catalyst_sample_id = catalyst.id

            lm_row = _dft(session, paper)
            lm_row.catalyst_sample_id = catalyst.id
            lm_row.property_type = "dos_claim"
            lm_row.value = None
            lm_row.unit = None
            lm_row.evidence_text = "The DOS near the Fermi level is increased."

            for row in (numeric_row, lm_row):
                _safe_review(session, paper, row)
                _evidence_ref(session, paper, row, page=11)
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session))
            validated = DFTMLDatasetExportV2.model_validate(payload)

            assert validated.metadata.schema_version == "dft_results_ml_v2"
            assert validated.metadata.ml_setting_field == "linked_dft_setting"
            assert len(validated.records) == 1
            assert len(validated.lm_records) == 1
            assert validated.records[0].recommended_ml_setting_field == "linked_dft_setting"
            assert validated.records[0].sample_context.instance_key
            assert validated.lm_records[0].claim.ml_role == "lm_auxiliary"
    finally:
        engine.dispose()


def test_select_training_records_v2_filters_only_contract_safe_training_rows(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            catalyst = _catalyst(session, paper)

            ready_row = _dft(session, paper)
            ready_row.catalyst_sample_id = catalyst.id

            missing_norm_row = _dft(session, paper)
            missing_norm_row.catalyst_sample_id = catalyst.id
            missing_norm_row.property_type = "cohesive_energy"
            missing_norm_row.value = -8.19
            missing_norm_row.unit = "eV/atom"
            missing_norm_row.adsorbate = "alpha-GDY"

            ambiguous_setting_row = _dft(session, paper)
            ambiguous_setting_row.catalyst_sample_id = catalyst.id

            for row in (ready_row, missing_norm_row, ambiguous_setting_row):
                _safe_review(session, paper, row)
                _evidence_ref(session, paper, row, page=12)

            session.add_all(
                [
                    DFTSetting(paper_id=paper.id, software="VASP", functional="PBE", raw_json={"section": "Methods"}),
                    DFTSetting(paper_id=paper.id, software="QE", functional="PBE0", raw_json={"section": "Results"}),
                    DFTSetting(paper_id=paper.id, software="CP2K", functional="SCAN", raw_json={"section": "Discussion"}),
                ]
            )
            session.commit()

            payload = asyncio.run(export_dft_dataset(session=session))
            training_records = select_training_records_v2(payload)

            assert payload["metadata"]["schema_version"] == "dft_results_ml_v2"
            assert len(training_records) == 0

            # Build a clean single-setting payload to show the positive consumer path.
            paper2 = _paper(session)
            catalyst2 = _catalyst(session, paper2)
            session.add(
                DFTSetting(
                    paper_id=paper2.id,
                    software="VASP",
                    functional="PBE",
                    raw_json={"section": "Results", "target_property_type": "adsorption_energy", "adsorbate": "Li2S4"},
                )
            )
            clean_row = _dft(session, paper2)
            clean_row.catalyst_sample_id = catalyst2.id
            _safe_review(session, paper2, clean_row)
            _evidence_ref(session, paper2, clean_row, page=13)
            session.commit()

            combined_payload = asyncio.run(export_dft_dataset(session=session))
            clean_training_records = select_training_records_v2(combined_payload)

            assert len(clean_training_records) == 1
            sample = clean_training_records[0]
            assert sample.paper.paper_id == str(paper2.id)
            assert sample.is_ml_ready is True
            assert sample.linked_dft_setting is not None
            assert sample.target.normalized_value is not None
            assert sample.recommended_ml_setting_field == "linked_dft_setting"
            assert sample.paper_level_dft_settings
    finally:
        engine.dispose()


def test_dft_quality_panel_reports_blocked_rows_and_links(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            safe_row = _dft(session, paper)
            blocked_row = _dft(session, paper)
            _safe_review(session, paper, safe_row)
            _evidence_ref(session, paper, safe_row, page=1)
            _evidence_ref(session, paper, blocked_row, page=1)
            session.commit()

            payload = asyncio.run(
                dft_dataset_quality(
                    property_type=None,
                    adsorbate=None,
                    year_min=None,
                    year_max=None,
                    reason=None,
                    limit=100,
                    session=session,
                )
            )

            assert payload["metadata"]["safety_gate"] == "safe_verified_with_required_evidence"
            assert payload["metadata"]["eligible_count"] == 1
            assert payload["metadata"]["blocked_count"] == 1
            assert payload["metadata"]["blocked_reasons"]["missing_review"] == 1
            blocked = [row for row in payload["rows"] if not row["is_exportable"]][0]
            assert blocked["record_id"] == str(blocked_row.id)
            assert blocked["blocked_reasons"] == ["missing_review"]
            assert "paper_id=" + str(paper.id) in blocked["library_detail_url"]
            assert "external_analysis_workbench" in blocked["review_workbench_url"]
    finally:
        engine.dispose()
