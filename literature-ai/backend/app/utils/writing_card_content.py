from __future__ import annotations

from typing import Any


CORE_WRITING_FIELDS = ("research_gap", "proposed_solution", "core_hypothesis", "section_strategy")


def normalized_evidence_chain(value: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    """Return the small, source-grounded subset exposed to writing workflows.

    The database keeps the extractor's complete legacy payload.  This helper is
    deliberately a read-time projection so adopting the unified content review
    does not require a new table or a destructive migration.
    """

    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split()).strip()
        source = " ".join(str(item.get("source") or "").split()).strip()
        if not text or not source:
            continue
        page = item.get("page") if isinstance(item.get("page"), int) else None
        key = (text.casefold(), page)
        if key in seen:
            continue
        seen.add(key)
        supports_fields = [
            field
            for field in (item.get("supports_fields") or [])
            if field in CORE_WRITING_FIELDS
        ]
        evidence_type = str(item.get("evidence_type") or "result").strip() or "result"
        normalized.append(
            {
                "evidence_id": f"evidence_chain:{index}",
                "text": text,
                "source": source,
                "page": page,
                "supports_fields": supports_fields,
                "locator_status": str(item.get("locator_status") or "text_only"),
                "evidence_type": evidence_type,
                "writing_uses": _writing_uses(evidence_type, supports_fields),
                "source_target_type": (
                    item.get("source_target_type")
                    or item.get("target_type")
                    or item.get("object_type")
                ),
                "source_target_id": (
                    item.get("source_target_id")
                    or item.get("target_id")
                    or item.get("object_id")
                ),
            }
        )
        if len(normalized) >= max(1, limit):
            break
    return normalized


def evidence_chain_search_text(value: Any, *, limit: int = 8) -> str:
    return " ".join(item["text"] for item in normalized_evidence_chain(value, limit=limit))


def _writing_uses(evidence_type: str, supports_fields: list[str]) -> list[str]:
    uses: list[str] = []
    for field in supports_fields:
        uses.append(
            {
                "research_gap": "introduction",
                "proposed_solution": "introduction",
                "core_hypothesis": "discussion",
                "section_strategy": "writing_context",
            }[field]
        )
    normalized_type = evidence_type.casefold()
    if normalized_type in {"result", "caption", "table"}:
        uses.extend(["results", "discussion"])
    elif normalized_type in {"mechanism", "mechanism_claim"}:
        uses.append("mechanism_analysis")
    if not uses:
        uses.append("writing_context")
    return list(dict.fromkeys(uses))
