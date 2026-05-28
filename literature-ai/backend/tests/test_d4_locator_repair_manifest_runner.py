from __future__ import annotations

import json
import sqlite3

from app.services.locator_repair_manifest_runner import (
    PILOT_PAPER_ID,
    RED_REVIEW_ID,
    YELLOW_REVIEW_IDS,
    build_d4_3g_manifest,
)


def _create_db(tmp_path):
    db_path = tmp_path / "d4_3g_manifest.db"
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
        CREATE TABLE evidence_spans (
            paper_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            text TEXT NOT NULL,
            confidence REAL
        );
        CREATE TABLE evidence_locators (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            field_name TEXT,
            evidence_text TEXT
        );
        """
    )
    connection.execute(
        """
        INSERT INTO papers
        (id, title, pdf_path, docling_json_path, markdown_path, tei_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (PILOT_PAPER_ID, "锂硫电池非均相电催化剂", "p.pdf", "docling.json", "p.md", "p.tei.xml"),
    )
    rows = [
        (
            YELLOW_REVIEW_IDS[0],
            "catalyst_samples",
            "sample-1",
            "name",
            "Fe-Co-V",
            "HAADF-STEM",
            "Fe-Co-V",
        ),
        (
            YELLOW_REVIEW_IDS[1],
            "catalyst_samples",
            "sample-1",
            "catalyst_type",
            "single_atom",
            "HAADF-STEM",
            "Fe-Co-V",
        ),
        (
            YELLOW_REVIEW_IDS[2],
            "catalyst_samples",
            "sample-1",
            "metal_centers",
            ["Fe", "Co", "V"],
            "HAADF-STEM",
            "Fe-Co-V",
        ),
        (
            YELLOW_REVIEW_IDS[3],
            "electrochemical_performance",
            "perf-1",
            "rate",
            "0.2C",
            "stitched caption",
            "0.2C",
        ),
        (
            RED_REVIEW_ID,
            "dft_settings",
            "dft-1",
            "convergence_settings",
            {"reproducibility": {"score": 0}},
            "{'software': [], 'functional': []}",
            "dft setting",
        ),
        (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "catalyst_samples",
            "sample-1",
            "support",
            "carbon",
            "not part of D4-3G",
            "carbon",
        ),
    ]
    for review_id, target_type, target_id, field, value, evidence, label in rows:
        connection.execute(
            """
            INSERT INTO extraction_field_reviews
            (id, paper_id, target_type, target_id, field_name, original_value,
             reviewed_value, evidence_text, reviewer_status, target_resolution_status, target_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                PILOT_PAPER_ID,
                target_type,
                target_id,
                field,
                json.dumps(value, ensure_ascii=False),
                "null",
                evidence,
                "pending",
                "active",
                label,
            ),
        )
    spans = [
        (
            "catalyst_sample",
            "sample-1",
            "因而, 与纳米团簇相比, 具有更高比表面积 和高原子利用率的单原子催化剂(single-atom catalyst, SAC)引起研究人员的广泛关注.",
            0.65,
        ),
        (
            "catalyst_sample",
            "sample-1",
            "其中, 以铁(Fe) [86, 87] 、钴(Co)、钒(V) [85] 为金属中 心的SAC修饰硫正极较为常见.",
            0.60,
        ),
        (
            "electrochemical_performance",
            "perf-1",
            "capacity of 1 mAh cm -2 . (f) Cycling performances of the S/TiN-VN@CNFs||Li/TiN-VN@CNFs full cells at 0.2 C [80]",
            0.68,
        ),
    ]
    for object_type, object_id, text, confidence in spans:
        connection.execute(
            """
            INSERT INTO evidence_spans
            (paper_id, object_type, object_id, text, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (PILOT_PAPER_ID, object_type, object_id, text, confidence),
        )
    connection.commit()
    connection.close()
    return db_path


def _blocks():
    return (
        {
            "text": "因而, 与纳米团簇相比, 具有更高比表面积 和高原子利用率的单原子催化剂 (single-atom catalyst, SAC) 引起研究人员的广泛关注.",
            "source_artifact": "docling.json:#/texts/79",
            "prov": [{"page_no": 7, "bbox": {"l": 53.858, "t": 477.052, "r": 287.155, "b": 359.672}}],
        },
        {
            "text": "其中, 以铁 (Fe) [86, 87] 、钴 (Co)、钒 (V) [85] 为金属中 心的SAC修饰硫正极较为常见.",
            "source_artifact": "docling.json:#/texts/80",
            "prov": [{"page_no": 7, "bbox": {"l": 53.859, "t": 356.594, "r": 287.167, "b": 70.085}}],
        },
        {
            "text": "capacity of 1 mAh cm -2 . (f) Cycling performances of the S/TiN-VN@CNFs||Li/TiN-VN@CNFs full cells at 0.2 C [80]",
            "source_artifact": "docling.json:#/texts/74",
            "prov": [{"page_no": 6, "bbox": {"l": 53.858, "t": 125.995, "r": 541.43, "b": 71.087}}],
        },
    )


def _manifest(tmp_path, blocks=None):
    return build_d4_3g_manifest(_create_db(tmp_path), docling_blocks=tuple(blocks or _blocks()))


def test_runner_only_selects_four_yellow_rows(tmp_path):
    manifest = _manifest(tmp_path)

    assert manifest["proposal_count"] == 4
    assert [item["field"] for item in manifest["proposals"]] == [
        "name",
        "catalyst_type",
        "metal_centers",
        "rate",
    ]


def test_convergence_settings_red_row_is_excluded(tmp_path):
    manifest = _manifest(tmp_path)

    assert all(item["field"] != "convergence_settings" for item in manifest["proposals"])
    assert manifest["excluded"] == [
        {
            "review_id": "4ba0e490-5934-439c-8136-33a8ddf4e201",
            "paper_id": PILOT_PAPER_ID,
            "field": "convergence_settings",
            "status": "RED",
            "proposal": "none",
            "reason": "no reliable source artifact / extracted empty-settings dict",
            "should_write_locator": False,
            "requires_human_confirmation": True,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
        }
    ]


def test_proposals_default_to_no_write_and_require_human_confirmation(tmp_path):
    manifest = _manifest(tmp_path)

    assert all(item["should_write_locator"] is False for item in manifest["proposals"])
    assert all(item["requires_human_confirmation"] is True for item in manifest["proposals"])


def test_proposals_do_not_set_verified_or_safe_verified(tmp_path):
    manifest = _manifest(tmp_path)

    assert all("verified" not in item for item in manifest["proposals"])
    assert all(item["mark_verified"] is False for item in manifest["proposals"])
    assert all(item["safe_verified"] is False for item in manifest["proposals"])


def test_proposals_do_not_unlock_export_or_writing(tmp_path):
    manifest = _manifest(tmp_path)

    assert all(item["export_eligible"] is False for item in manifest["proposals"])
    assert all(item["writing_eligible"] is False for item in manifest["proposals"])


def test_each_proposal_contains_human_review_manifest_fields(tmp_path):
    manifest = _manifest(tmp_path)
    required = {
        "review_id",
        "paper_id",
        "field",
        "value",
        "proposed_page",
        "proposed_bbox",
        "matched_text",
        "source_artifact",
        "match_method",
        "confidence",
        "warnings",
        "blockers",
        "requires_human_confirmation",
        "should_write_locator",
        "safe_verified",
        "export_eligible",
        "writing_eligible",
    }

    assert all(required <= set(item) for item in manifest["proposals"])


def test_runner_records_read_only_mode(tmp_path):
    manifest = _manifest(tmp_path)

    assert manifest["active_db_read_mode"] == "sqlite_uri_mode_ro"
    assert manifest["safety"]["writes_active_db"] is False
    assert manifest["safety"]["writes_locator"] is False


def test_runner_does_not_write_db_or_modify_review_rows(tmp_path):
    db_path = _create_db(tmp_path)
    before = _snapshot(db_path)

    manifest = build_d4_3g_manifest(db_path, docling_blocks=_blocks())

    after = _snapshot(db_path)
    assert manifest["active_db_locator_count_for_pilot"] == 0
    assert before == after


def test_missing_artifact_match_outputs_blocker_without_forged_page_or_bbox(tmp_path):
    manifest = _manifest(
        tmp_path,
        blocks=(
            {
                "text": "unrelated artifact text",
                "source_artifact": "docling.json:#/texts/1",
                "prov": [{"page_no": 9, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}],
            },
        ),
    )

    for proposal in manifest["proposals"]:
        assert proposal["proposal_status"] == "red"
        assert proposal["proposed_page"] is None
        assert proposal["proposed_bbox"] is None
        assert "no_text_match" in proposal["blockers"]
        assert "no_reliable_source_artifact_match" in proposal["blockers"]


def test_ambiguous_proposal_does_not_upgrade_to_green_or_safe(tmp_path):
    ambiguous_blocks = (
        {
            "text": "因而, 与纳米团簇相比, 具有更高比表面积 和高原子利用率的单原子催化剂(single-atom catalyst, SAC)引起研究人员的广泛关注.",
            "source_artifact": "docling.json:#/texts/79",
            "prov": [{"page_no": 7}],
        },
        {
            "text": "因而, 与纳米团簇相比, 具有更高比表面积 和高原子利用率的单原子催化剂(single-atom catalyst, SAC)引起研究人员的广泛关注.",
            "source_artifact": "docling.json:#/texts/179",
            "prov": [{"page_no": 9}],
        },
    )
    manifest = _manifest(tmp_path, blocks=ambiguous_blocks)
    catalyst_type = next(item for item in manifest["proposals"] if item["field"] == "catalyst_type")

    assert catalyst_type["proposal_status"] == "yellow"
    assert "ambiguous_match_requires_human_selection" in catalyst_type["blockers"]
    assert catalyst_type["safe_verified"] is False


def test_missing_page_is_not_forged(tmp_path):
    manifest = _manifest(
        tmp_path,
        blocks=(
            {
                "text": "capacity of 1 mAh cm -2 . (f) Cycling performances of the S/TiN-VN@CNFs||Li/TiN-VN@CNFs full cells at 0.2 C [80]",
                "source_artifact": "docling.json:#/texts/74",
            },
        ),
    )
    rate = next(item for item in manifest["proposals"] if item["field"] == "rate")

    assert rate["proposed_page"] is None
    assert "no_page_in_source" in rate["blockers"]


def test_missing_bbox_is_not_forged(tmp_path):
    manifest = _manifest(
        tmp_path,
        blocks=(
            {
                "text": "capacity of 1 mAh cm -2 . (f) Cycling performances of the S/TiN-VN@CNFs||Li/TiN-VN@CNFs full cells at 0.2 C [80]",
                "source_artifact": "docling.json:#/texts/74",
                "prov": [{"page_no": 6}],
            },
        ),
    )
    rate = next(item for item in manifest["proposals"] if item["field"] == "rate")

    assert rate["proposed_page"] == 6
    assert rate["proposed_bbox"] is None
    assert "bbox_unavailable" in rate["warnings"]


def _snapshot(db_path):
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        reviews = connection.execute(
            "SELECT * FROM extraction_field_reviews ORDER BY id"
        ).fetchall()
        locators = connection.execute("SELECT * FROM evidence_locators ORDER BY id").fetchall()
        return {"reviews": reviews, "locators": locators}
    finally:
        connection.close()
