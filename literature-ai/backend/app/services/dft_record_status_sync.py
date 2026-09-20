"""Record-level DFT candidate status re-computation helpers.

Every write path that touches DFT field reviews -- human promotion, bulk
mark-verified, review saves, imports and AI batches -- must finish by re-deriving the
parent record's ``candidate_status`` from those field reviews.  Historically each path
updated its own fields and left the record status stale, which is exactly how a fully
reviewed record could keep displaying as pending until some unrelated restart rewrote
it.

The derivation itself lives in ``AIVerificationService._sync_dft_record_closure`` and
is only wrapped here, so the two can never diverge.  A human-final decision outranks
automatic re-computation; that guard lives in
``AIVerificationService.sync_dft_record_candidate_status``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import DFTResult

logger = logging.getLogger(__name__)

_DFT_TARGET_TYPES = frozenset({"dft_results", "dft_result"})


def sync_dft_record_statuses(
    session: Session,
    *,
    paper_id: UUID,
    target_ids: Iterable[Any],
    target_type: str,
) -> dict[str, str]:
    """Re-derive ``candidate_status`` for each DFT target.

    Returns a mapping of ``target_id -> "before -> after"`` for the targets whose
    status actually changed, so callers can audit or log the transitions instead of
    relying on a silent side effect.  Non-DFT targets are ignored.
    """
    if str(target_type or "").strip().lower() not in _DFT_TARGET_TYPES:
        return {}

    unique_ids: list[str] = []
    for item in target_ids:
        token = str(item or "").strip()
        if token and token not in unique_ids:
            unique_ids.append(token)
    if not unique_ids:
        return {}

    # Imported lazily: AIVerificationService pulls in a large dependency graph and a
    # module-level import here would make this helper a circular-import trap.
    from app.services.ai_verification_service import AIVerificationService

    service = AIVerificationService(session)
    transitions: dict[str, str] = {}
    for token in unique_ids:
        try:
            row_id = UUID(token)
        except (TypeError, ValueError):
            continue
        row = session.get(DFTResult, row_id)
        if row is None:
            continue
        before = str(row.candidate_status or "")
        after = service.sync_dft_record_candidate_status(paper_id, row)
        if before != after:
            transitions[token] = f"{before} -> {after}"

    if transitions:
        logger.info(
            "DFT record candidate_status re-derived after a field-review write on paper %s: %s",
            paper_id,
            transitions,
        )
    return transitions
