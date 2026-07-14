from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ExternalAnalysisRun, SourceSnapshotReconciliation
from app.services.dft_review_bundle_service import DFTReviewBundleService


SOURCE_SNAPSHOT_ALGORITHM_VERSION = "source_snapshot_manifest_v1"
LEGACY_DFT_BUNDLE_ALGORITHM_VERSION = "offline_dft_review_bundle_v1"


class SourceSnapshotReconciliationError(RuntimeError):
    pass


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFC", str(value)).split())
    return normalized or None


def _integer(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return round(float(value), 6)


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return [_number(item) for item in value]


def _source_type_by_paper(source_documents: list[dict[str, Any]]) -> dict[str, str | None]:
    return {
        str(item.get("paper_id") or ""): _text(item.get("source_document_type"))
        for item in source_documents
        if item.get("paper_id")
    }


def _source_inventory(
    inventory: list[dict[str, Any]], *, source_types: dict[str, str | None]
) -> list[dict[str, Any]]:
    normalized = [
        {
            "role": _text(item.get("role")),
            "paper_id": _text(item.get("paper_id")),
            "paper_code": _text(item.get("paper_code")),
            "source_document_type": source_types.get(str(item.get("paper_id") or "")),
            "pdf_available": bool(item.get("pdf_available")),
            "included_in_bundle": bool(item.get("included_in_bundle")),
            "sha256": _text(item.get("sha256")),
            "size_bytes": _integer(item.get("size_bytes")),
        }
        for item in inventory
    ]
    return sorted(normalized, key=lambda item: (item["role"] != "main", item["role"] or "", item["paper_id"] or ""))


def _evidence_object(item: dict[str, Any], *, object_type: str) -> dict[str, Any]:
    common = {
        "object_type": object_type,
        "source_paper_id": _text(item.get("source_paper_id")),
        "source_paper_code": _text(item.get("source_paper_code")),
        "source_document_type": _text(item.get("source_document_type")),
        "source_record_id": _text(item.get("source_record_id")),
        "page": _integer(item.get("page")),
        "content_sha256": _text(item.get("content_sha256")),
        # A reviewed scope is part of the source contract; review-run IDs and
        # timestamps are intentionally excluded because they are not source content.
        "eligible_for_auto_apply": bool(item.get("eligible_for_auto_apply")),
    }
    if object_type == "figure":
        common.update(
            {
                "figure_label": _text(item.get("figure_label")),
                "caption": _text(item.get("caption")),
                "image_sha256": _text(item.get("image_sha256")),
                "bbox_norm": _bbox(item.get("current_bbox_norm") or item.get("bbox_norm")),
            }
        )
    else:
        common.update(
            {
                "caption": _text(item.get("caption")),
                "markdown_content": _text(item.get("markdown_content")),
            }
        )
    return common


def _section_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in items:
        if item.get("source_record_type") != "paper_section":
            continue
        normalized.append(
            {
                "source_paper_id": _text(item.get("source_paper_id")),
                "source_paper_code": _text(item.get("source_paper_code")),
                "source_document_type": _text(item.get("source_document_type")),
                "source_record_id": _text(item.get("source_record_id")),
                "page_start": _integer(item.get("page_start")),
                "page_end": _integer(item.get("page_end")),
                "content_sha256": _text(item.get("content_sha256")),
            }
        )
    return sorted(normalized, key=lambda item: (item["source_paper_id"] or "", item["page_start"] is None, item["page_start"] or 0, item["source_record_id"] or ""))


def _review_scope(review_gate: dict[str, Any]) -> dict[str, Any]:
    quality = review_gate.get("rag_quality") if isinstance(review_gate.get("rag_quality"), dict) else {}
    figures = quality.get("figures") if isinstance(quality.get("figures"), dict) else {}
    return {
        "stage_status": _text(review_gate.get("stage_status")),
        "current_snapshot_fingerprint": _text(review_gate.get("current_snapshot_fingerprint")),
        "completed_snapshot_fingerprint": _text(review_gate.get("completed_snapshot_fingerprint")),
        "rag_quality_status": _text(review_gate.get("rag_quality_status")),
        "eligible_figure_count": _integer(figures.get("eligible")),
        "total_figure_count": _integer(figures.get("total")),
    }


def build_source_snapshot_manifest(
    *,
    source_documents: list[dict[str, Any]],
    source_pdf_inventory: list[dict[str, Any]],
    extracted_figures: list[dict[str, Any]],
    extracted_tables: list[dict[str, Any]],
    text_snippets: list[dict[str, Any]],
    review_gate: dict[str, Any],
) -> dict[str, Any]:
    """Build a content-only, explainable source snapshot manifest.

    This deliberately excludes package paths, generated-at timestamps, database
    traversal order, review provenance, and DFT candidate lifecycle state.
    """

    source_types = _source_type_by_paper(source_documents)
    objects = [
        *[_evidence_object(item, object_type="figure") for item in extracted_figures],
        *[_evidence_object(item, object_type="table") for item in extracted_tables],
    ]
    objects.sort(
        key=lambda item: (
            item["object_type"],
            item["source_paper_id"] or "",
            item["page"] is None,
            item["page"] or 0,
            item["source_record_id"] or "",
        )
    )
    manifest = {
        "schema_version": SOURCE_SNAPSHOT_ALGORITHM_VERSION,
        "canonicalization": {
            "algorithm": "unicode-nfc-whitespace-null-v1",
            "unordered_collections": "stable-semantic-sort-v1",
            "excluded_non_scientific_fields": [
                "absolute_paths",
                "bundle_paths",
                "timestamps",
                "database_return_order",
                "review_provenance",
                "dft_candidate_lifecycle_state",
            ],
        },
        "source_documents": _source_inventory(source_pdf_inventory, source_types=source_types),
        "evidence_objects": objects,
        "page_section_evidence": _section_evidence(text_snippets),
        "review_scope": _review_scope(review_gate),
    }
    return {"fingerprint": _sha256(manifest), "manifest": manifest}


@dataclass(frozen=True)
class LegacyBundleSnapshot:
    fingerprint: str
    manifest: dict[str, Any]
    archive_sha256: str


def _json_from_zip(archive: ZipFile, name: str) -> Any:
    try:
        return json.loads(archive.read(name).decode("utf-8"))
    except KeyError as exc:
        raise SourceSnapshotReconciliationError(f"legacy_bundle_missing:{name}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceSnapshotReconciliationError(f"legacy_bundle_invalid_json:{name}") from exc


def _jsonl_from_zip(archive: ZipFile, name: str) -> list[dict[str, Any]]:
    try:
        raw = archive.read(name).decode("utf-8")
    except KeyError as exc:
        raise SourceSnapshotReconciliationError(f"legacy_bundle_missing:{name}") from exc
    except UnicodeDecodeError as exc:
        raise SourceSnapshotReconciliationError(f"legacy_bundle_invalid_utf8:{name}") from exc
    try:
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise SourceSnapshotReconciliationError(f"legacy_bundle_invalid_jsonl:{name}") from exc


def load_legacy_bundle_snapshot(path: Path, *, expected_fingerprint: str) -> LegacyBundleSnapshot:
    archive_path = path.resolve(strict=True)
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    try:
        with ZipFile(archive_path) as archive:
            bundle_manifest = _json_from_zip(archive, "manifest.json")
            if _text(bundle_manifest.get("schema_version")) != LEGACY_DFT_BUNDLE_ALGORITHM_VERSION:
                raise SourceSnapshotReconciliationError("legacy_bundle_schema_not_supported")
            actual_fingerprint = _text(bundle_manifest.get("bundle_fingerprint"))
            if not actual_fingerprint or actual_fingerprint != expected_fingerprint:
                raise SourceSnapshotReconciliationError("legacy_bundle_fingerprint_mismatch")
            metadata = _json_from_zip(archive, "parsed/paper_metadata.json")
            source_inventory = bundle_manifest.get("source_pdf_inventory") or metadata.get("source_pdf_inventory")
            if not isinstance(source_inventory, list):
                raise SourceSnapshotReconciliationError("legacy_bundle_source_inventory_missing")
            for item in source_inventory:
                bundle_file = _text(item.get("bundle_file"))
                if not bundle_file or not item.get("included_in_bundle"):
                    raise SourceSnapshotReconciliationError("legacy_bundle_source_pdf_not_included")
                try:
                    data = archive.read(bundle_file)
                except KeyError as exc:
                    raise SourceSnapshotReconciliationError(f"legacy_bundle_missing:{bundle_file}") from exc
                if hashlib.sha256(data).hexdigest() != _text(item.get("sha256")) or len(data) != _integer(item.get("size_bytes")):
                    raise SourceSnapshotReconciliationError("legacy_bundle_source_pdf_hash_mismatch")
            evidence = _json_from_zip(archive, "parsed/curated_figure_table_evidence_snapshot.json")
            result = build_source_snapshot_manifest(
                source_documents=metadata.get("source_documents") if isinstance(metadata.get("source_documents"), list) else [],
                source_pdf_inventory=source_inventory,
                extracted_figures=_json_from_zip(archive, "parsed/extracted_figures.json"),
                extracted_tables=_json_from_zip(archive, "parsed/extracted_tables.json"),
                text_snippets=_jsonl_from_zip(archive, "evidence/text_snippets.jsonl"),
                review_gate=evidence.get("review_gate") if isinstance(evidence.get("review_gate"), dict) else {},
            )
    except BadZipFile as exc:
        raise SourceSnapshotReconciliationError("legacy_bundle_invalid_zip") from exc
    return LegacyBundleSnapshot(
        fingerprint=result["fingerprint"], manifest=result["manifest"], archive_sha256=archive_sha256
    )


def structural_diff(expected: Any, actual: Any, *, path: str = "$") -> list[dict[str, Any]]:
    if type(expected) is not type(actual):
        return [{"path": path, "expected": expected, "actual": actual}]
    if isinstance(expected, dict):
        differences: list[dict[str, Any]] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                differences.append({"path": f"{path}.{key}", "expected": expected.get(key), "actual": actual.get(key)})
            else:
                differences.extend(structural_diff(expected[key], actual[key], path=f"{path}.{key}"))
        return differences
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [{"path": path, "expected_count": len(expected), "actual_count": len(actual)}]
        differences: list[dict[str, Any]] = []
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            differences.extend(structural_diff(left, right, path=f"{path}[{index}]"))
        return differences
    return [] if expected == actual else [{"path": path, "expected": expected, "actual": actual}]


class SourceSnapshotReconciliationService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def current_snapshot(self, paper_id: UUID) -> dict[str, Any]:
        materials = DFTReviewBundleService(self.session, self.settings)._build_materials(
            paper_id, enforce_figure_table_gate=False
        )
        return build_source_snapshot_manifest(
            source_documents=materials["source_documents"],
            source_pdf_inventory=materials["source_pdf_inventory"],
            extracted_figures=materials["extracted_figures"],
            extracted_tables=materials["extracted_tables"],
            text_snippets=materials["text_snippets"],
            review_gate=materials["curated_evidence_snapshot"]["review_gate"],
        )

    def dry_run(self, *, paper_id: UUID, discovery_run_id: UUID, archive_path: Path) -> dict[str, Any]:
        run = self.session.get(ExternalAnalysisRun, discovery_run_id)
        if run is None or run.paper_id != paper_id:
            raise SourceSnapshotReconciliationError("discovery_run_not_found_for_paper")
        payload = run.raw_payload if isinstance(run.raw_payload, dict) else {}
        metadata = payload.get("review_metadata") if isinstance(payload.get("review_metadata"), dict) else {}
        old_fingerprint = _text(metadata.get("bundle_fingerprint"))
        if not old_fingerprint:
            raise SourceSnapshotReconciliationError("legacy_run_missing_bundle_fingerprint")
        if _text(metadata.get("review_mode")) != "comprehensive_review" or _text(metadata.get("overall_status")) != "completed":
            raise SourceSnapshotReconciliationError("legacy_run_not_completed_comprehensive_review")
        coverage = payload.get("coverage_acknowledgement") if isinstance(payload.get("coverage_acknowledgement"), dict) else {}
        if coverage.get("missing_data_search_complete") is not True:
            raise SourceSnapshotReconciliationError("legacy_run_missing_data_search_incomplete")
        legacy = load_legacy_bundle_snapshot(archive_path, expected_fingerprint=old_fingerprint)
        current = self.current_snapshot(paper_id)
        differences = structural_diff(legacy.manifest, current["manifest"])
        historical_chart = _text(metadata.get("figure_table_completed_snapshot_fingerprint"))
        current_chart = _text(current["manifest"]["review_scope"].get("completed_snapshot_fingerprint"))
        if historical_chart != current_chart:
            differences.append(
                {
                    "path": "$.review_scope.completed_snapshot_fingerprint",
                    "expected": historical_chart,
                    "actual": current_chart,
                }
            )
        comparison = {
            "equivalent": not differences,
            "legacy_projection_fingerprint": legacy.fingerprint,
            "current_projection_fingerprint": current["fingerprint"],
            "legacy_archive_sha256": legacy.archive_sha256,
            "differences": differences,
        }
        return {
            "paper_id": str(paper_id),
            "discovery_run_id": str(discovery_run_id),
            "historical_fingerprint": old_fingerprint,
            "historical_algorithm_version": LEGACY_DFT_BUNDLE_ALGORITHM_VERSION,
            "current_fingerprint": current["fingerprint"],
            "current_algorithm_version": SOURCE_SNAPSHOT_ALGORITHM_VERSION,
            "historical_manifest": legacy.manifest,
            "current_manifest": current["manifest"],
            "comparison": comparison,
        }

    def reconcile(
        self,
        *,
        dry_run: dict[str, Any],
        reason: str,
        actor: str,
        fault_after_flush: bool = False,
    ) -> dict[str, Any]:
        comparison = dry_run["comparison"]
        if comparison.get("equivalent") is not True:
            raise SourceSnapshotReconciliationError("source_snapshot_not_equivalent")
        paper_id = UUID(str(dry_run["paper_id"]))
        run_id = UUID(str(dry_run["discovery_run_id"]))
        existing = self.session.scalar(
            select(SourceSnapshotReconciliation).where(
                SourceSnapshotReconciliation.paper_id == paper_id,
                SourceSnapshotReconciliation.discovery_run_id == run_id,
                SourceSnapshotReconciliation.historical_fingerprint == dry_run["historical_fingerprint"],
                SourceSnapshotReconciliation.current_fingerprint == dry_run["current_fingerprint"],
                SourceSnapshotReconciliation.current_algorithm_version == SOURCE_SNAPSHOT_ALGORITHM_VERSION,
            )
        )
        if existing is not None:
            return {"status": "already_reconciled", "database_writes": 0, "reconciliation_id": str(existing.id)}
        row = SourceSnapshotReconciliation(
            paper_id=paper_id,
            discovery_run_id=run_id,
            historical_fingerprint=dry_run["historical_fingerprint"],
            historical_algorithm_version=LEGACY_DFT_BUNDLE_ALGORITHM_VERSION,
            historical_manifest=dry_run["historical_manifest"],
            current_fingerprint=dry_run["current_fingerprint"],
            current_algorithm_version=SOURCE_SNAPSHOT_ALGORITHM_VERSION,
            current_manifest=dry_run["current_manifest"],
            comparison=comparison,
            reason=reason,
            actor=actor,
        )
        self.session.add(row)
        self.session.flush()
        if fault_after_flush:
            raise RuntimeError("source_snapshot_reconciliation_fault_after_flush")
        return {"status": "reconciled", "database_writes": 1, "reconciliation_id": str(row.id)}

    def has_current_reconciliation(
        self, *, paper_id: UUID, discovery_run_id: UUID, historical_fingerprint: str, current_fingerprint: str
    ) -> bool:
        row = self.session.scalar(
            select(SourceSnapshotReconciliation.id).where(
                SourceSnapshotReconciliation.paper_id == paper_id,
                SourceSnapshotReconciliation.discovery_run_id == discovery_run_id,
                SourceSnapshotReconciliation.historical_fingerprint == historical_fingerprint,
                SourceSnapshotReconciliation.current_fingerprint == current_fingerprint,
                SourceSnapshotReconciliation.current_algorithm_version == SOURCE_SNAPSHOT_ALGORITHM_VERSION,
            )
        )
        return row is not None
