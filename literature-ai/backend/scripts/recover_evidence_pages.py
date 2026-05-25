from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.models import EvidenceClaim, EvidenceLocator, EvidenceSpan
from app.db.session import session_scope


MIN_EXACT_MATCH_CHARS = 30
MIN_APPROXIMATE_CHARS = 80
APPROXIMATE_SCORE = 0.72


@dataclass(frozen=True)
class EvidencePageDecision:
    evidence_type: str
    evidence_id: str
    paper_id: str
    evidence_text: str
    existing_page: int | None
    decision: str
    locator_status: str
    provenance_level: str
    proposed_page: int | None
    confidence: float
    reason: str
    apply_eligible: bool


def _table_names(session: Session) -> set[str]:
    return set(inspect(session.bind).get_table_names())


def _columns(session: Session, table_name: str) -> set[str]:
    if table_name not in _table_names(session):
        return set()
    return {column["name"] for column in inspect(session.bind).get_columns(table_name)}


def _get_attr(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _valid_page(value: Any) -> int | None:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _resolve_artifact_path(path_value: str | None, storage_root: Path | None) -> Path | None:
    if not path_value:
        return None
    candidate = Path(path_value)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    candidates = []
    if storage_root is not None:
        candidates.append(storage_root / candidate)
    candidates.extend([BACKEND_ROOT / candidate, BACKEND_ROOT.parent / candidate, Path.cwd() / candidate])
    for path in candidates:
        if path.exists():
            return path
    return None


def _page_from_payload(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("page", "page_no", "page_number", "page_index", "source_page", "pdf_page"):
            page = _valid_page(value.get(key))
            if page is not None:
                return page + 1 if key == "page_index" and page == 0 else page
        prov = value.get("prov")
        if isinstance(prov, list):
            for item in prov:
                page = _page_from_payload(item)
                if page is not None:
                    return page
        if isinstance(prov, dict):
            return _page_from_payload(prov)
    return None


def _text_from_payload(value: Any) -> str:
    if isinstance(value, dict):
        parts = []
        for key in ("text", "content", "markdown", "caption", "raw_text"):
            item = value.get(key)
            if isinstance(item, str):
                parts.append(item)
        if not parts:
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    nested_text = _text_from_payload(nested)
                    if nested_text:
                        parts.append(nested_text)
        return "\n".join(parts)
    if isinstance(value, list):
        return "\n".join(_text_from_payload(item) for item in value)
    if isinstance(value, str):
        return value
    return ""


def _page_texts_from_docling(path: Path) -> dict[int, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    page_texts: dict[int, list[str]] = {}

    def visit(value: Any, inherited_page: int | None = None) -> None:
        page = _page_from_payload(value) if isinstance(value, dict) else None
        page = page or inherited_page
        if isinstance(value, dict):
            if page is not None:
                text = _text_from_payload(value)
                if text:
                    page_texts.setdefault(page, []).append(text)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    visit(nested, page)
        elif isinstance(value, list):
            for item in value:
                visit(item, page)

    visit(payload)
    return {page: "\n".join(parts) for page, parts in page_texts.items() if parts}


def _page_texts_from_markdown(path: Path) -> dict[int, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    matches = list(re.finditer(r"(?im)^#{1,3}\s*page\s+(\d+)\s*$", text))
    if not matches:
        return {}
    pages: dict[int, str] = {}
    for index, match in enumerate(matches):
        page = _valid_page(match.group(1))
        if page is None:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages[page] = text[start:end]
    return pages


def _paper_rows(session: Session, paper_id: str | None = None) -> dict[Any, dict[str, Any]]:
    if "papers" not in _table_names(session):
        return {}
    cols = _columns(session, "papers")
    selected = ["id"]
    for col in ("docling_json_path", "markdown_path"):
        if col in cols:
            selected.append(col)
    where = ""
    params: dict[str, Any] = {}
    if paper_id:
        where = " WHERE CAST(id AS TEXT) = :paper_id"
        params["paper_id"] = paper_id
    rows = session.execute(text(f"SELECT {', '.join(selected)} FROM papers{where}"), params).mappings()
    papers: dict[Any, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item.setdefault("docling_json_path", None)
        item.setdefault("markdown_path", None)
        papers[str(item["id"])] = item
    return papers


def build_page_text_index(session: Session, paper: Any, storage_root: Path | None = None) -> dict[int, str]:
    page_texts: dict[int, list[str]] = {}
    paper_id = _get_attr(paper, "id")
    if "paper_sections" in _table_names(session):
        cols = _columns(session, "paper_sections")
        if {"paper_id", "text"}.issubset(cols):
            page_start_col = "page_start" if "page_start" in cols else "NULL AS page_start"
            page_end_col = "page_end" if "page_end" in cols else "NULL AS page_end"
            rows = session.execute(
                text(
                    "SELECT text, "
                    f"{page_start_col}, {page_end_col} "
                    "FROM paper_sections WHERE CAST(paper_id AS TEXT) = :paper_id "
                    "OR REPLACE(CAST(paper_id AS TEXT), '-', '') = REPLACE(:paper_id, '-', '')"
                ),
                {"paper_id": str(paper_id)},
            ).mappings()
            for section in rows:
                page = _valid_page(section["page_start"]) or _valid_page(section["page_end"])
                if page is None or not section["text"]:
                    continue
                page_texts.setdefault(page, []).append(section["text"])

    docling_path = _resolve_artifact_path(_get_attr(paper, "docling_json_path"), storage_root)
    if docling_path is not None:
        for page, text_value in _page_texts_from_docling(docling_path).items():
            page_texts.setdefault(page, []).append(text_value)

    markdown_path = _resolve_artifact_path(_get_attr(paper, "markdown_path"), storage_root)
    if markdown_path is not None:
        for page, text_value in _page_texts_from_markdown(markdown_path).items():
            page_texts.setdefault(page, []).append(text_value)

    return {page: _normalize_text("\n".join(parts)) for page, parts in page_texts.items() if _normalize_text("\n".join(parts))}


def _approximate_candidate(evidence_text: str, page_texts: dict[int, str]) -> tuple[int | None, float]:
    best_page = None
    best_score = 0.0
    second_score = 0.0
    for page, page_text in page_texts.items():
        window = page_text[: max(len(evidence_text) * 3, 4000)]
        score = SequenceMatcher(None, evidence_text.lower(), window.lower()).ratio()
        if score > best_score:
            second_score = best_score
            best_score = score
            best_page = page
        elif score > second_score:
            second_score = score
    if best_page is not None and best_score >= APPROXIMATE_SCORE and best_score - second_score >= 0.15:
        return best_page, round(best_score, 3)
    return None, round(best_score, 3)


def decide_page_recovery(
    *,
    evidence_type: str,
    evidence_id: str,
    paper_id: str,
    evidence_text: str,
    existing_page: int | None,
    page_texts: dict[int, str],
) -> EvidencePageDecision:
    text = _normalize_text(evidence_text)
    if existing_page is not None:
        return EvidencePageDecision(
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            paper_id=paper_id,
            evidence_text=text,
            existing_page=existing_page,
            decision="exact_recovered",
            locator_status="exact_page",
            provenance_level="exact_pdf_page",
            proposed_page=existing_page,
            confidence=1.0,
            reason="evidence already has a valid page",
            apply_eligible=False,
        )
    if not text:
        return EvidencePageDecision(
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            paper_id=paper_id,
            evidence_text=text,
            existing_page=None,
            decision="unresolved",
            locator_status="unresolved",
            provenance_level="unavailable",
            proposed_page=None,
            confidence=0.0,
            reason="evidence text is missing",
            apply_eligible=False,
        )
    if len(text) < MIN_EXACT_MATCH_CHARS:
        return EvidencePageDecision(
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            paper_id=paper_id,
            evidence_text=text,
            existing_page=None,
            decision="text_too_short",
            locator_status="text_only",
            provenance_level="text_evidence_only",
            proposed_page=None,
            confidence=0.0,
            reason="evidence text is too short for reliable page recovery",
            apply_eligible=False,
        )
    if not page_texts:
        return EvidencePageDecision(
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            paper_id=paper_id,
            evidence_text=text,
            existing_page=None,
            decision="missing_artifact",
            locator_status="text_only",
            provenance_level="text_evidence_only",
            proposed_page=None,
            confidence=0.0,
            reason="no parsed page text artifact is available",
            apply_eligible=False,
        )

    matches = [page for page, page_text in page_texts.items() if text.lower() in page_text.lower()]
    if len(matches) == 1:
        return EvidencePageDecision(
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            paper_id=paper_id,
            evidence_text=text,
            existing_page=None,
            decision="exact_recovered",
            locator_status="exact_page",
            provenance_level="exact_pdf_page",
            proposed_page=matches[0],
            confidence=0.95,
            reason="evidence text is a unique complete match in parsed page text",
            apply_eligible=True,
        )
    if len(matches) > 1:
        return EvidencePageDecision(
            evidence_type=evidence_type,
            evidence_id=evidence_id,
            paper_id=paper_id,
            evidence_text=text,
            existing_page=None,
            decision="ambiguous_match",
            locator_status="approximate",
            provenance_level="approximate_pdf_page",
            proposed_page=None,
            confidence=0.45,
            reason=f"evidence text appears on multiple pages: {matches}",
            apply_eligible=False,
        )

    if len(text) >= MIN_APPROXIMATE_CHARS:
        proposed_page, score = _approximate_candidate(text, page_texts)
        if proposed_page is not None:
            return EvidencePageDecision(
                evidence_type=evidence_type,
                evidence_id=evidence_id,
                paper_id=paper_id,
                evidence_text=text,
                existing_page=None,
                decision="approximate_candidate",
                locator_status="approximate",
                provenance_level="approximate_pdf_page",
                proposed_page=proposed_page,
                confidence=score,
                reason="closest page text is approximate and requires human confirmation",
                apply_eligible=False,
            )

    return EvidencePageDecision(
        evidence_type=evidence_type,
        evidence_id=evidence_id,
        paper_id=paper_id,
        evidence_text=text,
        existing_page=None,
        decision="unresolved",
        locator_status="unresolved",
        provenance_level="unavailable",
        proposed_page=None,
        confidence=0.0,
        reason="evidence text was not found in parsed page text",
        apply_eligible=False,
    )


def _iter_evidence_rows(session: Session, paper_id: str | None = None) -> list[tuple[str, Any, str, int | None]]:
    tables = _table_names(session)
    rows: list[tuple[str, Any, str, int | None]] = []
    if "evidence_spans" in tables:
        stmt = select(EvidenceSpan)
        if paper_id:
            stmt = stmt.where(EvidenceSpan.paper_id == paper_id)
        for row in session.scalars(stmt).all():
            rows.append(("evidence_span", row, row.text, _valid_page(row.page)))
    if "evidence_claims" in tables:
        stmt = select(EvidenceClaim)
        if paper_id:
            stmt = stmt.where(EvidenceClaim.paper_id == paper_id)
        for row in session.scalars(stmt).all():
            rows.append(("evidence_claim", row, row.evidence_text, _valid_page(row.page_start) or _valid_page(row.page_end)))
    if "evidence_locators" in tables:
        stmt = select(EvidenceLocator)
        if paper_id:
            stmt = stmt.where(EvidenceLocator.paper_id == paper_id)
        for row in session.scalars(stmt).all():
            rows.append(("evidence_locator", row, row.evidence_text, _valid_page(row.page)))
    return rows


def analyze_evidence_pages(
    session: Session,
    *,
    paper_id: str | None = None,
    limit: int | None = None,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    papers = _paper_rows(session, paper_id)
    page_index_cache: dict[Any, dict[int, str]] = {}
    decisions: list[EvidencePageDecision] = []
    for evidence_type, row, evidence_text, existing_page in _iter_evidence_rows(session, paper_id):
        if limit is not None and len(decisions) >= limit:
            break
        paper = papers.get(str(row.paper_id)) or {"id": row.paper_id, "docling_json_path": None, "markdown_path": None}
        page_texts = page_index_cache.setdefault(str(row.paper_id), build_page_text_index(session, paper, storage_root))
        decisions.append(
            decide_page_recovery(
                evidence_type=evidence_type,
                evidence_id=str(row.id),
                paper_id=str(row.paper_id),
                evidence_text=evidence_text,
                existing_page=existing_page,
                page_texts=page_texts,
            )
        )

    counts = Counter(decision.decision for decision in decisions)
    locator_counts = Counter(decision.locator_status for decision in decisions)
    summary = {
        "evidence_total": len(decisions),
        "exact_recovered": counts["exact_recovered"],
        "approximate_candidate": counts["approximate_candidate"],
        "ambiguous_match": counts["ambiguous_match"],
        "text_only": locator_counts["text_only"],
        "missing_artifact": counts["missing_artifact"],
        "text_too_short": counts["text_too_short"],
        "unresolved": counts["unresolved"],
        "proposed_apply_count": sum(1 for decision in decisions if decision.apply_eligible),
        "dry_run": True,
    }
    return {"summary": summary, "decisions": [asdict(decision) for decision in decisions]}


def apply_recovery_decisions(session: Session, decisions: list[dict[str, Any]]) -> int:
    applied = 0
    for decision in decisions:
        if not decision.get("apply_eligible") or decision.get("decision") != "exact_recovered":
            continue
        page = _valid_page(decision.get("proposed_page"))
        if page is None:
            continue
        evidence_id = UUID(decision["evidence_id"])
        evidence_type = decision["evidence_type"]
        if evidence_type == "evidence_span":
            row = session.get(EvidenceSpan, evidence_id)
            if row is not None and row.page is None:
                row.page = page
                applied += 1
        elif evidence_type == "evidence_claim":
            row = session.get(EvidenceClaim, evidence_id)
            if row is not None and row.page_start is None and row.page_end is None:
                row.page_start = page
                row.page_end = page
                applied += 1
        elif evidence_type == "evidence_locator":
            row = session.get(EvidenceLocator, evidence_id)
            if row is not None and row.page is None:
                row.page = page
                row.locator_status = "exact_page"
                row.locator_confidence = max(float(row.locator_confidence or 0), float(decision.get("confidence") or 0))
                row.warning_reason = "page recovered from unique complete parsed page text match"
                applied += 1
    return applied


def print_report(report: dict[str, Any], *, applied: int = 0) -> None:
    summary = report["summary"]
    print("D1 evidence page recovery dry-run")
    print(
        "evidence_total={evidence_total} exact_recovered={exact_recovered} "
        "approximate_candidate={approximate_candidate} ambiguous_match={ambiguous_match} "
        "text_only={text_only} missing_artifact={missing_artifact} "
        "text_too_short={text_too_short} unresolved={unresolved} "
        "proposed_apply_count={proposed_apply_count}".format(**summary)
    )
    if applied:
        print(f"applied={applied}")
    else:
        print("No changes written.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover evidence page numbers using conservative parsed-artifact matching.")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing changes. This is the default.")
    parser.add_argument("--apply", action="store_true", help="Apply only exact unique page recoveries. Never writes bbox or reviews.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--paper-id", help="Optional paper UUID/text id to scope recovery.")
    parser.add_argument("--limit", type=int, help="Limit evidence rows scanned.")
    args = parser.parse_args()

    settings = get_settings()
    storage_root = Path(settings.storage_root) if settings.storage_root else None
    with session_scope(settings.database_url) as session:
        report = analyze_evidence_pages(session, paper_id=args.paper_id, limit=args.limit, storage_root=storage_root)
        applied = 0
        if args.apply:
            applied = apply_recovery_decisions(session, report["decisions"])
            report["summary"]["dry_run"] = False
            report["summary"]["applied"] = applied
        else:
            session.rollback()

    if args.json:
        output = {**report, "applied": applied}
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_report(report, applied=applied)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
