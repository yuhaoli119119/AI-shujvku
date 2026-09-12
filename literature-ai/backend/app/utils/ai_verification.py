from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.utils.artifact_paths import resolve_paper_pdf_path
from app.db.models import (
    DFTResult,
    ElectrochemicalPerformance,
    EvidenceClaim,
    EvidenceLocator,
    ExtractionFieldReview,
    MechanismClaim,
    Paper,
    PaperRelationship,
    PaperSection,
    PaperTable,
    WritingCard,
)
from app.services.paper_workbench_ai_package import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.services.review_target_resolver import ReviewTargetResolver
from app.services.evidence_page_recovery import PaperPageTextProvider


AI_VERIFICATION_CAPABILITY = "ai_verify_content"
AI_VERIFICATION_POLICY_VERSION = "single_ai_verification.v1"
AI_VERIFIED_STATUS = "ai_verified"
AI_SUPPORTED_TARGET_TYPES = frozenset(
    {
        "mechanism_claims",
        "dft_results",
        "electrochemical_performance",
        "sections",
        "section_page_fragments",
        "writing_cards",
    }
)

_TARGET_ALIASES = {
    "mechanismclaim": "mechanism_claims",
    "mechanism_claim": "mechanism_claims",
    "mechanism_claims": "mechanism_claims",
    "dftresult": "dft_results",
    "dft_result": "dft_results",
    "dft_results": "dft_results",
    "electrochemicalperformance": "electrochemical_performance",
    "electrochemical_performance": "electrochemical_performance",
    "section": "sections",
    "sections": "sections",
    "paper_section": "sections",
    "papersection": "sections",
    "section_page_fragment": "section_page_fragments",
    "section_page_fragments": "section_page_fragments",
    "sectionpagefragment": "section_page_fragments",
    "writing_card": "writing_cards",
    "writing_cards": "writing_cards",
    "writingcard": "writing_cards",
}

_TARGET_MODELS = {
    "mechanism_claims": MechanismClaim,
    "dft_results": DFTResult,
    "electrochemical_performance": ElectrochemicalPerformance,
    "sections": PaperSection,
    "section_page_fragments": EvidenceClaim,
    "writing_cards": WritingCard,
}


def canonical_ai_target_type(value: str) -> str:
    normalized = re.sub(r"[-\s]+", "_", str(value or "").strip().casefold())
    canonical = _TARGET_ALIASES.get(normalized)
    if canonical not in AI_SUPPORTED_TARGET_TYPES:
        raise ValueError(f"Unsupported AI verification target_type: {value}")
    return canonical


def normalize_evidence_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00ad", "").replace("‐", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", text).strip().casefold()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_ai_target(session: Session, *, paper_id: UUID, target_type: str, target_id: str) -> tuple[str, Any]:
    canonical = canonical_ai_target_type(target_type)
    try:
        normalized_id = UUID(str(target_id))
    except ValueError as exc:
        raise LookupError(f"Invalid target_id: {target_id}") from exc
    target = session.get(_TARGET_MODELS[canonical], normalized_id)
    if target is None or getattr(target, "paper_id", None) != paper_id:
        raise LookupError(f"Target not found for {canonical}:{target_id}")
    if canonical == "section_page_fragments" and str(target.source_type or "").casefold() != "section_page_fragment":
        raise LookupError(f"Target not found for {canonical}:{target_id}")
    return canonical, target


