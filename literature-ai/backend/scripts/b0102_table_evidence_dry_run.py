from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.db.models import (
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExtractionFieldReview,
    Paper,
    PaperTable,
)
from app.db.session import get_db_session
from app.services.ai_verification_service import AIVerificationService
from app.utils.ai_verification import (
    build_structured_table_cell_evidence,
    cached_read_pdf_page_text,
    ai_field_snapshot,
    locator_fingerprint,
    matching_locator,
    normalize_evidence_text,
    structured_table_cell_evidence_valid,
)
from app.utils.review_safety import required_review_fields


SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₋", "0123456789-")


def _chem(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").translate(SUBSCRIPTS).casefold())


def _material(value: Any) -> str:
    normalized = _chem(str(value or "").split("@", 1)[0])
    return "g" if normalized == "graphene" else normalized


def _parse_markdown_table(content: str) -> tuple[list[str], list[list[str]]]:
    lines = [
        line.strip()
        for line in (content or "").splitlines()
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


def _table_label(payload: dict[str, Any]) -> str | None:
    corrected = payload.get("corrected_value")
    corrected = corrected if isinstance(corrected, dict) else {}
    candidates = [corrected.get("source_table"), payload.get("table")]
    candidates.extend(payload.get("evidence_ids") or [])
    for candidate in candidates:
        match = re.search(r"(?:table[: ]*|table:0*)(s?\d+)", str(candidate or ""), re.I)
        if match:
            return match.group(1).upper()
    return None


def _adsorbate_from_header(header: str) -> str | None:
    normalized = _chem(header).replace("easol", "").replace("ea", "")
    for adsorbate in ("li2s8", "li2s6", "li2s4", "li2s2", "li2s", "s8"):
        if adsorbate in normalized:
            return adsorbate
    return None


def _value_matches(value: Any, cell: str) -> bool:
    try:
        expected = float(value)
    except (TypeError, ValueError):
        return False
    numbers = [
        float(token)
        for token in re.findall(
            r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?",
            cell.replace("−", "-"),
        )
    ]
    return any(abs(expected - item) <= max(1e-8, abs(expected) * 1e-6) for item in numbers)


def _unit_is_explicit(unit: Any, parts: list[Any]) -> bool:
    evidence = " ".join(str(part or "") for part in parts)
    normalized = str(unit or "").strip().casefold()
    if normalized == "ev":
        return bool(re.search(r"(?<![A-Za-z])eV(?![A-Za-z])", evidence, re.I))
    if normalized in {"å", "a", "angstrom"}:
        return bool(re.search(r"[ÅÅ]|\bangstroms?\b", evidence, re.I))
    if normalized == "e":
        return bool(re.search(r"(?<![A-Za-z0-9])e(?![A-Za-z0-9])|\belectrons?\b", evidence, re.I))
    return bool(normalized and normalized in evidence.casefold())


def _candidate_columns(
    table_label: str,
    headers: list[str],
    row: list[str],
    dft: DFTResult,
    corrected: dict[str, Any],
    payload: dict[str, Any],
) -> list[int]:
    adsorbate = _chem(dft.adsorbate)
    property_type = str(dft.property_type or "").casefold()
    if table_label == "S1":
        return [index for index, header in enumerate(headers) if index and _adsorbate_from_header(header) == adsorbate]
    if table_label == "S2":
        configuration_index = corrected.get("configuration_index")
        return [configuration_index] if isinstance(configuration_index, int) and 0 < configuration_index < len(headers) else []
    if table_label == "S3":
        if property_type == "bond_length":
            atom_pair = _chem(corrected.get("bond_pair") or payload.get("atom_pair"))
            return [index for index, header in enumerate(headers) if index and atom_pair and atom_pair in _chem(header)]
        if property_type == "adsorption_energy":
            return [index for index, header in enumerate(headers) if index and "ea" in _chem(header)]
        return []
    if table_label == "S4":
        step_label = _chem(corrected.get("step_label"))
        return [index for index, header in enumerate(headers) if index and _chem(header) == step_label]
    if table_label == "S5":
        term = "zpe" if "zero_point" in property_type else "ts" if "entropy" in property_type else ""
        if len(row) < 2 or term != _chem(row[1]).replace("ev", ""):
            return []
        return [index for index, header in enumerate(headers) if index > 1 and _adsorbate_from_header(header) == adsorbate]
    if table_label == "S6":
        return [index for index, header in enumerate(headers) if index and _adsorbate_from_header(header) == adsorbate]
    return []


def main() -> None:
    generator = get_db_session()
    session = next(generator)
    try:
        session.execute(text("SET TRANSACTION READ ONLY"))
        main_paper = session.scalar(select(Paper).where(Paper.paper_code == "B0102"))
        si_paper = session.scalar(select(Paper).where(Paper.paper_code == "S0102"))
        if main_paper is None or si_paper is None:
            raise RuntimeError("B0102 or S0102 paper is missing")

        table_rows = session.scalars(
            select(PaperTable).where(PaperTable.paper_id.in_([main_paper.id, si_paper.id]))
        ).all()
        tables: dict[str, tuple[PaperTable, list[str], list[list[str]]]] = {}
        for table in table_rows:
            match = re.search(r"\bTable\s+(S?\d+)\b", table.caption or "", re.I)
            if match:
                tables[match.group(1).upper()] = (table, *_parse_markdown_table(table.markdown_content or ""))

        results = session.execute(
            select(DFTResult, CatalystSample)
            .outerjoin(CatalystSample, CatalystSample.id == DFTResult.catalyst_sample_id)
            .where(DFTResult.paper_id == main_paper.id)
        ).all()
        outcomes: Counter[str] = Counter()
        by_table: Counter[str] = Counter()
        contract_outcomes: Counter[str] = Counter()
        contract_failures: Counter[str] = Counter()
        contract_failures_by_table: Counter[str] = Counter()
        locator_anchor_failures: Counter[str] = Counter()
        locator_anchor_samples: list[dict[str, str]] = []
        field_contract_outcomes: Counter[str] = Counter()
        field_contract_failures: Counter[str] = Counter()
        service = AIVerificationService(session)

        for dft, catalyst in results:
            payload = dft.evidence_payload if isinstance(dft.evidence_payload, dict) else {}
            corrected = payload.get("corrected_value")
            corrected = corrected if isinstance(corrected, dict) else {}
            table_label = _table_label(payload)
            matches: list[tuple[PaperTable, list[str], list[list[str]], int, int]] = []
            if table_label in tables:
                table, headers, rows = tables[table_label]
                material = corrected.get("material_identity") or payload.get("material_identity") or getattr(catalyst, "name", None)
                material_key = _material(material)
                for row_index, row in enumerate(rows):
                    if not row or _material(row[0]) != material_key:
                        continue
                    for column_index in _candidate_columns(table_label, headers, row, dft, corrected, payload):
                        if column_index < len(row) and _value_matches(dft.value, row[column_index]):
                            matches.append((table, headers, rows, row_index, column_index))

            if len(matches) != 1:
                outcome = "ambiguous_cell" if len(matches) > 1 else "no_unique_cell"
            else:
                table, headers, rows, row_index, column_index = matches[0]
                unit_parts = [table.caption, headers[column_index], rows[row_index][column_index]]
                if table_label == "S5" and len(rows[row_index]) > 1:
                    unit_parts.append(rows[row_index][1])
                outcome = (
                    "unique_cell_explicit_unit"
                    if _unit_is_explicit(dft.unit, unit_parts)
                    else "unique_cell_unit_missing"
                )
                built = build_structured_table_cell_evidence(
                    session,
                    paper_id=table.paper_id,
                    table_id=table.id,
                    page=table.page,
                    source_row_index=row_index,
                    source_column_index=column_index,
                )
                paper = session.get(Paper, table.paper_id)
                page_text, page_error, _ = cached_read_pdf_page_text(session, paper, table.page)
                reference = {
                    "kind": built["kind"],
                    "table_id": built["table_id"],
                    "source_row_index": built["source_row_index"],
                    "source_column_index": built["source_column_index"],
                } if built else None
                locator = EvidenceLocator(
                    paper_id=table.paper_id,
                    source_type="table",
                    target_type="dft_results",
                    target_id=str(dft.id),
                    field_name="value",
                    evidence_text=built["canonical_evidence_text"] if built else "",
                    page=table.page,
                    table_id=table.id,
                    locator_status="exact_page",
                )
                locator_ok = bool(
                    built
                    and page_error is None
                    and structured_table_cell_evidence_valid(
                        session,
                        locator=locator,
                        reference=reference,
                        page_text=page_text,
                    )
                )
                checks = service._content_checks(
                    "dft_results",
                    dft,
                    "value",
                    dft.value,
                    dft.unit,
                    built["canonical_evidence_text"] if built else "",
                )
                failed = [name for name, passed in checks.items() if not passed]
                if not locator_ok:
                    failed.append("structured_table_locator")
                    normalized_page = normalize_evidence_text(page_text)
                    label_match = re.search(r"\btable\s+s?\d+\b", built["table_caption"], re.I) if built else None
                    anchor_checks = {
                        "label": bool(label_match and normalize_evidence_text(label_match.group(0)) in normalized_page),
                        "row": bool(built and normalize_evidence_text(built["row_label"]) in normalized_page),
                        "cell": bool(built and normalize_evidence_text(built["cell_value"]) in normalized_page),
                    }
                    locator_anchor_failures.update(
                        f"{table_label}:{name}" for name, passed in anchor_checks.items() if not passed
                    )
                    if len(locator_anchor_samples) < 8:
                        locator_anchor_samples.append({
                            "table": str(table_label),
                            "row_label": str(built["row_label"] if built else ""),
                            "cell_value": str(built["cell_value"] if built else ""),
                            "failed": ",".join(name for name, passed in anchor_checks.items() if not passed),
                        })
                if failed:
                    contract_outcomes["blocked"] += 1
                    contract_failures.update(failed)
                    contract_failures_by_table.update(
                        f"{table_label}:{reason}" for reason in failed
                    )
                else:
                    contract_outcomes["passed"] += 1
                for field_name in required_review_fields("dft_results", dft):
                    snapshot = ai_field_snapshot("dft_results", dft, field_name)
                    field_checks = service._content_checks(
                        "dft_results",
                        dft,
                        field_name,
                        snapshot["value"],
                        snapshot.get("unit"),
                        built["canonical_evidence_text"] if built else "",
                    )
                    field_failed = [name for name, passed in field_checks.items() if not passed]
                    if not locator_ok:
                        field_failed.append("structured_table_locator")
                    status = "blocked" if field_failed else "passed"
                    field_contract_outcomes[f"{field_name}:{status}"] += 1
                    field_contract_failures.update(
                        f"{field_name}:{reason}" for reason in field_failed
                    )
            outcomes[outcome] += 1
            by_table[f"{table_label or 'none'}:{outcome}"] += 1

        historical_fingerprints: Counter[str] = Counter()
        ai_reviews = session.scalars(
            select(ExtractionFieldReview).where(
                ExtractionFieldReview.reviewer_status == "ai_verified"
            )
        ).all()
        for review in ai_reviews:
            payload = review.review_payload if isinstance(review.review_payload, dict) else {}
            verification = payload.get("ai_verification") if isinstance(payload, dict) else None
            if not isinstance(verification, dict) or verification.get("table_evidence") is not None:
                continue
            try:
                evidence_paper_id = UUID(str(
                    verification.get("evidence_paper_id")
                    or verification.get("source_paper_id")
                    or review.paper_id
                ))
                page = int(verification.get("page"))
            except (TypeError, ValueError, AttributeError):
                historical_fingerprints["invalid_reference"] += 1
                continue
            locator = matching_locator(
                session,
                paper_id=evidence_paper_id,
                target_type=review.target_type,
                target_id=review.target_id,
                field_name=review.field_name,
                page=page,
                evidence_text=str(review.evidence_text or ""),
            )
            if locator is None:
                historical_fingerprints["locator_missing"] += 1
            elif locator_fingerprint(locator) == verification.get("locator_fingerprint"):
                historical_fingerprints["match"] += 1
            else:
                historical_fingerprints["mismatch"] += 1

        session.rollback()
        print(json.dumps({
            "paper_code": "B0102",
            "dft_result_count": len(results),
            "outcomes": dict(sorted(outcomes.items())),
            "by_table": dict(sorted(by_table.items())),
            "structured_value_contract": dict(sorted(contract_outcomes.items())),
            "structured_value_block_reasons": dict(sorted(contract_failures.items())),
            "structured_value_block_reasons_by_table": dict(sorted(contract_failures_by_table.items())),
            "locator_anchor_failures": dict(sorted(locator_anchor_failures.items())),
            "locator_anchor_samples": locator_anchor_samples,
            "field_contract_outcomes": dict(sorted(field_contract_outcomes.items())),
            "field_contract_block_reasons": dict(sorted(field_contract_failures.items())),
            "historical_plain_locator_fingerprints": dict(sorted(historical_fingerprints.items())),
            "database_writes": False,
        }, ensure_ascii=False, indent=2))
    finally:
        session.rollback()
        generator.close()


if __name__ == "__main__":
    main()
