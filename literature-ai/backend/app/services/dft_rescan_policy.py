from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SOURCE_DOCUMENT_TYPES = {
    "main_text",
    "supplementary_information",
    "supporting_reference",
    "unknown",
}

EXCLUDED_NUMERIC_SIGNAL_REASONS = {
    "duplicate",
    "page_number",
    "citation_number",
    "axis_tick",
    "experimental_condition",
    "unreadable_image_value",
}

DFT_METHOD_ONLY_REACTION_STEP_TOKENS = {
    "bj",
    "b3lyp",
    "blyp",
    "calculation",
    "calculations",
    "d2",
    "d3",
    "d4",
    "dispersion",
    "dft",
    "dftd",
    "functional",
    "gga",
    "grimme",
    "hse06",
    "lda",
    "method",
    "optb86b",
    "optb88",
    "optpbe",
    "paw",
    "pbe",
    "pbe0",
    "pbesol",
    "pw91",
    "revpbe",
    "rpbe",
    "u",
    "vasp",
    "vdw",
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def is_dft_method_only_reaction_step(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    tokens = re.findall(r"[a-z0-9]+", text)
    return bool(tokens) and all(token in DFT_METHOD_ONLY_REACTION_STEP_TOKENS for token in tokens)


def normalize_dft_reaction_step_for_identity(value: Any) -> str:
    if is_dft_method_only_reaction_step(value):
        return ""
    return _text(value)


def normalize_source_document_type(value: Any) -> str:
    text = _text(value).replace("-", "_").replace(" ", "_")
    aliases = {
        "main": "main_text",
        "main_pdf": "main_text",
        "paper": "main_text",
        "正文": "main_text",
        "si": "supplementary_information",
        "supplementary": "supplementary_information",
        "supplementary_material": "supplementary_information",
        "supplementary_information": "supplementary_information",
        "supporting_ref": "supporting_reference",
        "supporting_reference": "supporting_reference",
        "reference": "supporting_reference",
        "cited_reference": "supporting_reference",
    }
    return aliases.get(text, text if text in SOURCE_DOCUMENT_TYPES else "unknown")


def normalize_unit(value: Any) -> str:
    text = _text(value).replace(" ", "")
    if text in {"ev", "electronvolt", "electronvolts"}:
        return "eV"
    if text in {"mev"}:
        return "meV"
    if text in {"v", "volt", "volts"}:
        return "V"
    if text in {"kjmol-1", "kj/mol", "kjmol^-1"}:
        return "kJ/mol"
    if text in {"kcalmol-1", "kcal/mol", "kcalmol^-1"}:
        return "kcal/mol"
    return text


def normalize_numeric_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
        if not match:
            return _text(value)
        number = float(match.group(0))
    return f"{number:.6g}"


def _first_payload(*payloads: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", []):
                return value
    return None


def _owned_source_bucket(source_document_type: str) -> str:
    if source_document_type == "supporting_reference":
        return "supporting_reference"
    return "paper_owned"


def build_dft_dedupe_signature(payload: dict[str, Any]) -> str:
    """Build a stable DFT value signature without page/table locator fields.

    Main-text and SI occurrences are intentionally placed in the same
    paper-owned bucket so repeated evidence for the same paper data merges
    instead of creating a new row. Supporting-reference data remains separate.
    """

    corrected = payload.get("corrected_value") if isinstance(payload.get("corrected_value"), dict) else {}
    evidence = payload.get("evidence_payload") if isinstance(payload.get("evidence_payload"), dict) else {}
    location = payload.get("evidence_location") if isinstance(payload.get("evidence_location"), dict) else {}
    source_type = normalize_source_document_type(
        _first_payload(payload, evidence, location, keys=("source_document_type", "source_type"))
    )
    parts = {
        "paper_id": _text(payload.get("paper_id")),
        "source_bucket": _owned_source_bucket(source_type),
        "material": _text(
            _first_payload(
                payload,
                corrected,
                keys=("normalized_material_or_catalyst", "normalized_material", "material", "catalyst", "catalyst_name"),
            )
        ),
        "adsorbate": _text(_first_payload(payload, corrected, keys=("normalized_adsorbate", "adsorbate"))),
        "property_type": _text(
            _first_payload(payload, corrected, keys=("normalized_property_type", "property_type", "energy_type"))
        ),
        "reaction_step": normalize_dft_reaction_step_for_identity(
            _first_payload(payload, corrected, keys=("normalized_reaction_step", "reaction_step"))
        ),
        "value": normalize_numeric_value(_first_payload(payload, corrected, keys=("normalized_value", "value"))),
        "unit": normalize_unit(_first_payload(payload, corrected, keys=("normalized_unit", "unit"))),
    }
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"dft:{digest}"


def _row_signature(row: Any) -> str:
    if isinstance(row, dict):
        payload = dict(row.get("evidence_payload") or {})
        payload.update({key: row.get(key) for key in ("paper_id", "adsorbate", "property_type", "value", "unit", "reaction_step")})
        return str(payload.get("dedupe_signature") or build_dft_dedupe_signature(payload))
    evidence_payload = getattr(row, "evidence_payload", None)
    payload = dict(evidence_payload) if isinstance(evidence_payload, dict) else {}
    payload.update(
        {
            "paper_id": getattr(row, "paper_id", None),
            "adsorbate": getattr(row, "adsorbate", None),
            "property_type": getattr(row, "property_type", None),
            "value": getattr(row, "value", None),
            "unit": getattr(row, "unit", None),
            "reaction_step": getattr(row, "reaction_step", None),
        }
    )
    return str(payload.get("dedupe_signature") or build_dft_dedupe_signature(payload))


def summarize_rescan_progress(
    previous_rows: list[Any],
    imported_candidates: list[Any],
    numeric_signals: list[dict[str, Any]] | dict[str, Any] | None,
    *,
    rescan_round: int = 1,
) -> dict[str, Any]:
    previous_signatures = {_row_signature(row) for row in previous_rows or []}
    imported_signatures = [_row_signature(row) for row in imported_candidates or []]
    imported_unique = set(imported_signatures)
    new_signatures = imported_unique - previous_signatures

    signal_items: list[dict[str, Any]]
    if isinstance(numeric_signals, dict):
        signal_items = list(numeric_signals.get("signals") or numeric_signals.get("examples") or [])
        signal_count = int(numeric_signals.get("numeric_value_count") or len(signal_items) or 0)
    else:
        signal_items = list(numeric_signals or [])
        signal_count = len(signal_items)
    excluded_count = sum(
        1
        for item in signal_items
        if item.get("excluded")
        or str(item.get("reason") or item.get("category") or "").strip().lower() in EXCLUDED_NUMERIC_SIGNAL_REASONS
    )
    previous_unique_count = len(previous_signatures)
    after_unique_count = len(previous_signatures | imported_unique)
    new_unique_count = len(new_signatures)
    duplicate_count = max(0, len(imported_signatures) - new_unique_count)
    denominator = max(signal_count, 1)
    return {
        "schema_version": "dft_rescan_progress_v1",
        "rescan_round": int(rescan_round or 1),
        "previous_unique_count": previous_unique_count,
        "new_unique_count": new_unique_count,
        "duplicate_count": duplicate_count,
        "excluded_numeric_signal_count": excluded_count,
        "coverage_ratio_before": round(previous_unique_count / denominator, 4),
        "coverage_ratio_after": round(after_unique_count / denominator, 4),
        "stop_reason": None,
    }


def should_stop_rescan(summary: dict[str, Any]) -> tuple[bool, str | None]:
    rescan_round = int(summary.get("rescan_round") or 0)
    new_unique_count = int(summary.get("new_unique_count") or 0)
    previous_unique_count = int(summary.get("previous_unique_count") or 0)
    duplicate_count = int(summary.get("duplicate_count") or 0)
    excluded_count = int(summary.get("excluded_numeric_signal_count") or 0)
    before = float(summary.get("coverage_ratio_before") or 0.0)
    after = float(summary.get("coverage_ratio_after") or before)
    if rescan_round >= 3:
        return True, "max_rounds_reached"
    if new_unique_count < 3:
        return True, "low_new_unique_count"
    denominator = max(previous_unique_count, 1)
    if new_unique_count / denominator < 0.05:
        return True, "low_new_unique_ratio"
    if (after - before) < 0.05:
        return True, "low_coverage_gain"
    if duplicate_count + excluded_count > max(new_unique_count * 3, 10):
        return True, "remaining_signals_not_reliable_text_values"
    return False, None


def finalize_rescan_summary(summary: dict[str, Any]) -> dict[str, Any]:
    stop, reason = should_stop_rescan(summary)
    return {
        **summary,
        "stop_reason": reason,
        "next_status": "Needs_Human_Check" if stop else "Needs_IDE_Rescan",
    }
