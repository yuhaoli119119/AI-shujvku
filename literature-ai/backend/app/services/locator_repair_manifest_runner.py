from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.locator_recovery_helper import (
    LocatorRecoveryRequest,
    build_locator_repair_proposal,
)


PILOT_PAPER_ID = "3978dc79f94f4457863fd68449ae293d"

YELLOW_REVIEW_IDS = (
    "e2c75b7f2d9c41ffa6e1e95e5d491896",
    "09f836768f134e82a576ab359b264933",
    "280f2d9e3ebb41079702f6ea6d645465",
    "56f7258445b3465b9a4097ec60a2fabf",
)

RED_REVIEW_ID = "4ba0e4905934439c813633a8ddf4e201"

RED_EXCLUSION_REASON = "no reliable source artifact / extracted empty-settings dict"


@dataclass(frozen=True)
class ReviewRow:
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
class EvidenceSpanRow:
    object_type: str
    object_id: str
    text: str
    confidence: float | None


class ReadOnlyLocatorRepairManifestRunner:
    """Build a human-review proposal manifest without any persistence path."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        library_root: str | Path | None = None,
        docling_blocks: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.library_root = Path(library_root).resolve() if library_root is not None else self.db_path.parent
        self._docling_blocks_override = docling_blocks

    def build_manifest(self) -> dict[str, Any]:
        rows, paper, spans, locator_count = self._read_active_db_snapshot()
        rows_by_id = {row.id: row for row in rows}
        docling_blocks = self._docling_blocks_override
        if docling_blocks is None:
            docling_blocks = tuple(self._load_docling_blocks(paper.get("docling_json_path")))

        proposals = []
        for review_id in YELLOW_REVIEW_IDS:
            row = rows_by_id.get(review_id)
            if row is None:
                proposals.append(self._missing_review_proposal(review_id))
                continue
            proposal = self._build_yellow_proposal(row, spans, docling_blocks)
            proposals.append(proposal)

        red_row = rows_by_id.get(RED_REVIEW_ID)
        exclusions = [self._red_exclusion(red_row)]

        return {
            "manifest_type": "d4_3g_read_only_locator_repair_proposal",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "active_db_path": str(self.db_path),
            "active_db_read_mode": "sqlite_uri_mode_ro",
            "paper_id": PILOT_PAPER_ID,
            "paper_title": paper.get("title"),
            "proposal_count": len(proposals),
            "red_exclusion_count": len(exclusions),
            "active_db_locator_count_for_pilot": locator_count,
            "safety": {
                "writes_active_db": False,
                "writes_locator": False,
                "mark_verified": False,
                "save_reviews": False,
                "migration_apply": False,
                "extraction_or_reprocessing_apply": False,
                "materialize": False,
                "registry_write": False,
                "export_unlocked": False,
                "writing_unlocked": False,
            },
            "proposals": proposals,
            "excluded": exclusions,
        }

    def _read_active_db_snapshot(
        self,
    ) -> tuple[list[ReviewRow], dict[str, Any], list[EvidenceSpanRow], int]:
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

            review_ids = (*YELLOW_REVIEW_IDS, RED_REVIEW_ID)
            placeholders = ",".join("?" for _ in review_ids)
            review_rows = connection.execute(
                f"""
                SELECT id, paper_id, target_type, target_id, field_name,
                       original_value, reviewed_value, evidence_text, reviewer_status,
                       target_resolution_status, target_label
                FROM extraction_field_reviews
                WHERE paper_id = ? AND id IN ({placeholders})
                ORDER BY field_name
                """,
                (PILOT_PAPER_ID, *review_ids),
            ).fetchall()

            span_rows = connection.execute(
                """
                SELECT object_type, object_id, text, confidence
                FROM evidence_spans
                WHERE paper_id = ?
                """,
                (PILOT_PAPER_ID,),
            ).fetchall()

            locator_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM evidence_locators WHERE paper_id = ?",
                    (PILOT_PAPER_ID,),
                ).fetchone()[0]
                or 0
            )

        return (
            [_review_from_row(row) for row in review_rows],
            dict(paper_row),
            [_span_from_row(row) for row in span_rows],
            locator_count,
        )

    def _load_docling_blocks(self, stored_path: str | None) -> list[dict[str, Any]]:
        docling_path = self._resolve_artifact_path(stored_path)
        if docling_path is None:
            return []
        payload = json.loads(docling_path.read_text(encoding="utf-8"))
        blocks: list[dict[str, Any]] = []
        for item in payload.get("texts", []):
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            self_ref = str(item.get("self_ref") or "")
            blocks.append(
                {
                    "text": text,
                    "source_artifact": f"{docling_path.as_posix()}:{self_ref}" if self_ref else docling_path.as_posix(),
                    "self_ref": self_ref or None,
                    "prov": item.get("prov"),
                }
            )
        return blocks

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
        basename = raw.name
        if basename:
            for root in (self.library_root, self.library_root / "storage"):
                candidates.append(root / basename)
                if root.exists():
                    matches = list(root.rglob(basename))
                    candidates.extend(matches)

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

    def _build_yellow_proposal(
        self,
        row: ReviewRow,
        spans: list[EvidenceSpanRow],
        docling_blocks: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        evidence_text, evidence_warnings = _select_evidence_text(row, spans)
        candidate_blocks = _candidate_blocks_for_evidence(docling_blocks, evidence_text)
        if candidate_blocks and len(candidate_blocks) != len(docling_blocks):
            evidence_warnings.append("docling_candidates_narrowed_to_target_evidence_span")
        helper_proposal = build_locator_repair_proposal(
            LocatorRecoveryRequest(
                paper_id=row.paper_id,
                review_id=_display_review_id(row.id),
                field_name=row.field_name,
                target_value=row.proposed_value,
                evidence_text=evidence_text,
                docling_blocks=candidate_blocks or docling_blocks,
            )
        )

        warnings = list(helper_proposal.warnings)
        warnings.extend(evidence_warnings)
        if row.evidence_text and row.evidence_text != evidence_text:
            warnings.append("current_review_evidence_text_not_used_as_locator_query")
        if row.field_name == "name":
            warnings.append("normalized_aggregate_value_not_literal_source_phrase")

        blockers = list(helper_proposal.blockers)
        if not evidence_text:
            blockers.append("no_target_specific_evidence_span")
        if helper_proposal.source_artifact is None:
            blockers.append("no_reliable_source_artifact_match")

        return {
            "review_id": _display_review_id(row.id),
            "paper_id": row.paper_id,
            "field": row.field_name,
            "value": row.proposed_value,
            "proposal_status": helper_proposal.status,
            "proposed_locator_status": helper_proposal.proposed_locator_status,
            "proposed_page": helper_proposal.page,
            "proposed_bbox": helper_proposal.bbox,
            "matched_text": helper_proposal.matched_text,
            "source_artifact": helper_proposal.source_artifact,
            "match_method": helper_proposal.match_method,
            "confidence": helper_proposal.confidence,
            "warnings": _unique(warnings),
            "blockers": _unique(blockers),
            "requires_human_confirmation": True,
            "should_write_locator": False,
            "mark_verified": False,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
        }

    @staticmethod
    def _missing_review_proposal(review_id: str) -> dict[str, Any]:
        return {
            "review_id": _display_review_id(review_id),
            "paper_id": PILOT_PAPER_ID,
            "field": None,
            "value": None,
            "proposal_status": "red",
            "proposed_locator_status": "missing_locator",
            "proposed_page": None,
            "proposed_bbox": None,
            "matched_text": None,
            "source_artifact": None,
            "match_method": "missing_review_row",
            "confidence": 0.0,
            "warnings": ["proposal_not_verified", "does_not_unlock_export_or_writing"],
            "blockers": ["expected_yellow_review_row_missing"],
            "requires_human_confirmation": True,
            "should_write_locator": False,
            "mark_verified": False,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
        }

    @staticmethod
    def _red_exclusion(row: ReviewRow | None) -> dict[str, Any]:
        return {
            "review_id": _display_review_id(row.id if row else RED_REVIEW_ID),
            "paper_id": PILOT_PAPER_ID,
            "field": "convergence_settings",
            "status": "RED",
            "proposal": "none",
            "reason": RED_EXCLUSION_REASON,
            "should_write_locator": False,
            "requires_human_confirmation": True,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
        }


def build_d4_3g_manifest(
    db_path: str | Path,
    *,
    library_root: str | Path | None = None,
    docling_blocks: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    return ReadOnlyLocatorRepairManifestRunner(
        db_path,
        library_root=library_root,
        docling_blocks=docling_blocks,
    ).build_manifest()


def write_manifest_json(manifest: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _review_from_row(row: sqlite3.Row) -> ReviewRow:
    return ReviewRow(
        id=str(row["id"]),
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


def _span_from_row(row: sqlite3.Row) -> EvidenceSpanRow:
    return EvidenceSpanRow(
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        text=str(row["text"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
    )


def _json_value(raw: Any) -> Any:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _select_evidence_text(row: ReviewRow, spans: list[EvidenceSpanRow]) -> tuple[str | None, list[str]]:
    relevant = [
        span
        for span in spans
        if _target_type_matches(row.target_type, span.object_type) and span.object_id == row.target_id
    ]
    warnings: list[str] = []

    if row.field_name == "catalyst_type":
        text = _first_span_text(relevant, ("single-atom catalyst", "SAC"))
    elif row.field_name in {"name", "metal_centers"}:
        text = _first_span_text(relevant, ("铁(Fe)", "钴(Co)", "钒(V)", "Fe", "Co", "V"))
    elif row.field_name == "rate":
        text = _first_span_text(relevant, ("0.2 C", "S/TiN-VN@CNFs", "Cycling performances"))
    else:
        text = row.evidence_text

    if text is None:
        warnings.append("target_specific_evidence_span_missing")
    elif row.evidence_text and row.evidence_text != text:
        warnings.append("target_specific_evidence_span_used_for_locator_query")
    return text, warnings


def _target_type_matches(review_target_type: str, span_object_type: str) -> bool:
    if review_target_type == span_object_type:
        return True
    return review_target_type.rstrip("s") == span_object_type.rstrip("s")


def _first_span_text(spans: list[EvidenceSpanRow], required_terms: tuple[str, ...]) -> str | None:
    for span in spans:
        if all(term in span.text for term in required_terms):
            return span.text
    for span in spans:
        if any(term in span.text for term in required_terms):
            return span.text
    return spans[0].text if spans else None


def _candidate_blocks_for_evidence(
    docling_blocks: tuple[dict[str, Any], ...],
    evidence_text: str | None,
) -> tuple[dict[str, Any], ...]:
    if not evidence_text:
        return ()
    evidence_compact = _compact_for_match(evidence_text)
    if not evidence_compact:
        return ()
    matches = [
        block
        for block in docling_blocks
        if evidence_compact in _compact_for_match(str(block.get("text") or ""))
    ]
    return tuple(matches)


def _compact_for_match(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _display_review_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