def ai_field_snapshot(target_type: str, target: Any, field_name: str) -> dict[str, Any]:
    canonical = canonical_ai_target_type(target_type)
    if canonical == "mechanism_claims":
        fields = {
            "claim_type": {"value": target.claim_type, "unit": None, "evidence_text": target.evidence_text or ""},
            "claim_text": {"value": target.claim_text, "unit": None, "evidence_text": target.evidence_text or ""},
            "key_species": {"value": target.evidence_types or [], "unit": None, "evidence_text": target.evidence_text or ""},
            "mechanism_direction": {"value": None, "unit": None, "evidence_text": target.evidence_text or ""},
        }
    elif canonical == "dft_results":
        from app.services.review_target_resolver import get_dft_catalyst_identity
        from app.utils.configuration_index import extract_configuration_index
        payload = getattr(target, "evidence_payload", None) if not isinstance(target, dict) else target.get("evidence_payload")
        payload = payload if isinstance(payload, dict) else {}
        catalyst_id = getattr(target, "catalyst_sample_id", None) if not isinstance(target, dict) else target.get("catalyst_sample_id")
        catalyst_sample = getattr(target, "catalyst_sample", None) if not isinstance(target, dict) else target.get("catalyst_sample")
        active_site_rows = []
        if catalyst_id and not isinstance(target, dict):
            try:
                from sqlalchemy.orm import object_session
                sess = object_session(target)
                if sess is not None:
                    cached_catalyst, active_site_rows = get_dft_catalyst_identity(sess, catalyst_id)
                    if catalyst_sample is None:
                        catalyst_sample = cached_catalyst
            except Exception:
                pass

        c_name = getattr(catalyst_sample, "name", None) if catalyst_sample else payload.get("material_identity") or payload.get("catalyst_name")
        c_type = getattr(catalyst_sample, "catalyst_type", None) if catalyst_sample else payload.get("catalyst_type")
        c_metals = getattr(catalyst_sample, "metal_centers", None) if catalyst_sample else payload.get("metal_centers") or []
        c_coord = getattr(catalyst_sample, "coordination", None) if catalyst_sample else payload.get("coordination")
        c_supp = getattr(catalyst_sample, "support", None) if catalyst_sample else payload.get("support")
        c_site = payload.get("active_site") or payload.get("active_site_context")
        if active_site_rows:
            c_site = [row.active_site_key for row in active_site_rows]
        cfg_idx = extract_configuration_index(payload)

        fields = {
            "catalyst": {
                "value": str(catalyst_id) if catalyst_id else None,
                "unit": None,
                "evidence_text": (getattr(target, "evidence_text", None) if not isinstance(target, dict) else target.get("evidence_text")) or "",
                "name": c_name,
                "catalyst_type": c_type,
                "metal_centers": c_metals if isinstance(c_metals, list) else ([c_metals] if c_metals else []),
                "coordination": c_coord,
                "support": c_supp,
                "active_site": c_site,
            },
            "adsorbate": {"value": getattr(target, "adsorbate", None) if not isinstance(target, dict) else target.get("adsorbate"), "unit": None, "evidence_text": (getattr(target, "evidence_text", None) if not isinstance(target, dict) else target.get("evidence_text")) or ""},
            "energy_type": {"value": getattr(target, "property_type", None) if not isinstance(target, dict) else target.get("property_type"), "unit": None, "evidence_text": (getattr(target, "evidence_text", None) if not isinstance(target, dict) else target.get("evidence_text")) or ""},
            "value": {
                "value": getattr(target, "value", None) if not isinstance(target, dict) else target.get("value"),
                "unit": getattr(target, "unit", None) if not isinstance(target, dict) else target.get("unit"),
                "evidence_text": (getattr(target, "evidence_text", None) if not isinstance(target, dict) else target.get("evidence_text")) or "",
                "configuration_index": cfg_idx,
            },
            "reaction_step": {"value": getattr(target, "reaction_step", None) if not isinstance(target, dict) else target.get("reaction_step"), "unit": None, "evidence_text": (getattr(target, "evidence_text", None) if not isinstance(target, dict) else target.get("evidence_text")) or ""},
        }
    elif canonical == "electrochemical_performance":
        fields = {
            "sulfur_loading": {"value": target.sulfur_loading_mg_cm2, "unit": "mg/cm2", "evidence_text": target.evidence_text or ""},
            "sulfur_content": {"value": target.sulfur_content_wt_percent, "unit": "wt%", "evidence_text": target.evidence_text or ""},
            "electrolyte_sulfur_ratio": {"value": target.electrolyte_sulfur_ratio, "unit": None, "evidence_text": target.evidence_text or ""},
            "capacity": {"value": target.capacity_value, "unit": "mAh/g", "evidence_text": target.evidence_text or ""},
            "cycle_number": {"value": target.cycle_number, "unit": None, "evidence_text": target.evidence_text or ""},
            "rate": {"value": target.rate, "unit": None, "evidence_text": target.evidence_text or ""},
            "decay_per_cycle": {"value": target.decay_per_cycle, "unit": "%/cycle", "evidence_text": target.evidence_text or ""},
        }
    elif canonical == "sections":
        fields = {"text": {"value": target.text, "unit": None, "evidence_text": target.text or ""}}
    elif canonical == "section_page_fragments":
        fields = {
            "text": {
                "value": target.claim_text,
                "unit": None,
                "evidence_text": target.evidence_text or "",
            }
        }
    else:
        fields = {
            "research_gap": {"value": target.research_gap, "unit": None, "evidence_text": ""},
            "proposed_solution": {"value": target.proposed_solution, "unit": None, "evidence_text": ""},
            "core_hypothesis": {"value": target.core_hypothesis, "unit": None, "evidence_text": ""},
            "evidence_chain": {"value": target.evidence_chain, "unit": None, "evidence_text": ""},
        }
    if field_name not in fields:
        raise ValueError(f"Unsupported field for {canonical}: {field_name}")
    return fields[field_name]


