from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExternalAnalysisRun, Paper, SourceSnapshotReconciliation
from app.services.source_snapshot_reconciliation_service import (
    LEGACY_DFT_BUNDLE_ALGORITHM_VERSION,
    SourceSnapshotReconciliationError,
    SourceSnapshotReconciliationService,
    build_source_snapshot_manifest,
    load_legacy_bundle_snapshot,
    structural_diff,
)


def _source_materials() -> dict:
    main_id = "00000000-0000-0000-0000-000000000101"
    si_id = "00000000-0000-0000-0000-000000000102"
    main_pdf = b"main source PDF bytes"
    si_pdf = b"supplementary source PDF bytes"
    return {
        "source_documents": [
            {"paper_id": main_id, "paper_code": "BTEST", "role": "main", "source_document_type": "main_text"},
            {"paper_id": si_id, "paper_code": "STEST", "role": "si", "source_document_type": "supplementary_information"},
        ],
        "source_pdf_inventory": [
            {"paper_id": main_id, "paper_code": "BTEST", "role": "main", "pdf_available": True, "included_in_bundle": True, "sha256": hashlib.sha256(main_pdf).hexdigest(), "size_bytes": len(main_pdf), "bundle_file": "source/main.pdf"},
            {"paper_id": si_id, "paper_code": "STEST", "role": "si", "pdf_available": True, "included_in_bundle": True, "sha256": hashlib.sha256(si_pdf).hexdigest(), "size_bytes": len(si_pdf), "bundle_file": "source/si/STEST.pdf"},
        ],
        "files": {"source/main.pdf": main_pdf, "source/si/STEST.pdf": si_pdf},
        "extracted_figures": [
            {"source_paper_id": si_id, "source_paper_code": "STEST", "source_document_type": "supplementary_information", "source_record_id": "si-figure", "page": 1, "figure_label": "Figure S1", "caption": "same page as main", "content_sha256": "f" * 64, "image_sha256": "i" * 64, "current_bbox_norm": [0.1, 0.2, 0.3, 0.4], "eligible_for_auto_apply": True},
            {"source_paper_id": main_id, "source_paper_code": "BTEST", "source_document_type": "main_text", "source_record_id": "main-figure", "page": 1, "figure_label": "Figure 1", "caption": "same page as SI", "content_sha256": "a" * 64, "image_sha256": "b" * 64, "current_bbox_norm": [0.1, 0.2, 0.3, 0.4], "eligible_for_auto_apply": True},
        ],
        "extracted_tables": [
            {"source_paper_id": main_id, "source_paper_code": "BTEST", "source_document_type": "main_text", "source_record_id": "main-table", "page": 2, "caption": "Table 1", "markdown_content": "|a|b|", "content_sha256": "c" * 64, "eligible_for_auto_apply": True},
        ],
        "text_snippets": [
            {"source_record_type": "paper_section", "source_paper_id": main_id, "source_paper_code": "BTEST", "source_document_type": "main_text", "source_record_id": "section-1", "page_start": 3, "page_end": 3, "content_sha256": "d" * 64},
            # This item intentionally simulates lifecycle-derived context; it is not source evidence.
            {"source_record_type": "dft_result_candidate_evidence", "source_record_id": "transient", "content_sha256": "e" * 64},
        ],
        "review_gate": {"stage_status": "completed", "current_snapshot_fingerprint": "chart" * 16, "completed_snapshot_fingerprint": "chart" * 16, "rag_quality_status": "ready", "rag_quality": {"figures": {"eligible": 2, "total": 2}}},
    }


def _snapshot(materials: dict) -> dict:
    return build_source_snapshot_manifest(
        source_documents=materials["source_documents"],
        source_pdf_inventory=materials["source_pdf_inventory"],
        extracted_figures=materials["extracted_figures"],
        extracted_tables=materials["extracted_tables"],
        text_snippets=materials["text_snippets"],
        review_gate=materials["review_gate"],
    )


def _legacy_archive(tmp_path: Path, materials: dict, fingerprint: str) -> Path:
    path = tmp_path / "legacy.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema_version": LEGACY_DFT_BUNDLE_ALGORITHM_VERSION, "bundle_fingerprint": fingerprint, "source_pdf_inventory": materials["source_pdf_inventory"]}))
        archive.writestr("parsed/paper_metadata.json", json.dumps({"source_documents": materials["source_documents"], "source_pdf_inventory": materials["source_pdf_inventory"]}))
        archive.writestr("parsed/extracted_figures.json", json.dumps(materials["extracted_figures"]))
        archive.writestr("parsed/extracted_tables.json", json.dumps(materials["extracted_tables"]))
        archive.writestr("parsed/curated_figure_table_evidence_snapshot.json", json.dumps({"review_gate": materials["review_gate"]}))
        archive.writestr("evidence/text_snippets.jsonl", "\n".join(json.dumps(item) for item in materials["text_snippets"]))
        for name, data in materials["files"].items():
            archive.writestr(name, data)
    return path


def test_snapshot_canonicalization_ignores_order_paths_timestamps_and_transient_context():
    materials = _source_materials()
    first = _snapshot(materials)
    reordered = _source_materials()
    reordered["source_documents"].reverse()
    reordered["source_pdf_inventory"].reverse()
    reordered["extracted_figures"].reverse()
    reordered["extracted_figures"][0]["absolute_path"] = "/different/machine/figure.png"
    reordered["extracted_figures"][0]["generated_at"] = "2099-01-01T00:00:00Z"
    reordered["text_snippets"].append({"source_record_type": "dft_result_candidate_evidence", "source_record_id": "later"})

    assert _snapshot(reordered)["fingerprint"] == first["fingerprint"]


