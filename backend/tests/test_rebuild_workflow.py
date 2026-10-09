from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Paper, RebuildDataValue, RebuildPaperFile, WorkflowJob
from app.main import app
from app.schemas.rebuild import (
    RebuildAnalysisRequest,
    RebuildDataRowInput,
    RebuildValueCorrectionRequest,
    RebuildVisualAssetRequest,
)
from app.services.rebuild_workflow_service import (
    analyze,
    import_data_row,
    list_rows,
    reaction_templates,
    upsert_visual_asset,
    update_data_value,
)


def _make_paper(session) -> Paper:
    paper = Paper(
        library_name="默认文献库",
        title="Rebuild workflow test paper",
        doi=None,
        pdf_path="pdf/test.pdf",
        paper_code="R0001",
    )
    session.add(paper)
    session.commit()
    return paper


def _source(page_number: int, source_kind: str = "table", **kwargs):
    payload = {
        "source_kind": source_kind,
        "page_number": page_number,
        "label": "Table S1",
    }
    payload.update(kwargs)
    return payload


def _row(material: str, values: list[dict], condition: dict | None = None):
    return RebuildDataRowInput(
        reaction="HER",
        material=material,
        active_site_type="single_atom",
        active_site="Fe-N4",
        data_type="experimental",
        condition=condition or {"electrolyte": "0.1 M KOH"},
        values=values,
    )