def ai_target_fingerprint(target_type: str, target: Any) -> str:
    canonical = canonical_ai_target_type(target_type)
    if canonical in {"mechanism_claims", "dft_results", "electrochemical_performance"}:
        return ReviewTargetResolver.build_target_fingerprint(ReviewTargetResolver.__new__(ReviewTargetResolver), canonical, target)
    if canonical == "sections":
        payload = {
            "target_type": canonical,
            "id": str(target.id),
            "paper_id": str(target.paper_id),
            "section_title": target.section_title,
            "section_type": target.section_type,
            "text": target.text,
            "page_start": target.page_start,
            "page_end": target.page_end,
        }
    elif canonical == "section_page_fragments":
        payload = {
            "target_type": canonical,
            "id": str(target.id),
            "paper_id": str(target.paper_id),
            "parent_section_id": str(target.section_id) if target.section_id else None,
            "claim_text": target.claim_text,
            "evidence_text": target.evidence_text,
            "page_start": target.page_start,
            "page_end": target.page_end,
            "metadata": target.meta,
        }
    else:
        payload = {
            "target_type": canonical,
            "id": str(target.id),
            "paper_id": str(target.paper_id),
            "research_gap": target.research_gap,
            "proposed_solution": target.proposed_solution,
            "core_hypothesis": target.core_hypothesis,
            "evidence_chain": target.evidence_chain,
            "section_strategy": target.section_strategy,
        }
    return stable_hash(payload)


def locator_fingerprint(locator: EvidenceLocator) -> str:
    payload = {
        "id": str(locator.id),
        "paper_id": str(locator.paper_id),
        "target_type": canonical_ai_target_type(str(locator.target_type or "")),
        "target_id": str(locator.target_id or ""),
        "field_name": str(locator.field_name or ""),
        "page": locator.page,
        "bbox": locator.bbox,
        "evidence_text": normalize_evidence_text(locator.evidence_text),
        "locator_status": str(locator.locator_status or "").casefold(),
    }
    # Preserve historical PDF-locator fingerprints exactly.  Structured table
    # locators add source identity without invalidating existing AI reviews.
    if locator.table_id is not None:
        payload["table_id"] = str(locator.table_id)
    return stable_hash(payload)


def matching_locator(
    session: Session,
    *,
    paper_id: UUID,
    target_type: str,
    target_id: str,
    field_name: str,
    page: int,
    evidence_text: str,
    table_id: UUID | None = None,
) -> EvidenceLocator | None:
    canonical = canonical_ai_target_type(target_type)
    rows = session.scalars(
        select(EvidenceLocator).where(
            EvidenceLocator.paper_id == paper_id,
            EvidenceLocator.target_id == str(target_id),
            EvidenceLocator.field_name == field_name,
            EvidenceLocator.page == page,
            EvidenceLocator.table_id == table_id,
        )
    ).all()
    wanted = normalize_evidence_text(evidence_text)
    for locator in rows:
        try:
            locator_type = canonical_ai_target_type(str(locator.target_type or ""))
        except ValueError:
            continue
        if (
            locator_type == canonical
            and str(locator.locator_status or "").casefold() in {"exact_page", "exact_bbox"}
            and normalize_evidence_text(locator.evidence_text) == wanted
        ):
            return locator
    return None


