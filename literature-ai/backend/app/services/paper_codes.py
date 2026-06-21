from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper


PAPER_CODE_RE = re.compile(r"^([A-Z])(\d+)$")


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
