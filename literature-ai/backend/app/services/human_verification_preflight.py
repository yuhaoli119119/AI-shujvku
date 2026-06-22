from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PILOT_PAPER_ID = "3978dc79f94f4457863fd68449ae293d"

REPAIRED_REVIEW_EXPECTATIONS = {
    "09f836768f134e82a576ab359b264933": {
        "field": "catalyst_type",
        "value": "single_atom",
        "page": 7,
        "docling_ref": "#/texts/79",
    },
    "280f2d9e3ebb41079702f6ea6d645465": {
        "field": "metal_centers",
        "value": ["Fe", "Co", "V"],
        "page": 7,
        "docling_ref": "#/texts/80",
    },
    "56f7258445b3465b9a4097ec60a2fabf": {
        "field": "rate",
        "value": "0.2C",
        "page": 6,
        "docling_ref": "#/texts/74",
    },
}

EXCLUDED_REVIEW_EXPECTATIONS = {
    "e2c75b7f2d9c41ffa6e1e95e5d491896": {
        "field": "name",
        "value": "Fe-Co-V",
        "status": "blocked",
        "reason": "HIGH-CAUTION aggregate label remains unrepaired and is not eligible for preflight approval",
    },
    "4ba0e4905934439c813633a8ddf4e201": {
        "field": "convergence_settings",
        "value": {
            "reproducibility": {
                "score": 0,
            }
        },
        "status": "blocked",
        "reason": "RED excluded: no reliable source artifact / extracted empty-settings dict",
    },
}

READY = "ready_for_human_review"
ATTENTION = "needs_human_attention"
BLOCKED = "blocked"


@dataclass(frozen=True)
class PreflightReviewRow:
    id: str
    paper_id: str
    target_type: str
    target_id: str
    field_name: str
    original_value: Any
    reviewed_value: Any
    evidence_text: str | None
    reviewer_status: str
    target_resolution_status: str
    target_label: str | None

    @property
    def proposed_value(self) -> Any:
        return self.reviewed_value if self.reviewed_value is not None else self.original_value


@dataclass(frozen=True)
class PreflightLocatorRow:
    id: str
    paper_id: str
    target_type: str | None
    target_id: str | None
    field_name: str | None
    page: int | None
    bbox: Any
    evidence_text: str | None
    locator_status: str | None
    locator_confidence: float | None
    parser_source: str | None
    warning_reason: str | None


