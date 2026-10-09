from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import logging
import re
from typing import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper

logger = logging.getLogger(__name__)


PAPER_CODE_RE = re.compile(r"^([A-Z])(\d+)$")
SUPPLEMENTARY_MAIN_CODE_RE = re.compile(r"^[A-Z](\d+)$")
SUPPLEMENTARY_CODE_RE = re.compile(r"^S(\d+)(?:-(\d+))?$")


def paper_code_prefix(paper_type: str | None) -> str:
    text = str(paper_type or "").strip()
    upper = text.upper()
    lower = text.lower()
    if upper.startswith("A") or "comput" in lower or "dft" in lower:
        return "A"
    if upper.startswith("B") or "mixed" in lower or "hybrid" in lower:
        return "B"
    if upper.startswith("C") or "experim" in lower:
        return "C"
    if upper.startswith("R") or "review" in lower:
        return "R"
    return "U"


def format_paper_code(prefix: str, number: int) -> str:
    width = max(4, len(str(number)))
    return f"{prefix}{number:0{width}d}"


def supplementary_base_code(main_paper_code: str | None = None, serial_number: int | None = None) -> str:
    clean_code = str(main_paper_code or "").strip().upper()
    match = SUPPLEMENTARY_MAIN_CODE_RE.match(clean_code)
    if match:
        return format_paper_code("S", int(match.group(1)))
    if serial_number is not None:
        return format_paper_code("S", int(serial_number))
    raise ValueError("supplementary_code_requires_main_code_or_serial")


def next_supplementary_paper_code(
    session: Session,
    *,
    main_paper_code: str | None = None,
    serial_number: int | None = None,
    exclude_paper_id: UUID | None = None,
) -> str:
    base_code = supplementary_base_code(main_paper_code=main_paper_code, serial_number=serial_number)
    existing_codes = session.execute(
        select(Paper.paper_code, Paper.id).where(Paper.paper_code.ilike(f"{base_code}%"))
    ).all()

    occupied_ordinals: set[int] = set()
    for raw_code, paper_id in existing_codes:
        if exclude_paper_id is not None and paper_id == exclude_paper_id:
            continue
        clean_code = str(raw_code or "").strip().upper()
        match = SUPPLEMENTARY_CODE_RE.match(clean_code)
        if not match:
            continue
        if match.group(1) != base_code[1:]:
            continue
        suffix = match.group(2)
        occupied_ordinals.add(int(suffix) if suffix else 1)

    if 1 not in occupied_ordinals:
        return base_code
    return f"{base_code}-{max(occupied_ordinals) + 1}"


def ensure_paper_codes(session: Session, papers: Iterable[Paper] | None = None) -> dict[str, str]:
    selected = list(papers) if papers is not None else list(session.scalars(select(Paper)).all())
    used_codes: set[str] = set()
    global_max = 0
    for code in session.scalars(select(Paper.paper_code).where(Paper.paper_code.is_not(None))).all():
        clean = str(code or "").strip().upper()
        if not clean:
            continue
        used_codes.add(clean)
        match = PAPER_CODE_RE.match(clean)
        if match:
            global_max = max(global_max, int(match.group(2)))

    assigned: dict[str, str] = {}
    repairable: list[Paper] = []
    missing: list[Paper] = []
    for paper in selected:
        current_code = str(getattr(paper, "paper_code", "") or "").strip().upper()
        if not current_code:
            missing.append(paper)
            continue
        match = PAPER_CODE_RE.match(current_code)
        desired_prefix = paper_code_prefix(getattr(paper, "paper_type", None))
        if match and match.group(1) == "U" and desired_prefix != "U":
            repairable.append(paper)

    for paper in repairable:
        current_code = str(getattr(paper, "paper_code", "") or "").strip().upper()
        match = PAPER_CODE_RE.match(current_code)
        if not match:
            continue
        desired_prefix = paper_code_prefix(getattr(paper, "paper_type", None))
        number = int(match.group(2))
        new_code = format_paper_code(desired_prefix, number)
        if new_code in used_codes and new_code != current_code:
            next_number = global_max + 1
            new_code = format_paper_code(desired_prefix, next_number)
            while new_code in used_codes:
                next_number += 1
                new_code = format_paper_code(desired_prefix, next_number)
            global_max = next_number
        paper.paper_code = new_code
        assigned[str(paper.id)] = new_code
        used_codes.discard(current_code)
        used_codes.add(new_code)
        session.add(paper)

    if not missing and not assigned:
        return {}

    epoch = datetime.min
    for paper in sorted(missing, key=lambda item: (item.created_at or epoch, str(item.id))):
        prefix = paper_code_prefix(getattr(paper, "paper_type", None))
        number = global_max + 1
        code = format_paper_code(prefix, number)
        while code in used_codes:
            number += 1
            code = format_paper_code(prefix, number)
        paper.paper_code = code
        assigned[str(paper.id)] = code
        used_codes.add(code)
        global_max = number
        session.add(paper)

    session.flush()
    return assigned


def backfill_paper_codes_detached(session: Session, papers: Iterable[Paper] | None = None) -> dict[str, str]:
    """Repair missing or legacy paper codes without committing the caller's transaction.

    Read handlers used to call ``ensure_paper_codes`` and then ``session.commit()``
    directly, which made a GET request a database writer -- and committed anything else
    that happened to be pending on that read session.  The repair still happens (the UI
    would otherwise show blank identifiers forever), but it now runs in its own
    committed session so the reading transaction is never committed as a side effect.
    """
    selected = list(papers or [])
    if not selected:
        return {}

    needs_repair = False
    for paper in selected:
        current_code = str(getattr(paper, "paper_code", "") or "").strip().upper()
        if not current_code:
            needs_repair = True
            break
        match = PAPER_CODE_RE.match(current_code)
        if match and match.group(1) == "U":
            if paper_code_prefix(getattr(paper, "paper_type", None)) != "U":
                needs_repair = True
                break
    if not needs_repair:
        return {}

    paper_ids = [paper.id for paper in selected]
    repair_session = Session(bind=session.get_bind(), expire_on_commit=False)
    try:
        rows = repair_session.scalars(select(Paper).where(Paper.id.in_(paper_ids))).all()
        assigned = ensure_paper_codes(repair_session, rows)
        if assigned:
            repair_session.commit()
        else:
            repair_session.rollback()
    finally:
        repair_session.close()

    if assigned:
        logger.info(
            "Repaired %s paper code(s) during a read request in a detached transaction: %s",
            len(assigned),
            assigned,
        )
        # Drop the stale attribute on the caller's objects so the response shows the
        # persisted code, without marking the read session dirty.
        for paper in selected:
            if str(paper.id) in assigned:
                session.expire(paper, ["paper_code"])
    return assigned