def _parse_markdown_table(content: str) -> tuple[list[str], list[list[str]]]:
    lines = [
        line.strip()
        for line in str(content or "").splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]
    if len(lines) < 2:
        return [], []
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows: list[list[str]] = []
    for line in lines[1:]:
        if re.fullmatch(r"\|?[\s:\-|+]+\|?", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == len(headers):
            rows.append(cells)
    return headers, rows


def build_structured_table_cell_evidence(
    session: Session,
    *,
    paper_id: UUID,
    table_id: UUID,
    page: int,
    source_row_index: int,
    source_column_index: int,
) -> dict[str, Any] | None:
    """Build server-owned evidence for one exact parsed-table cell.

    Row and column indexes are zero-based indexes into the normalized Markdown
    body and header.  The generated text deliberately keeps the cell context in
    one semicolon-delimited item so a unit from a header may support that cell,
    while a value or unit from another row cannot be spliced into it.
    """
    table = session.get(PaperTable, table_id)
    if table is None or table.paper_id != paper_id or table.page != page:
        return None
    return build_structured_table_cell_evidence_from_table(
        table,
        paper_id=paper_id,
        page=page,
        source_row_index=source_row_index,
        source_column_index=source_column_index,
    )


def build_structured_table_cell_evidence_from_table(
    table: PaperTable,
    *,
    paper_id: UUID,
    page: int,
    source_row_index: int,
    source_column_index: int,
) -> dict[str, Any] | None:
    """Build one cell's canonical evidence from an already-loaded table."""
    if table.paper_id != paper_id or table.page != page:
        return None
    headers, rows = _parse_markdown_table(table.markdown_content or "")
    if (
        source_row_index < 0
        or source_row_index >= len(rows)
        or source_column_index < 0
        or source_column_index >= len(headers)
    ):
        return None
    row = rows[source_row_index]
    if source_column_index >= len(row):
        return None
    header = headers[source_column_index].strip()
    cell = row[source_column_index].strip()
    row_label = row[0].strip() if row else ""
    descriptor = ""
    if source_column_index > 1 and len(row) > 1:
        candidate = row[1].strip()
        if re.search(r"[A-Za-z]", candidate) and not re.search(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", candidate):
            descriptor = candidate
    cell_context = " ".join(
        part for part in (str(table.caption or "").strip(), descriptor, header, cell) if part
    ).strip()
    if not cell or not cell_context:
        return None
    canonical = "; ".join(
        part
        for part in (
            f"caption={str(table.caption or '').strip()}",
            f"row={row_label}",
            f"column={header}",
            f"cell_context={cell_context}",
        )
        if part.split("=", 1)[1]
    )
    return {
        "kind": "paper_table_cell.v1",
        "table_id": str(table.id),
        "source_row_index": source_row_index,
        "source_column_index": source_column_index,
        "canonical_evidence_text": canonical,
        "table_caption": str(table.caption or "").strip(),
        "row_label": row_label,
        "column_header": header,
        "cell_value": cell,
    }


def build_structured_table_cell_evidence_index(table: PaperTable) -> dict[str, list[dict[str, Any]]]:
    """Index every normalized canonical cell value in an already-loaded table.

    Callers must treat an index value with more than one item as ambiguous.  A
    table may legitimately contain repeated values, and selecting the first
    one would silently weaken the evidence contract.
    """
    headers, rows = _parse_markdown_table(table.markdown_content or "")
    index: dict[str, list[dict[str, Any]]] = {}
    for row_index, row in enumerate(rows):
        for column_index in range(min(len(row), len(headers))):
            built = build_structured_table_cell_evidence_from_table(
                table,
                paper_id=table.paper_id,
                page=int(table.page or 0),
                source_row_index=row_index,
                source_column_index=column_index,
            )
            if built is not None:
                index.setdefault(normalize_evidence_text(built["canonical_evidence_text"]), []).append(built)
    return index


def structured_table_cell_evidence_valid(
    session: Session,
    *,
    locator: EvidenceLocator,
    reference: dict[str, Any],
    page_text: str | None,
) -> bool:
    if locator.table_id is None or not isinstance(reference, dict) or not page_text:
        return False
    try:
        table_id = UUID(str(reference.get("table_id") or ""))
        row_index = int(reference["source_row_index"])
        column_index = int(reference["source_column_index"])
        page = int(locator.page)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    if table_id != locator.table_id or row_index < 0 or column_index < 0:
        return False
    built = build_structured_table_cell_evidence(
        session,
        paper_id=locator.paper_id,
        table_id=table_id,
        page=page,
        source_row_index=row_index,
        source_column_index=column_index,
    )
    if built is None or reference.get("kind") != built["kind"]:
        return False
    if normalize_evidence_text(locator.evidence_text) != normalize_evidence_text(built["canonical_evidence_text"]):
        return False

    normalized_page = normalize_evidence_text(page_text)
    label_match = re.search(r"\btable\s+s?\d+\b", built["table_caption"], re.I)
    label_ok = bool(label_match and normalize_evidence_text(label_match.group(0)) in normalized_page)
    row_ok = bool(built["row_label"] and normalize_evidence_text(built["row_label"]) in normalized_page)
    cell_value = str(built["cell_value"] or "")
    cell_ok = bool(cell_value and normalize_evidence_text(cell_value) in normalized_page)
    if not cell_ok:
        page_numeric = normalize_evidence_text(page_text).replace("−", "-").replace("–", "-").replace("—", "-")
        cell_numbers = re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", cell_value.replace("−", "-"))
        cell_ok = bool(cell_numbers) and all(
            re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", page_numeric) is not None
            for number in cell_numbers
        )
    return label_ok and row_ok and cell_ok


def read_pdf_page_text(paper: Paper, page: int) -> tuple[str | None, str | None, Path | None]:
    record = PaperPageTextProvider(get_settings()).read_page(paper, page)
    pdf_path = Path(record.pdf_path) if record.pdf_path else None
    if record.status == "ok":
        return record.text, None, pdf_path
    if record.status == "extraction_failed":
        return None, "unreadable_pdf", pdf_path
    return None, record.status, pdf_path


def cached_read_pdf_page_text(session: Session, paper: Paper, page: int) -> tuple[str | None, str | None, Path | None]:
    """Read each unchanged PDF page once per request/session, including failures."""
    cache = session.info.setdefault("authoritative_pdf_page_cache", {})
    resolved_path = resolve_paper_pdf_path(paper.pdf_path, get_settings().storage_root)
    revision = None
    if resolved_path is not None:
        try:
            stat = resolved_path.stat()
            revision = (
                str(resolved_path),
                stat.st_dev,
                stat.st_ino,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
            )
        except OSError:
            revision = (str(resolved_path), None, None, None, None, None)
    key = (str(paper.id), int(page), revision)
    if key not in cache:
        cache[key] = read_pdf_page_text(paper, page)
    return cache[key]


def exact_locator_geometry_is_valid(locator: EvidenceLocator, pdf_path: Path | None) -> bool:
    """Validate that exact_bbox is real page geometry, not an untrusted label."""
    status = str(locator.locator_status or "").casefold()
    if locator.page is None or locator.page < 1 or status not in {"exact_page", "exact_bbox"}:
        return False
    if status == "exact_page":
        return True
    bbox = locator.bbox
    if not isinstance(bbox, dict) or pdf_path is None or not pdf_path.is_file():
        return False
    try:
        x0, y0, x1, y1 = (float(bbox[key]) for key in ("x0", "y0", "x1", "y1"))
        if not all(math.isfinite(value) for value in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
            return False
        import fitz
        with fitz.open(pdf_path) as document:
            if locator.page > document.page_count:
                return False
            rect = document.load_page(locator.page - 1).rect
        return x0 >= rect.x0 and y0 >= rect.y0 and x1 <= rect.x1 and y1 <= rect.y1
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False


def ai_review_payload_structurally_valid(review: ExtractionFieldReview | dict[str, Any]) -> bool:
    if isinstance(review, dict):
        status = str(review.get("reviewer_status") or review.get("review_status") or review.get("status") or "").casefold()
        resolution = str(review.get("target_resolution_status") or review.get("resolution_status") or "active").casefold()
        payload = review.get("review_payload")
        target_fingerprint = review.get("target_fingerprint")
    else:
        status = str(review.reviewer_status or "").casefold()
        resolution = str(review.target_resolution_status or "").casefold()
        payload = review.review_payload
        target_fingerprint = review.target_fingerprint
    verification = payload.get("ai_verification") if isinstance(payload, dict) else None
    if status != AI_VERIFIED_STATUS or resolution not in {"active", "remapped"} or not isinstance(verification, dict):
        return False
    evidence_checks = verification.get("evidence_checks")
    locator_checks = verification.get("locator_checks")
    required = (
        verification.get("actor_type") == "ai",
        verification.get("identity_verified") is True,
        verification.get("capability") == AI_VERIFICATION_CAPABILITY,
        verification.get("policy_version") == AI_VERIFICATION_POLICY_VERSION,
        verification.get("decision") in {"verified", "corrected"},
        verification.get("single_ai") is True,
        verification.get("second_ai_used") is False,
        bool(str(verification.get("source_identity") or "").strip()),
        bool(str(verification.get("source_label") or "").strip()),
        bool(str(verification.get("model_agent") or "").strip()),
        bool(str(verification.get("created_at") or "").strip()),
        float(verification.get("confidence") or 0) >= get_settings().ai_verification_min_confidence,
        bool(target_fingerprint),
        verification.get("target_snapshot_fingerprint") == target_fingerprint,
        bool(str(verification.get("locator_fingerprint") or "").strip()),
        isinstance(evidence_checks, dict) and bool(evidence_checks) and all(value is True for value in evidence_checks.values()),
        isinstance(locator_checks, dict) and bool(locator_checks) and all(value is True for value in locator_checks.values()),
    )
    return all(required)


def authoritative_ai_review_valid(session: Session, review: ExtractionFieldReview, target: Any) -> bool:
    if not ai_review_payload_structurally_valid(review):
        return False
    verification = review.review_payload["ai_verification"]
    try:
        canonical = canonical_ai_target_type(review.target_type)
        if ai_target_fingerprint(canonical, target) != review.target_fingerprint:
            return False
        page = int(verification.get("page"))
    except (TypeError, ValueError):
        return False

    target_paper_id = review.paper_id
    evidence_paper_str = verification.get("evidence_paper_id") or verification.get("source_paper_id") or str(target_paper_id)
    try:
        evidence_paper_uuid = UUID(str(evidence_paper_str).strip())
    except (ValueError, AttributeError):
        return False

    if evidence_paper_uuid != target_paper_id:
        rel = session.scalar(
            select(PaperRelationship.id).where(
                PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                or_(
                    and_(
                        PaperRelationship.source_paper_id == target_paper_id,
                        PaperRelationship.target_paper_id == evidence_paper_uuid,
                    ),
                    and_(
                        PaperRelationship.target_paper_id == target_paper_id,
                        PaperRelationship.source_paper_id == evidence_paper_uuid,
                    ),
                ),
            ).limit(1)
        )
        if rel is None:
            return False

    table_reference = verification.get("table_evidence")
    table_id: UUID | None = None
    if table_reference is not None:
        if not isinstance(table_reference, dict):
            return False
        try:
            table_id = UUID(str(table_reference.get("table_id") or ""))
        except (ValueError, AttributeError):
            return False

    locator = matching_locator(
        session,
        paper_id=evidence_paper_uuid,
        target_type=canonical,
        target_id=review.target_id,
        field_name=review.field_name,
        page=page,
        evidence_text=str(review.evidence_text or ""),
        table_id=table_id,
    )
    if locator is None or locator_fingerprint(locator) != verification.get("locator_fingerprint"):
        return False
    paper = session.get(Paper, evidence_paper_uuid)
    if paper is None:
        return False
    page_text, error, pdf_path = cached_read_pdf_page_text(session, paper, page)
    if error is not None or not exact_locator_geometry_is_valid(locator, pdf_path):
        return False
    if table_reference is not None:
        return structured_table_cell_evidence_valid(
            session,
            locator=locator,
            reference=table_reference,
            page_text=page_text,
        )
    return normalize_evidence_text(review.evidence_text) in normalize_evidence_text(page_text)
