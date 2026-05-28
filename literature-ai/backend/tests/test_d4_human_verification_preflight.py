from __future__ import annotations

import json
import sqlite3

from app.services.human_verification_preflight import (
    ATTENTION,
    BLOCKED,
    PILOT_PAPER_ID,
    READY,
    build_human_verification_preflight_report,
)


CATALYST_ID = "09f836768f134e82a576ab359b264933"
METALS_ID = "280f2d9e3ebb41079702f6ea6d645465"
RATE_ID = "56f7258445b3465b9a4097ec60a2fabf"
NAME_ID = "e2c75b7f2d9c41ffa6e1e95e5d491896"
DFT_ID = "4ba0e4905934439c813633a8ddf4e201"


def _create_db(tmp_path, *, missing_locator: str | None = None, non_pilot: str | None = None, missing_page: str | None = None, missing_bbox: str | None = None):
    db_path = tmp_path / "d4_3i_preflight.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE papers (
            id TEXT PRIMARY KEY,
            title TEXT,
            pdf_path TEXT,
            docling_json_path TEXT,
            markdown_path TEXT,
            tei_path TEXT
        );
        CREATE TABLE extraction_field_reviews (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            original_value TEXT,
            reviewed_value TEXT,
            evidence_text TEXT,
            reviewer_status TEXT NOT NULL,
            target_resolution_status TEXT NOT NULL,
            target_label TEXT
        );
        CREATE TABLE evidence_locators (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            field_name TEXT,
            page INTEGER,
            bbox TEXT,
            evidence_text TEXT,
            locator_status TEXT,
            locator_confidence REAL,
            parser_source TEXT,
            warning_reason TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?)",
        (PILOT_PAPER_ID, "锂硫电池非均相电催化剂", "p.pdf", "docling.json", "p.md", "p.tei.xml"),
    )
    rows = [
        (CATALYST_ID, "catalyst_samples", "sample-1", "catalyst_type", "single_atom", "single atom evidence"),
        (METALS_ID, "catalyst_samples", "sample-1", "metal_centers", ["Fe", "Co", "V"], "Fe Co V evidence"),
        (RATE_ID, "electrochemical_performance", "perf-1", "rate", "0.2C", "0.2 C evidence"),
        (NAME_ID, "catalyst_samples", "sample-1", "name", "Fe-Co-V", "HAADF-STEM"),
        (DFT_ID, "dft_settings", "dft-1", "convergence_settings", {"reproducibility": {"score": 0}}, "{}"),
    ]
    for review_id, target_type, target_id, field, value, evidence in rows:
        paper_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" if non_pilot == review_id else PILOT_PAPER_ID
        connection.execute(
            """
            INSERT INTO extraction_field_reviews
            (id, paper_id, target_type, target_id, field_name, original_value,
             reviewed_value, evidence_text, reviewer_status, target_resolution_status, target_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                paper_id,
                target_type,
                target_id,
                field,
                json.dumps(value, ensure_ascii=False),
                "null",
                evidence,
                "pending",
                "active",
                str(value),
            ),
        )
    locator_rows = [
        (CATALYST_ID, "catalyst_samples", "sample-1", "catalyst_type", 7, {"x0": 1}, "The single-atom catalyst, SAC, is described.", "#/texts/79"),
        (METALS_ID, "catalyst_samples", "sample-1", "metal_centers", 7, {"x0": 1}, "Fe, Co, and V are metal centers.", "#/texts/80"),
        (RATE_ID, "electrochemical_performance", "perf-1", "rate", 6, {"x0": 1}, "Full cells at 0.2 C are shown.", "#/texts/74"),
    ]
    for review_id, target_type, target_id, field, page, bbox, evidence_text, docling_ref in locator_rows:
        if missing_locator == review_id:
            continue
        connection.execute(
            """
            INSERT INTO evidence_locators
            (id, paper_id, target_type, target_id, field_name, page, bbox, evidence_text,
             locator_status, locator_confidence, parser_source, warning_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"loc-{review_id}",
                PILOT_PAPER_ID,
                target_type,
                target_id,
                field,
                None if missing_page == review_id else page,
                None if missing_bbox == review_id else json.dumps(bbox),
                evidence_text,
                "exact_page",
                0.68,
                "docling",
                f"controlled_locator_repair:D4-3H.1;review_id={review_id};source_artifact=docling.json:{docling_ref};match_method=substring_match;locator_only_no_review_change",
            ),
        )
    connection.commit()
    connection.close()
    return db_path


