from __future__ import annotations

import asyncio
import csv
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.papers.aggregation import dft_dataset_quality, export_dft_dataset, export_dft_results_csv
from app.db.models import Base, CatalystSample, DFTResult, DFTSetting, EvidenceSpan, ExtractionFieldReview, Paper


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'export_gate.db'}"
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


def test_dft_export_default_excludes_missing_evidence_reference(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d1-blocked-count"] == "1"
            assert "missing_evidence" in response.headers["x-d1-blocked-reasons"]
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


def test_dft_export_blocks_missing_page_and_does_not_fabricate_page_or_bbox(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            paper = _paper(session)
            row = _dft(session, paper)
            _safe_review(session, paper, row)
            _evidence_ref(session, paper, row, page=None)
            session.commit()

            response, rows = _export_rows(session)

            assert rows == []
            assert response.headers["x-d3-export-count"] == "0"
            assert response.headers["x-d3-block-count"] == "1"
            assert "unsafe_locator" in response.headers["x-d1-blocked-reasons"]
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
            assert payload["metadata"]["eligible_count"] == 1
            assert payload["metadata"]["blocked_count"] == 1
            assert payload["metadata"]["blocked_reasons"]["missing_review"] == 1
            assert len(payload["records"]) == 1
            record = payload["records"][0]
            assert record["record_id"] == str(safe_row.id)
            assert record["paper"]["paper_id"] == str(paper.id)
            assert record["target"]["value"] == -1.23
            assert record["target"]["unit"] == "eV"
            assert record["target"]["normalized_value"] == -1.23
            assert record["target"]["normalized_unit"] == "eV"
            assert record["catalyst"]["name"] == "Fe-N-C"
            assert record["dft_settings"][0]["functional"] == "PBE"
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

            kj_record = next(r for r in payload2["records"] if r["record_id"] == str(safe_row_kj.id))
            assert kj_record["target"]["normalized_value"] == -1.0
            assert kj_record["target"]["normalized_unit"] == "eV"
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
