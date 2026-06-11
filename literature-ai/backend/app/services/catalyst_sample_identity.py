from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalystSample


SAMPLE_CREATE_FIELDS = {
    "name",
    "catalyst_type",
    "metal_centers",
    "coordination",
    "support",
    "synthesis_method",
    "evidence_strength",
}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _metals(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({_norm(item) for item in value if _norm(item)}))


def clean_sample_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Catalyst sample creation requires an object proposed_value.")
    cleaned = {key: payload.get(key) for key in SAMPLE_CREATE_FIELDS if key in payload}
    cleaned["metal_centers"] = list(payload.get("metal_centers") or [])
    if not any(
        (
            str(cleaned.get("name") or "").strip(),
            cleaned["metal_centers"],
            str(cleaned.get("coordination") or "").strip(),
            str(cleaned.get("support") or "").strip(),
            str(payload.get("structure_name") or "").strip(),
        )
    ):
        raise ValueError("Catalyst sample creation requires a material identity.")
    return cleaned


@dataclass(frozen=True)
class SampleIdentityResolution:
    status: str
    sample: CatalystSample | None = None
    candidate_ids: tuple[str, ...] = ()


def resolve_sample_identity(
    session: Session,
    *,
    paper_id: Any,
    proposed_value: dict[str, Any],
) -> SampleIdentityResolution:
    samples = list(
        session.scalars(
            select(CatalystSample).where(CatalystSample.paper_id == paper_id)
        ).all()
    )
    proposed_name = _norm(proposed_value.get("name"))
    proposed_structure = _norm(proposed_value.get("structure_name"))
    proposed_metals = _metals(proposed_value.get("metal_centers"))
    proposed_coordination = _norm(proposed_value.get("coordination"))
    proposed_support = _norm(proposed_value.get("support"))

    matches: list[CatalystSample] = []
    conflicts: list[CatalystSample] = []
    for sample in samples:
        sample_name = _norm(sample.name)
        sample_metals = _metals(sample.metal_centers)
        sample_coordination = _norm(sample.coordination)
        sample_support = _norm(sample.support)
        strong_match = bool(
            (proposed_name and proposed_name == sample_name)
            or (proposed_structure and proposed_structure in {sample_name, sample_coordination})
            or (
                proposed_metals
                and proposed_metals == sample_metals
                and (
                    (proposed_coordination and proposed_coordination == sample_coordination)
                    or (proposed_support and proposed_support == sample_support)
                )
            )
        )
        if not strong_match:
            continue
        explicit_conflict = bool(
            (proposed_metals and sample_metals and proposed_metals != sample_metals)
            or (proposed_coordination and sample_coordination and proposed_coordination != sample_coordination)
            or (proposed_support and sample_support and proposed_support != sample_support)
        )
        (conflicts if explicit_conflict else matches).append(sample)

    candidates = matches + conflicts
    if len(matches) == 1 and not conflicts:
        return SampleIdentityResolution(status="reuse", sample=matches[0], candidate_ids=(str(matches[0].id),))
    if candidates:
        return SampleIdentityResolution(
            status="ambiguous",
            candidate_ids=tuple(str(item.id) for item in candidates),
        )
    return SampleIdentityResolution(status="create")
