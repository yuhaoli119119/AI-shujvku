"""Canonical, display-oriented status semantics for DFT result candidates.

This module deliberately does not decide whether a result may be exported.
Export eligibility remains the responsibility of ``review_safety``'s evidence
and review gate.  The ready predicate is only for inexpensive review-center
summaries when evaluating that gate would be inappropriate or unavailable.
"""

from __future__ import annotations

from typing import Any


DFT_REJECTED_STATUSES = frozenset({"rejected", "ai_rejected", "rejected_by_local_ai"})

DFT_TERMINAL_STATUSES = frozenset(
    {
        "ml_ready",
        "ai_verified_ml_ready",
        *DFT_REJECTED_STATUSES,
        "human_reviewed_needs_evidence",
        "gemini_verified",
        "human_confirmed",
        "citation_ready",
        "verified",
        "human_verified",
    }
)

# Ready is a presentation summary, never an export authorization.
DFT_READY_STATUSES = frozenset(
    {
        "ml_ready",
        "ai_verified_ml_ready",
        "gemini_verified",
        "human_confirmed",
        "citation_ready",
        "verified",
        "human_verified",
    }
)


def normalize(status: Any) -> str:
    """Return the canonical comparison token for a DFT candidate status."""
    return str(status or "").strip().lower()


def is_terminal(status: Any) -> bool:
    """Whether the status has left the active DFT candidate queue."""
    return normalize(status) in DFT_TERMINAL_STATUSES


def is_status_ready(status: Any) -> bool:
    """Whether the status is ready for review-center summary purposes only."""
    return normalize(status) in DFT_READY_STATUSES