def _docling(text74="Full cells at 0.2 C are shown."):
    texts = [{"self_ref": f"#/texts/{index}", "text": "unused"} for index in range(81)]
    texts[74] = {"self_ref": "#/texts/74", "text": text74}
    texts[79] = {"self_ref": "#/texts/79", "text": "The single-atom catalyst, SAC, is described."}
    texts[80] = {"self_ref": "#/texts/80", "text": "Fe, Co, and V are metal centers."}
    return {"texts": texts}


def _report(tmp_path, **kwargs):
    db_kwargs = {key: kwargs.pop(key) for key in list(kwargs) if key in {"missing_locator", "non_pilot", "missing_page", "missing_bbox"}}
    return build_human_verification_preflight_report(
        _create_db(tmp_path, **db_kwargs),
        docling_payload=kwargs.pop("docling_payload", _docling()),
    )


def _item(report, review_id):
    return next(item for item in report["items"] if item["review_id"].replace("-", "") == review_id)


def test_repaired_locator_row_generates_preflight_item(tmp_path):
    report = _report(tmp_path)

    catalyst = _item(report, CATALYST_ID)
    assert catalyst["field"] == "catalyst_type"
    assert catalyst["preflight_status"] == READY


def test_preflight_item_defaults_to_not_verified_or_eligible(tmp_path):
    report = _report(tmp_path)

    for item in report["items"]:
        assert item["verified"] is False
        assert item["safe_verified"] is False
        assert item["export_eligible"] is False
        assert item["writing_eligible"] is False


def test_ready_for_human_review_does_not_equal_verified(tmp_path):
    report = _report(tmp_path)
    catalyst = _item(report, CATALYST_ID)

    assert catalyst["preflight_status"] == READY
    assert catalyst["verified"] is False
    assert catalyst["ready_for_human_review_is_verified"] is False


def test_missing_bbox_is_not_forged_and_records_page_only_precision(tmp_path):
    report = _report(tmp_path, missing_bbox=RATE_ID)
    rate = _item(report, RATE_ID)

    assert rate["page"] == 6
    assert rate["bbox"] is None
    assert rate["locator_precision"] == "page_only"
    assert "bbox_missing_locator_precision_page_only" in rate["warnings"]


def test_missing_page_is_not_forged(tmp_path):
    report = _report(tmp_path, missing_page=RATE_ID)
    rate = _item(report, RATE_ID)

    assert rate["page"] is None
    assert "locator_page_missing" in rate["blockers"]
    assert rate["preflight_status"] == BLOCKED


def test_name_fe_co_v_is_excluded_or_blocked(tmp_path):
    report = _report(tmp_path)

    assert all(item["field"] != "name" for item in report["items"])
    excluded = next(item for item in report["excluded_or_blocked"] if item["field"] == "name")
    assert excluded["preflight_status"] == BLOCKED


def test_convergence_settings_is_excluded_or_blocked(tmp_path):
    report = _report(tmp_path)

    assert all(item["field"] != "convergence_settings" for item in report["items"])
    excluded = next(item for item in report["excluded_or_blocked"] if item["field"] == "convergence_settings")
    assert excluded["preflight_status"] == BLOCKED


def test_non_pilot_row_is_rejected(tmp_path):
    report = _report(tmp_path, non_pilot=CATALYST_ID)
    catalyst = _item(report, CATALYST_ID)

    assert catalyst["preflight_status"] == BLOCKED
    assert "non_pilot_paper" in catalyst["blockers"]


def test_missing_locator_row_is_not_ready(tmp_path):
    report = _report(tmp_path, missing_locator=CATALYST_ID)
    catalyst = _item(report, CATALYST_ID)

    assert catalyst["preflight_status"] == BLOCKED
    assert "missing_locator" in catalyst["blockers"]


def test_ambiguous_text_overlap_needs_attention_not_ready(tmp_path):
    report = _report(tmp_path, docling_payload=_docling(text74="The unrelated paragraph mentions 0.2 C only."))
    rate = _item(report, RATE_ID)

    assert rate["preflight_status"] == ATTENTION
    assert "ambiguous_text_overlap_locator_to_source_artifact" in rate["warnings"]


def test_helper_does_not_write_db_or_modify_review_rows(tmp_path):
    db_path = _create_db(tmp_path)
    before = _snapshot(db_path)

    build_human_verification_preflight_report(db_path, docling_payload=_docling())

    assert _snapshot(db_path) == before


def test_helper_does_not_generate_verified_like_payload(tmp_path):
    report = _report(tmp_path)

    assert "mark_verified" not in json.dumps(report, ensure_ascii=False)
    assert report["safety"]["verified_true"] is False


def _snapshot(db_path):
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return {
            "reviews": connection.execute("SELECT * FROM extraction_field_reviews ORDER BY id").fetchall(),
            "locators": connection.execute("SELECT * FROM evidence_locators ORDER BY id").fetchall(),
        }
    finally:
        connection.close()
