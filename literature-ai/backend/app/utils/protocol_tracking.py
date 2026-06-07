from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]

# NOTE: This project uses PostgreSQL (with pgvector extension) as its database,
# NOT SQLite. All session_scope() calls operate against PostgreSQL.
# Do not assume SQLite-style locking or file-based database behavior.

PROTOCOL_FILES: dict[str, str] = {
    "dft_results": "prompts/dft_results.yaml",
    "dft_ai_protocol": "prompts/dft_ai_protocol.yaml",
    "gemini_audit_protocol": "prompts/gemini_audit_protocol.yaml",
    "dft_settings": "prompts/dft_settings.yaml",
    "mechanism_claims": "prompts/mechanism_claims.yaml",
    "paper_writer": "prompts/paper_writer.yaml",
    "writing_card": "prompts/writing_card.yaml",
}


def protocol_snapshot(key: str, *, fallback_version: str | None = None) -> dict[str, Any]:
    rel_path = PROTOCOL_FILES.get(key, key)
    path = REPO_ROOT / rel_path
    if not path.exists():
        return {
            "key": key,
            "path": rel_path,
            "version": fallback_version,
            "sha256": None,
            "available": False,
        }
    raw = path.read_text(encoding="utf-8", errors="replace")
    version = _yaml_scalar(raw, "version") or fallback_version
    return {
        "key": key,
        "path": rel_path,
        "version": version,
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "available": True,
    }


def ai_review_payload(
    *,
    agent_role: str,
    reviewer: str | None,
    model_name: str | None,
    decision: str,
    protocol_key: str = "gemini_audit_protocol",
    confidence: float | None = None,
    note: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "agent_role": agent_role,
        "reviewer": reviewer,
        "model_name": model_name,
        "decision": decision,
        "confidence": confidence,
        "reviewer_note": note,
        "protocol": protocol_snapshot(protocol_key),
        "writes_final_truth": False,
        "requires_human_confirmation": True,
    }
    if extra:
        payload.update(extra)
    return payload


def append_ai_audit(existing_payload: Any, audit: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    payload = dict(existing_payload) if isinstance(existing_payload, dict) else {}
    audits = list(payload.get("ai_audits") or [])
    conflict = _has_conflict(audits, audit)
    audits.append(audit)
    payload["ai_audits"] = audits
    payload["latest_ai_audit"] = audit
    payload["review_conflict"] = conflict
    payload["conflict_policy"] = "AI disagreement blocks export and requires human confirmation."
    return payload, conflict


def _has_conflict(existing_audits: list[Any], incoming: dict[str, Any]) -> bool:
    incoming_bucket = _decision_bucket(incoming.get("decision"))
    if incoming_bucket == "neutral":
        return False
    for item in existing_audits:
        if not isinstance(item, dict):
            continue
        if item.get("reviewer") == incoming.get("reviewer") and item.get("model_name") == incoming.get("model_name"):
            continue
        if _decision_bucket(item.get("decision")) not in {"neutral", incoming_bucket}:
            return True
    return False


def _decision_bucket(decision: Any) -> str:
    normalized = str(decision or "").strip().upper()
    if normalized in {"PASS", "ACCEPT", "VERIFIED", "OK"}:
        return "positive"
    if normalized in {"REVISE", "FLAG", "INSUFFICIENT", "REJECT", "NEEDS_FIX", "SUSPECTED_DUPLICATE"}:
        return "negative"
    return "neutral"


def _yaml_scalar(raw: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+)$", raw)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")
