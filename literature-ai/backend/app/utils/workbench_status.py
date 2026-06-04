from __future__ import annotations

from typing import Any


WORKBENCH_SCHEMA_VERSION = "codex_workbench_v1"
EXTRACTION_PROTOCOL_VERSION = "codex_dft_candidate_v1"

PAPER_WORKFLOW_STATUSES = (
    "Imported",
    "Quality_Checked",
    "Parsed_Material_Ready",
    "Codex_Candidate",
    "Gemini_Verified",
    "Gemini_Revised",
    "Gemini_Flagged",
    "Evidence_Insufficient",
    "Needs_Human_Confirmation",
    "Human_Confirmed",
    "Rejected",
    "ML_Ready",
    "Citation_Ready",
)

PDF_QUALITY_STATUSES = (
    "A_text_readable",
    "B_text_partial",
    "C_scan_clear",
    "D_scan_unclear",
    "Broken",
)

FIGURE_CROP_STATUSES = (
    "candidate_crop",
    "verified_crop",
    "needs_recrop",
    "caption_only",
    "rejected",
)

GEMINI_AUDIT_DECISIONS = ("PASS", "REVISE", "FLAG", "INSUFFICIENT")


def normalize_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    if text in allowed:
        return text
    lowered = text.lower()
    for item in allowed:
        if item.lower() == lowered:
            return item
    return default


def workflow_status_after_parsing(*, has_candidates: bool) -> str:
    return "Codex_Candidate" if has_candidates else "Parsed_Material_Ready"


def workflow_status_after_gemini(decision: str) -> str:
    normalized = normalize_choice(decision, GEMINI_AUDIT_DECISIONS, "INSUFFICIENT")
    return {
        "PASS": "Gemini_Verified",
        "REVISE": "Gemini_Revised",
        "FLAG": "Gemini_Flagged",
        "INSUFFICIENT": "Evidence_Insufficient",
    }[normalized]