def test_snapshot_keeps_main_and_si_same_page_as_distinct_source_objects():
    manifest = _snapshot(_source_materials())["manifest"]
    figures = [item for item in manifest["evidence_objects"] if item["object_type"] == "figure"]

    assert len(figures) == 2
    assert {item["source_document_type"] for item in figures} == {"main_text", "supplementary_information"}
    assert {item["source_paper_id"] for item in figures} == {
        "00000000-0000-0000-0000-000000000101",
        "00000000-0000-0000-0000-000000000102",
    }


def test_real_pdf_or_evidence_scope_changes_produce_structural_diff():
    baseline = _snapshot(_source_materials())
    changed = _source_materials()
    changed["source_pdf_inventory"][1]["sha256"] = "z" * 64
    changed["extracted_figures"][0]["page"] = 9
    current = _snapshot(changed)

    differences = structural_diff(baseline["manifest"], current["manifest"])
    assert baseline["fingerprint"] != current["fingerprint"]
    assert any("source_documents" in item["path"] for item in differences)
    assert any("evidence_objects" in item["path"] for item in differences)


def test_legacy_archive_requires_complete_hashable_source_manifest(tmp_path: Path):
    materials = _source_materials()
    archive = _legacy_archive(tmp_path, materials, "1" * 64)

    loaded = load_legacy_bundle_snapshot(archive, expected_fingerprint="1" * 64)
    assert loaded.manifest == _snapshot(materials)["manifest"]

    tampered = _source_materials()
    tampered["files"]["source/main.pdf"] = b"tampered"
    tampered_archive = _legacy_archive(tmp_path, tampered, "1" * 64)
    with pytest.raises(SourceSnapshotReconciliationError, match="source_pdf_hash_mismatch"):
        load_legacy_bundle_snapshot(tampered_archive, expected_fingerprint="1" * 64)


def test_reconciliation_is_auditable_idempotent_and_transactional(setup_test_db, tmp_path: Path, monkeypatch):
    materials = _source_materials()
    archive = _legacy_archive(tmp_path, materials, "2" * 64)
    current = _snapshot(materials)
    paper_id = uuid4()
    run_id = uuid4()
    with Session(setup_test_db) as session:
        paper = Paper(id=paper_id, paper_code="BTEST", title="test", pdf_path="storage/pdf/test.pdf")
        run = ExternalAnalysisRun(
            id=run_id,
            paper_id=paper_id,
            source="local_ai",
            raw_payload={
                "review_metadata": {"bundle_fingerprint": "2" * 64, "review_mode": "comprehensive_review", "overall_status": "completed", "figure_table_completed_snapshot_fingerprint": materials["review_gate"]["completed_snapshot_fingerprint"]},
                "coverage_acknowledgement": {"missing_data_search_complete": True},
            },
        )
        session.add(paper)
        session.flush()
        session.add(run)
        session.commit()

        service = SourceSnapshotReconciliationService(session, settings=object())
        monkeypatch.setattr(service, "current_snapshot", lambda _paper_id: current)
        dry_run = service.dry_run(paper_id=paper_id, discovery_run_id=run_id, archive_path=archive)
        assert dry_run["comparison"]["equivalent"] is True

        applied = service.reconcile(dry_run=dry_run, reason="test", actor="test")
        assert applied["status"] == "reconciled"
        session.commit()
        assert session.scalar(select(SourceSnapshotReconciliation.id)) is not None

        repeated = service.reconcile(dry_run=dry_run, reason="test", actor="test")
        assert repeated["status"] == "already_reconciled"
        assert repeated["database_writes"] == 0

        rollback_run = ExternalAnalysisRun(
            paper_id=paper_id,
            source="local_ai",
            raw_payload={
                "review_metadata": {"bundle_fingerprint": "2" * 64, "review_mode": "comprehensive_review", "overall_status": "completed", "figure_table_completed_snapshot_fingerprint": materials["review_gate"]["completed_snapshot_fingerprint"]},
                "coverage_acknowledgement": {"missing_data_search_complete": True},
            },
        )
        session.add(rollback_run)
        session.commit()
        rollback_dry_run = service.dry_run(paper_id=paper_id, discovery_run_id=rollback_run.id, archive_path=archive)
        with pytest.raises(RuntimeError, match="fault_after_flush"):
            service.reconcile(dry_run=rollback_dry_run, reason="test", actor="test", fault_after_flush=True)
        session.rollback()
        assert session.scalar(select(SourceSnapshotReconciliation.id).where(SourceSnapshotReconciliation.discovery_run_id == rollback_run.id)) is None

        session.rollback()
        changed = _source_materials()
        changed["extracted_tables"][0]["markdown_content"] = "|real|change|"
        monkeypatch.setattr(service, "current_snapshot", lambda _paper_id: _snapshot(changed))
        rejected = service.dry_run(paper_id=paper_id, discovery_run_id=run_id, archive_path=archive)
        assert rejected["comparison"]["equivalent"] is False
        with pytest.raises(SourceSnapshotReconciliationError, match="not_equivalent"):
            service.reconcile(dry_run=rejected, reason="test", actor="test")
        session.rollback()