def test_rebuild_pdf_association_is_idempotent_and_does_not_queue_parsing(
    setup_test_db, monkeypatch, tmp_path
):
    monkeypatch.setenv("LITAI_AUTH_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    pdf_path = tmp_path / "main.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% test document\n")
    with TestClient(app) as client:
        response = client.post(
            f"/api/rebuild/papers/{paper_id}/files",
            data={"role": "main"},
            files={"file": ("main.pdf", pdf_path.read_bytes(), "application/pdf")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["automatic_parsing_started"] is False
        assert response.json()["created"] is True
        response = client.post(
            f"/api/rebuild/papers/{paper_id}/files",
            data={"role": "main"},
            files={"file": ("main-copy.pdf", pdf_path.read_bytes(), "application/pdf")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] is False

    with factory() as session:
        assert session.scalar(select(RebuildPaperFile).where(RebuildPaperFile.paper_id == paper_id)) is not None
        assert session.scalar(select(WorkflowJob).where(WorkflowJob.library_name == "默认文献库")) is None


def test_rebuild_import_prefers_explicit_value_and_preserves_conflicts(setup_test_db):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        file_record = RebuildPaperFile(
            paper_id=paper.id,
            role="main",
            storage_path="pdf/main.pdf",
            original_filename="main.pdf",
            sha256="a" * 64,
            file_size=100,
        )
        session.add(file_record)
        session.commit()

        estimated = _row(
            "Fe-N4-C",
            [
                {
                    "field_name": "overpotential_mv_10",
                    "raw_value": "1.24",
                    "numeric_value": 1.24,
                    "unit": "mV",
                    "value_type": "estimated",
                    "precision_digits": 2,
                    "sources": [_source(3, "estimated", estimate_basis="Fig. 2a 坐标估读")],
                }
            ],
        )
        result = import_data_row(session, paper.id, estimated)
        assert result["status"] == "created"
        session.commit()

        explicit = _row(
            "Fe-N4-C",
            [
                {
                    "field_name": "overpotential_mv_10",
                    "raw_value": "1.2",
                    "numeric_value": 1.2,
                    "unit": "mV",
                    "value_type": "explicit",
                    "sources": [_source(4, "table", table_row=2, table_column=3)],
                }
            ],
        )
        result = import_data_row(session, paper.id, explicit)
        assert result["values"][0]["status"] == "explicit_value_promoted"
        session.commit()

        conflicting = _row(
            "Fe-N4-C",
            [
                {
                    "field_name": "overpotential_mv_10",
                    "raw_value": "1.3",
                    "numeric_value": 1.3,
                    "unit": "mV",
                    "value_type": "explicit",
                    "sources": [_source(5, "text", quote="The overpotential is 1.3 mV.")],
                }
            ],
        )
        result = import_data_row(session, paper.id, conflicting)
        assert result["values"][0]["status"] == "value_conflict_preserved"
        session.commit()

        rows = list_rows(session, reaction="HER")
        assert rows["total"] == 1
        value = rows["items"][0]["values"][0]
        assert value["raw_value"] == "1.2"
        assert value["value_type"] == "explicit"
        assert len(value["sources"]) == 3
        assert value["conflict"]["status"] == "value_conflict"


def test_rebuild_analysis_excludes_mixed_units_and_handles_constant(setup_test_db):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        samples = [
            ("A", "10", 10.0, "mV", "20", 20.0, "mV/dec"),
            ("B", "20", 20.0, "mV", "30", 30.0, "mV/dec"),
            ("C", "30", 30.0, "V", "40", 40.0, "mV/dec"),
        ]
        for material, x_raw, x_value, x_unit, y_raw, y_value, y_unit in samples:
            result = import_data_row(
                session,
                paper.id,
                _row(
                    material,
                    [
                        {
                            "field_name": "overpotential_mv_10",
                            "raw_value": x_raw,
                            "numeric_value": x_value,
                            "unit": x_unit,
                            "value_type": "explicit",
                            "sources": [_source(2, "table", table_row=1)],
                        },
                        {
                            "field_name": "tafel_slope_mv_dec",
                            "raw_value": y_raw,
                            "numeric_value": y_value,
                            "unit": y_unit,
                            "value_type": "explicit",
                            "sources": [_source(2, "table", table_row=1)],
                        },
                        {
                            "field_name": "stability_hours",
                            "raw_value": "10",
                            "numeric_value": 10.0,
                            "unit": "h",
                            "value_type": "explicit",
                            "sources": [_source(2, "table", table_row=1)],
                        },
                    ],
                ),
            )
            assert result["status"] == "created"
        session.commit()

        result = analyze(
            session,
            RebuildAnalysisRequest(x_field="overpotential_mv_10", y_field="tafel_slope_mv_dec", reaction="HER"),
        )
        assert result["sample_count"] == 2
        assert result["excluded_count"] == 1
        assert result["regression"]["slope"] == 1.0
        assert result["regression"]["intercept"] == 10.0
        assert any("单位不一致" in warning for warning in result["warnings"])

        constant = analyze(
            session,
            RebuildAnalysisRequest(
                x_field="overpotential_mv_10",
                y_field="stability_hours",
                reaction="HER",
            ),
        )
        assert constant["sample_count"] == 2
        assert constant["regression"] is None
        assert any("常量" in warning for warning in constant["warnings"])


def test_rebuild_crop_merges_cross_page_regions_and_csv_export(
    setup_test_db, monkeypatch, tmp_path
):
    import fitz
    from app.config import get_settings

    monkeypatch.setenv("LITAI_AUTH_ENABLED", "false")
    get_settings.cache_clear()
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id

    pdf_path = tmp_path / "si.pdf"
    document = fitz.open()
    for page_number in range(1, 3):
        page = document.new_page(width=400, height=300)
        page.draw_rect(fitz.Rect(40, 50, 360, 220), color=None, fill=(0.2, 0.5, 0.9))
        page.insert_text((50, 40), f"Table S1 page {page_number}", fontsize=12)
    document.save(pdf_path)
    document.close()

    client = TestClient(app)
    if True:
        upload = client.post(
            f"/api/rebuild/papers/{paper_id}/files",
            data={"role": "si"},
            files={"file": ("si.pdf", pdf_path.read_bytes(), "application/pdf")},
        )
        assert upload.status_code == 200, upload.text
        file_id = upload.json()["file"]["id"]

        crop = client.post(
            f"/api/rebuild/papers/{paper_id}/assets/crop",
            json={
                "asset_key": "table-s1-merged",
                "asset_type": "table",
                "file_id": file_id,
                "figure_label": "Table S1",
                "caption": "Cross-page electrochemical table",
                "explanation": "两页逻辑上属于同一张表，已纵向合并并保留两页来源。",
                "regions": [
                    {"page_number": 1, "bbox": [0.08, 0.08, 0.92, 0.78]},
                    {"page_number": 2, "bbox": [0.08, 0.08, 0.92, 0.78]},
                ],
            },
        )
        assert crop.status_code == 200, crop.text
        asset = crop.json()["asset"]
        assert crop.json()["rendered_pages"] == 2
        assert asset["page_numbers"] == [1, 2]

        import_payload = {
            "paper_id": str(paper_id),
            "rows": [
                {
                    "reaction": "HER",
                    "material": "Fe-N4-C",
                    "active_site_type": "single_atom",
                    "active_site": "Fe-N4",
                    "data_type": "experimental",
                    "condition": {"electrolyte": "0.1 M KOH"},
                    "values": [
                        {
                            "field_name": "overpotential_mv_10",
                            "raw_value": "120",
                            "numeric_value": 120.0,
                            "unit": "mV",
                            "value_type": "explicit",
                            "sources": [{"source_kind": "table", "page_number": 1, "label": "Table S1"}],
                        },
                        {
                            "field_name": "tafel_slope_mv_dec",
                            "raw_value": "42",
                            "numeric_value": 42.0,
                            "unit": "mV/dec",
                            "value_type": "explicit",
                            "sources": [{"source_kind": "table", "page_number": 2, "label": "Table S1"}],
                        },
                    ],
                }
            ],
        }
        imported = client.post("/api/rebuild/rows/import", json=import_payload)
        assert imported.status_code == 200, imported.text
        analysis = client.post(
            "/api/rebuild/analysis",
            json={
                "x_field": "overpotential_mv_10",
                "y_field": "tafel_slope_mv_dec",
                "reaction": "HER",
                "data_type": "experimental",
            },
        )
        assert analysis.status_code == 200, analysis.text
        csv_response = client.get(f"/api/rebuild/analysis/{analysis.json()['id']}/csv")
        assert csv_response.status_code == 200
        assert csv_response.headers["content-type"].startswith("text/csv")
        assert "Fe-N4-C" in csv_response.text

    settings = get_settings()
    image_path = settings.storage_root / asset["image_path"]
    assert image_path.is_file()
    assert image_path.stat().st_size > 1000


def test_rebuild_value_correction_preserves_previous_value(setup_test_db):
    from uuid import UUID

    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        imported = import_data_row(
            session,
            paper.id,
            _row(
                "Fe-N4-C",
                [
                    {
                        "field_name": "overpotential_mv_10",
                        "raw_value": "120",
                        "numeric_value": 120.0,
                        "unit": "mV",
                        "value_type": "explicit",
                        "sources": [_source(2, "table", table_row=1)],
                    }
                ],
            ),
        )
        assert imported["status"] == "created"
        session.commit()

        row = list_rows(session, reaction="HER")["items"][0]
        value = row["values"][0]
        corrected = update_data_value(
            session,
            value_id=UUID(value["id"]),
            payload=RebuildValueCorrectionRequest(
                raw_value="125",
                numeric_value=125.0,
                unit="mV",
                value_type="explicit",
                precision_digits=0,
                correction_reason="人工复核表格第 2 行",
                source={
                    "source_kind": "table",
                    "page_number": 2,
                    "label": "Table S1",
                    "table_row": 2,
                    "table_column": 1,
                    "quote": "Corrected value 125 mV",
                },
            ),
        )
        session.commit()

        assert corrected.raw_value == "125"
        assert corrected.conflict["status"] == "corrected_previous_retained"
        assert corrected.conflict["previous"]["raw_value"] == "120"


def test_rebuild_ai_work_package_and_batch_asset_import(setup_test_db, monkeypatch):
    factory = sessionmaker(bind=setup_test_db, autoflush=False, autocommit=False, future=True)
    with factory() as session:
        paper = _make_paper(session)
        paper_id = paper.id
        assets = [
            RebuildVisualAssetRequest(
                asset_key="fig-1-a",
                asset_type="subfigure",
                figure_label="Figure 1",
                subfigure_label="a",
                caption="Schematic synthesis",
                page_numbers=[1],
                material_mapping={"curves": ["A", "B"]},
                context_text="Main text context",
                explanation="中文逐图解释",
            ),
            RebuildVisualAssetRequest(
                asset_key="fig-1-b",
                asset_type="subfigure",
                figure_label="Figure 1",
                subfigure_label="b",
                caption="SEM image",
                page_numbers=[1],
                material_mapping={"sample": "A"},
                context_text="Main text context",
                explanation="中文逐图解释",
            ),
        ]
        for payload in assets:
            _, created = upsert_visual_asset(
                session,
                paper_id=paper_id,
                payload=payload,
                settings=None,
            )
            assert created is True
        session.commit()

        assert paper.paper_code == "R0001"
        assert len(assets) == 2
        assert list_rows(session, paper_id=paper_id)["total"] == 0
        assert reaction_templates()["reactions"]["CO2RR"]["label"] == "二氧化碳还原反应"