class HumanVerificationPreflight:
    """Read-only D4-3I evidence alignment preflight.

    This service has no persistence method and no verified-like payload builder.
    It reads SQLite through URI mode=ro and returns audit metadata only.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        library_root: str | Path | None = None,
        docling_payload: dict[str, Any] | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.library_root = Path(library_root).resolve() if library_root is not None else self.db_path.parent
        self._docling_payload = docling_payload

    def build_report(self) -> dict[str, Any]:
        paper, rows, locators = self._read_snapshot()
        docling_payload = self._docling_payload if self._docling_payload is not None else self._load_docling_payload(paper)

        items = []
        for review_id, expected in REPAIRED_REVIEW_EXPECTATIONS.items():
            item = self._build_item(
                review_id=review_id,
                expected=expected,
                row=rows.get(review_id),
                locators=locators,
                docling_payload=docling_payload,
            )
            items.append(item)

        excluded = []
        for review_id, expected in EXCLUDED_REVIEW_EXPECTATIONS.items():
            excluded.append(self._build_excluded_item(review_id, expected, rows.get(review_id), locators))

        verified_rows = sum(1 for row in rows.values() if str(row.reviewer_status).lower() == "verified")
        safe_verified_rows = 0
        return {
            "manifest_type": "D4-3I.0_human_verification_preflight",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "paper_id": PILOT_PAPER_ID,
            "paper_title": paper.get("title"),
            "active_db_path": str(self.db_path),
            "active_db_read_mode": "sqlite_uri_mode_ro_query_only",
            "safety": {
                "writes_active_db": False,
                "writes_review_rows": False,
                "writes_evidence_locators": False,
                "verification_gate_called": False,
                "save_reviews_verified": False,
                "reviewer_status_verified": False,
                "verified_true": False,
                "migration_apply": False,
                "extraction_or_reprocessing_apply": False,
                "materialize": False,
                "registry_write": False,
                "db_copy_move_delete": False,
                "artifact_cleanup": False,
                "export_unlocked": False,
                "writing_unlocked": False,
            },
            "counts": {
                "preflight_items": len(items),
                "ready_for_human_review": sum(1 for item in items if item["preflight_status"] == READY),
                "needs_human_attention": sum(1 for item in items if item["preflight_status"] == ATTENTION),
                "blocked": sum(1 for item in items if item["preflight_status"] == BLOCKED),
                "excluded_or_blocked": len(excluded),
                "verified_rows": verified_rows,
                "safe_verified_rows": safe_verified_rows,
                "export_eligible_count": 0,
                "writing_eligible_count": 0,
            },
            "items": items,
            "excluded_or_blocked": excluded,
            "ready_items_do_not_imply_verified": True,
        }

    def _read_snapshot(
        self,
    ) -> tuple[dict[str, Any], dict[str, PreflightReviewRow], list[PreflightLocatorRow]]:
        review_ids = tuple(REPAIRED_REVIEW_EXPECTATIONS) + tuple(EXCLUDED_REVIEW_EXPECTATIONS)
        placeholders = ",".join("?" for _ in review_ids)
        with _connect_read_only(self.db_path) as connection:
            paper_row = connection.execute(
                """
                SELECT id, title, pdf_path, docling_json_path, markdown_path, tei_path
                FROM papers
                WHERE id = ?
                """,
                (PILOT_PAPER_ID,),
            ).fetchone()
            if paper_row is None:
                raise ValueError(f"Pilot paper not found: {PILOT_PAPER_ID}")

            review_rows = connection.execute(
                f"""
                SELECT id, paper_id, target_type, target_id, field_name,
                       original_value, reviewed_value, evidence_text, reviewer_status,
                       target_resolution_status, target_label
                FROM extraction_field_reviews
                WHERE id IN ({placeholders})
                ORDER BY field_name, id
                """,
                review_ids,
            ).fetchall()

            locator_rows = connection.execute(
                """
                SELECT id, paper_id, target_type, target_id, field_name, page, bbox,
                       evidence_text, locator_status, locator_confidence, parser_source, warning_reason
                FROM evidence_locators
                WHERE paper_id = ?
                ORDER BY field_name, id
                """,
                (PILOT_PAPER_ID,),
            ).fetchall()

        return (
            dict(paper_row),
            {_canonical_id(row["id"]): _review_from_row(row) for row in review_rows},
            [_locator_from_row(row) for row in locator_rows],
        )

    def _load_docling_payload(self, paper: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_artifact_path(paper.get("docling_json_path"))
        if path is None:
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_artifact_path(self, stored_path: str | None) -> Path | None:
        if not stored_path:
            return None
        raw = Path(stored_path)
        candidates = [
            raw,
            self.library_root / raw,
            self.library_root.parent.parent / raw,
            self.db_path.parent / raw,
        ]
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                return resolved
        return None

    def _build_item(
        self,
        *,
        review_id: str,
        expected: dict[str, Any],
        row: PreflightReviewRow | None,
        locators: list[PreflightLocatorRow],
        docling_payload: dict[str, Any],
    ) -> dict[str, Any]:
        warnings: list[str] = []
        blockers: list[str] = []
        source_artifact = None
        matched_text = None
        source_ref = expected["docling_ref"]

        if row is None:
            blockers.append("review_id_not_found")
            return _preflight_item(review_id, expected, BLOCKED, "Review row is missing", warnings, blockers)

        if row.paper_id != PILOT_PAPER_ID:
            blockers.append("non_pilot_paper")
        if row.field_name != expected["field"]:
            blockers.append("field_mismatch")
        if str(row.reviewer_status).lower() != "pending":
            blockers.append("reviewer_status_not_pending")
        if str(row.target_resolution_status).lower() != "active":
            blockers.append("target_resolution_status_not_active")

        locator = _select_locator(row, locators)
        if locator is None:
            blockers.append("missing_locator")
        else:
            source_artifact = _source_artifact_from_warning(locator.warning_reason)
            if locator.page != expected["page"]:
                blockers.append("locator_page_mismatch")
            if locator.evidence_text and str(locator.evidence_text).strip():
                if row.evidence_text:
                    review_warnings = _text_overlap_warnings(
                        row.evidence_text,
                        locator.evidence_text,
                        "review_evidence_text",
                    )
                    if review_warnings:
                        warnings.append("review_row_evidence_text_not_target_specific")
            else:
                blockers.append("locator_evidence_text_missing")
            if locator.bbox is None:
                warnings.append("bbox_missing_locator_precision_page_only")
            if locator.page is None:
                blockers.append("locator_page_missing")

        docling_text = _docling_text(docling_payload, source_ref)
        if not docling_text:
            blockers.append("source_artifact_ref_not_resolvable")
        else:
            matched_text = docling_text

        locator_text = locator.evidence_text if locator else None
        support_text = " ".join(text for text in (locator_text, matched_text) if text)
        if not support_text.strip():
            blockers.append("matched_text_missing")
        elif not _value_supported(expected["field"], expected["value"], support_text):
            blockers.append("field_value_not_supported_by_text")

        if locator_text and matched_text:
            warnings.extend(_text_overlap_warnings(locator_text, matched_text, "locator_to_source_artifact"))

        if source_artifact is None:
            warnings.append("source_artifact_not_recorded_on_locator_warning")
        elif source_ref not in source_artifact:
            blockers.append("source_artifact_ref_mismatch")

        if blockers:
            status = BLOCKED
            reason = "Preflight blocked by missing or mismatched required evidence metadata"
        elif any(warning.startswith("ambiguous_text_overlap") for warning in warnings):
            status = ATTENTION
            reason = "Evidence exists, but text overlap needs human attention before approval"
        else:
            status = READY
            reason = "Pending active pilot row has locator, matching page/source ref, evidence text, and value support"

        precision = "page_only" if locator is not None and locator.bbox is None else "bbox"
        return _preflight_item(
            review_id,
            expected,
            status,
            reason,
            _unique(warnings),
            _unique(blockers),
            row=row,
            locator=locator,
            source_artifact=source_artifact,
            matched_text=matched_text,
            locator_precision=precision,
        )

    def _build_excluded_item(
        self,
        review_id: str,
        expected: dict[str, Any],
        row: PreflightReviewRow | None,
        locators: list[PreflightLocatorRow],
    ) -> dict[str, Any]:
        locator = _select_locator(row, locators) if row is not None else None
        blockers = ["excluded_from_human_verification_preflight"]
        if locator is not None:
            blockers.append("unexpected_locator_present_but_not_eligible")
        return {
            "review_id": _display_review_id(review_id),
            "paper_id": row.paper_id if row is not None else PILOT_PAPER_ID,
            "field": expected["field"],
            "value": row.proposed_value if row is not None else expected["value"],
            "preflight_status": expected["status"],
            "reason": expected["reason"],
            "warnings": [],
            "blockers": blockers,
            "verified": False,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
            "ready_for_human_review_is_verified": False,
        }


def build_human_verification_preflight_report(
    db_path: str | Path,
    *,
    library_root: str | Path | None = None,
    docling_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raise RuntimeError("SQLite human-verification preflight has been removed. Use PostgreSQL-backed review APIs.")


def _disabled_build_human_verification_preflight_report(
    *,
    db_path: str | Path,
    artifact_root: str | Path,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    raise RuntimeError("SQLite human-verification preflight has been removed.")


def write_preflight_report_json(report: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _connect_read_only(db_path: Path) -> Any:
    raise RuntimeError("SQLite read-only connections have been removed.")


def _review_from_row(row: Any) -> PreflightReviewRow:
    return PreflightReviewRow(
        id=_canonical_id(row["id"]),
        paper_id=str(row["paper_id"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        field_name=str(row["field_name"]),
        original_value=_json_value(row["original_value"]),
        reviewed_value=_json_value(row["reviewed_value"]),
        evidence_text=row["evidence_text"],
        reviewer_status=str(row["reviewer_status"]),
        target_resolution_status=str(row["target_resolution_status"]),
        target_label=row["target_label"],
    )


def _locator_from_row(row: Any) -> PreflightLocatorRow:
    return PreflightLocatorRow(
        id=_canonical_id(row["id"]),
        paper_id=str(row["paper_id"]),
        target_type=row["target_type"],
        target_id=row["target_id"],
        field_name=row["field_name"],
        page=int(row["page"]) if row["page"] is not None else None,
        bbox=_json_value(row["bbox"]),
        evidence_text=row["evidence_text"],
        locator_status=row["locator_status"],
        locator_confidence=float(row["locator_confidence"]) if row["locator_confidence"] is not None else None,
        parser_source=row["parser_source"],
        warning_reason=row["warning_reason"],
    )


def _preflight_item(
    review_id: str,
    expected: dict[str, Any],
    status: str,
    reason: str,
    warnings: list[str],
    blockers: list[str],
    *,
    row: PreflightReviewRow | None = None,
    locator: PreflightLocatorRow | None = None,
    source_artifact: str | None = None,
    matched_text: str | None = None,
    locator_precision: str | None = None,
) -> dict[str, Any]:
    return {
        "review_id": _display_review_id(review_id),
        "paper_id": row.paper_id if row is not None else PILOT_PAPER_ID,
        "field": row.field_name if row is not None else expected["field"],
        "value": row.proposed_value if row is not None else expected["value"],
        "expected_value": expected["value"],
        "reviewer_status": row.reviewer_status if row is not None else None,
        "target_resolution_status": row.target_resolution_status if row is not None else None,
        "verified": False,
        "safe_verified": False,
        "export_eligible": False,
        "writing_eligible": False,
        "ready_for_human_review_is_verified": False,
        "page": locator.page if locator is not None else None,
        "expected_page": expected["page"],
        "source_artifact": source_artifact,
        "expected_docling_ref": expected["docling_ref"],
        "evidence_text": locator.evidence_text if locator is not None else None,
        "matched_text": matched_text,
        "bbox": locator.bbox if locator is not None else None,
        "locator_precision": locator_precision,
        "preflight_status": status,
        "reason": reason,
        "warnings": warnings,
        "blockers": blockers,
    }


def _select_locator(row: PreflightReviewRow | None, locators: list[PreflightLocatorRow]) -> PreflightLocatorRow | None:
    if row is None:
        return None
    matches = [
        locator
        for locator in locators
        if locator.paper_id == row.paper_id
        and locator.target_type == row.target_type
        and locator.target_id == row.target_id
        and locator.field_name == row.field_name
    ]
    return matches[0] if matches else None


def _docling_text(payload: dict[str, Any], ref: str) -> str | None:
    match = re.fullmatch(r"#/texts/(\d+)", ref)
    if not match:
        return None
    texts = payload.get("texts")
    if not isinstance(texts, list):
        return None
    index = int(match.group(1))
    if index >= len(texts):
        return None
    item = texts[index]
    if not isinstance(item, dict) or item.get("self_ref") != ref:
        return None
    text = item.get("text")
    return text if isinstance(text, str) and text.strip() else None


def _source_artifact_from_warning(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(?:^|;)source_artifact=([^;]+)", value)
    return match.group(1) if match else None


def _text_overlap_warnings(left: str | None, right: str | None, label: str) -> list[str]:
    if not left or not right:
        return []
    left_compact = _compact(left)
    right_compact = _compact(right)
    if not left_compact or not right_compact:
        return [f"ambiguous_text_overlap_{label}"]
    if left_compact in right_compact or right_compact in left_compact:
        return []
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens or not right_tokens:
        return [f"ambiguous_text_overlap_{label}"]
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    if overlap < 0.5:
        return [f"ambiguous_text_overlap_{label}"]
    return []


def _value_supported(field: str, value: Any, text: str) -> bool:
    compact = _compact(text)
    if field == "catalyst_type":
        return "singleatomcatalyst" in compact or "sac" in compact or "singleatom" in compact
    if field == "metal_centers":
        return all(_compact(str(item)) in compact for item in value)
    if field == "rate":
        return "02c" in compact
    if isinstance(value, str):
        return _compact(value) in compact
    return bool(compact)


def _json_value(raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _canonical_id(value: Any) -> str:
    text = str(value)
    try:
        return uuid.UUID(text).hex
    except ValueError:
        return text.replace("-", "")


def _display_review_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


def _compact(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).lower()


def _tokens(value: str) -> list[str]:
    return re.findall(r"[0-9A-Za-z\u4e00-\u9fff]+", value.lower())


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
