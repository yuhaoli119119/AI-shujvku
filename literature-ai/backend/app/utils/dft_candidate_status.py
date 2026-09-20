"""Canonical status registry for DFT result candidates.

This module is the single source of truth for four questions that used to be
answered by several divergent inline lists scattered across the backend and the
frontend:

1. Does a ``candidate_status`` token exist at all?             -> ``is_registered``
2. Has the record left the active queue / work list?          -> ``is_terminal``
3. Does it count as display-ready in cheap summaries?         -> ``is_status_ready``
4. May the AI auto-review queue skip it as already settled?   -> ``is_settled_for_review_queue``

Export eligibility is deliberately NOT decided here.  ``review_safety``'s evidence
and review gate (``gate.eligible``) owns that decision; ``is_status_ready`` is a
presentation summary only and must never be used to authorise an export.

Fail-safe policy
----------------
An unregistered token is treated as **active** (visible, still awaiting attention)
and never as terminal.  Silently hiding an unknown record was the original defect,
so this registry deliberately fails open toward visibility rather than silence.
"""

from __future__ import annotations

from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Canonical tokens (all comparisons go through :func:`normalize`, so these are
# stored lower-case even though some writers persist mixed case, e.g. ML_Ready).
# --------------------------------------------------------------------------- #

DFT_STATUS_PENDING = "system_candidate"
DFT_STATUS_UNVERIFIED = "candidate_unverified"
DFT_STATUS_PRIMARY_APPLIED = "ai_primary_applied"
DFT_STATUS_READY_AI = "ai_verified_ml_ready"
DFT_STATUS_READY_EXPORT = "ml_ready"
DFT_STATUS_TERMINAL_UNUSABLE = "ai_terminal_unusable"
DFT_STATUS_NEEDS_EVIDENCE = "human_reviewed_needs_evidence"
DFT_STATUS_NEEDS_HUMAN = "needs_human_confirmation"
DFT_STATUS_FINAL_SUBMITTED = "final_user_submitted"
DFT_STATUS_BLOCKED_FROM_EXPORT = "blocked_from_export"


DFT_REJECTED_STATUSES = frozenset({"rejected", "ai_rejected", "rejected_by_local_ai"})

# Deprecated historical tokens -> canonical replacement.  The startup normaliser is
# allowed to rewrite *these* (and NULL / blank), and nothing else, so a recorded
# decision can never be silently reset by a restart.
DFT_CANDIDATE_LEGACY_ALIASES: dict[str, str] = {
    "codex_candidate": DFT_STATUS_PENDING,
}

# Statuses that mean "still in the active work list".  An unregistered token is
# treated as active as well; this set documents the tokens we know about.
DFT_ACTIVE_STATUSES = frozenset(
    {
        DFT_STATUS_PENDING,
        DFT_STATUS_UNVERIFIED,
        DFT_STATUS_PRIMARY_APPLIED,
        DFT_STATUS_NEEDS_HUMAN,
    }
)

# Settled decisions: the record has left the active work list.  Note that
# ``needs_human_confirmation`` is deliberately absent -- it still requires a human
# decision and must stay visible, it is only excluded from *AI* re-review.
DFT_TERMINAL_STATUSES = frozenset(
    {
        DFT_STATUS_READY_AI,
        DFT_STATUS_READY_EXPORT,
        *DFT_REJECTED_STATUSES,
        DFT_STATUS_TERMINAL_UNUSABLE,
        DFT_STATUS_NEEDS_EVIDENCE,
        DFT_STATUS_FINAL_SUBMITTED,
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
        DFT_STATUS_READY_EXPORT,
        DFT_STATUS_READY_AI,
        DFT_STATUS_FINAL_SUBMITTED,
        "gemini_verified",
        "human_confirmed",
        "citation_ready",
        "verified",
        "human_verified",
    }
)

# Human-final decisions that automatic repair must not overwrite.
# Deliberately not the same as terminal: ``ai_terminal_unusable`` is terminal but
# repair is still free to revisit it.
DFT_REPAIR_LOCKED_STATUSES = frozenset(
    {
        DFT_STATUS_READY_EXPORT,
        DFT_STATUS_FINAL_SUBMITTED,
        "human_verified",
        "verified",
    }
)

# Excluded from the *AI* auto-review queue even though still active for humans.
DFT_AI_QUEUE_EXCLUDED_STATUSES = frozenset({DFT_STATUS_NEEDS_HUMAN})

# Derived at read time (review-queue display overlay), never persisted.
DFT_DERIVED_STATUSES = frozenset({DFT_STATUS_BLOCKED_FROM_EXPORT})

# Settled for the AI queue regardless of the export gate: hard negative or already
# final.  ``ml_ready`` / ``ai_verified_ml_ready`` are intentionally excluded -- they
# only count as settled when the evidence gate agrees, so a record that claims
# readiness while failing its gate is still re-presented for review.
DFT_QUEUE_SETTLED_STATUSES = frozenset(
    (DFT_TERMINAL_STATUSES - {DFT_STATUS_READY_AI, DFT_STATUS_READY_EXPORT})
    | DFT_AI_QUEUE_EXCLUDED_STATUSES
)

DFT_UNKNOWN_STATUS_LABEL = "未识别状态，需人工复核"

# Display labels (zh-Hans).  Unknown tokens fall back to DFT_UNKNOWN_STATUS_LABEL so
# a raw database token is never shown to a user.
DFT_STATUS_LABELS: dict[str, str] = {
    DFT_STATUS_PENDING: "系统候选",
    DFT_STATUS_UNVERIFIED: "未审核候选",
    DFT_STATUS_PRIMARY_APPLIED: "AI 修复结果已应用，待验证",
    DFT_STATUS_READY_AI: "已审核可用于机器学习",
    DFT_STATUS_READY_EXPORT: "已审核可导出",
    DFT_STATUS_TERMINAL_UNUSABLE: "已终止不可用",
    DFT_STATUS_NEEDS_EVIDENCE: "已审核但仍缺证据",
    "rejected": "已拒绝",
    "ai_rejected": "已拒绝",
    "rejected_by_local_ai": "已拒绝",
    DFT_STATUS_NEEDS_HUMAN: "需人工确认（结论冲突）",
    DFT_STATUS_FINAL_SUBMITTED: "已提交定稿",
    DFT_STATUS_BLOCKED_FROM_EXPORT: "当前不可导出",
    "gemini_verified": "AI 复核候选",
    "human_confirmed": "已人工确认",
    "citation_ready": "可引用",
    "verified": "已核验",
    "human_verified": "已人工核验",
}

DFT_KNOWN_STATUSES = frozenset(DFT_STATUS_LABELS)


# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #


def normalize(status: Any) -> str:
    """Return the canonical comparison token for a DFT candidate status."""
    return str(status or "").strip().lower()


def canonical_token(status: Any) -> str | None:
    """Map legacy alias -> canonical token; registered token -> itself; else None."""
    token = normalize(status)
    if not token:
        return None
    if token in DFT_STATUS_LABELS:
        return token
    return DFT_CANDIDATE_LEGACY_ALIASES.get(token)


def is_registered(status: Any) -> bool:
    """Whether the token is part of the registry."""
    return normalize(status) in DFT_STATUS_LABELS


def is_legacy_alias(status: Any) -> bool:
    """Whether the token is a deprecated historical alias."""
    return normalize(status) in DFT_CANDIDATE_LEGACY_ALIASES


def is_terminal(status: Any) -> bool:
    """Whether the status has left the active DFT candidate queue.

    Unknown tokens are not terminal, so an unrecognised record stays visible.
    """
    return normalize(status) in DFT_TERMINAL_STATUSES


def is_active_candidate(status: Any) -> bool:
    """Whether the record still belongs to the active work list."""
    return not is_terminal(status)


def is_status_ready(status: Any) -> bool:
    """Whether the status is ready for review-center summary purposes only."""
    return normalize(status) in DFT_READY_STATUSES


def is_repair_locked(status: Any) -> bool:
    """Whether automatic DFT repair must leave the record alone."""
    return normalize(status) in DFT_REPAIR_LOCKED_STATUSES


def is_ai_review_queue_excluded(status: Any) -> bool:
    """Whether the AI auto-review queue must not pick this record up."""
    return normalize(status) in DFT_AI_QUEUE_EXCLUDED_STATUSES


def is_settled_for_review_queue(status: Any, *, gate_eligible: bool = False) -> bool:
    """Whether the AI review queue may skip this record as already settled.

    ``ml_ready`` / ``ai_verified_ml_ready`` additionally require the evidence and
    review gate to agree, so a record that claims readiness while failing its gate
    is still presented for review.
    """
    token = normalize(status)
    if token in DFT_QUEUE_SETTLED_STATUSES:
        return True
    if token in {DFT_STATUS_READY_AI, DFT_STATUS_READY_EXPORT}:
        return bool(gate_eligible)
    return False


def display_label(status: Any) -> str:
    """Human-readable label; never returns a raw database token."""
    token = normalize(status)
    if token in DFT_STATUS_LABELS:
        return DFT_STATUS_LABELS[token]
    legacy = DFT_CANDIDATE_LEGACY_ALIASES.get(token)
    if legacy is not None:
        return DFT_STATUS_LABELS[legacy]
    return DFT_UNKNOWN_STATUS_LABEL


def unknown_tokens(statuses: Iterable[Any]) -> list[str]:
    """Distinct unregistered, non-empty tokens -- for non-silent diagnostics."""
    seen = {normalize(item) for item in statuses}
    return sorted(token for token in seen if token and token not in DFT_STATUS_LABELS)
