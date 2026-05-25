from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import sys
from pathlib import Path
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings
from app.db.session import session_scope


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


@dataclass
class DoiRepairCandidate:
    id: object
    title: str | None
    doi: str | None


def extract_dois(value: str | None) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for match in DOI_RE.finditer(value):
        doi = match.group(0).rstrip(".,;:)").lower()
        if doi not in seen:
            seen.add(doi)
            result.append(doi)
    return result


def find_polluted_doi_papers(session: Session) -> list[tuple[DoiRepairCandidate, list[str]]]:
    # Select only the columns this repair needs so old schemas with missing newer columns still dry-run safely.
    papers = session.execute(text("SELECT id, title, doi FROM papers WHERE doi IS NOT NULL")).mappings().all()
    flagged: list[tuple[DoiRepairCandidate, list[str]]] = []
    for row in papers:
        paper = DoiRepairCandidate(id=row["id"], title=row["title"], doi=row["doi"])
        dois = extract_dois(paper.doi)
        if len(dois) != 1 or (paper.doi or "").strip().lower() != dois[0]:
            flagged.append((paper, dois))
    return flagged


def proposed_doi(dois: Sequence[str], mode: str) -> str | None:
    return dois[0] if mode == "first" and dois else None


def repair_polluted_dois(
    session: Session,
    *,
    apply: bool = False,
    mode: str = "first",
) -> list[tuple[DoiRepairCandidate, list[str]]]:
    flagged = find_polluted_doi_papers(session)
    if apply:
        for paper, dois in flagged:
            session.execute(
                text("UPDATE papers SET doi = :doi WHERE id = :paper_id"),
                {"doi": proposed_doi(dois, mode), "paper_id": paper.id},
            )
    return flagged


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run first repair for Paper.doi values polluted by multiple DOI strings. "
            "Run from literature-ai with: python backend/scripts/repair_polluted_doi_metadata.py --dry-run"
        )
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="List abnormal records without writing changes. This is the default.")
    action.add_argument("--apply", action="store_true", help="Apply changes. Requires an explicit repair mode.")
    parser.add_argument("--mode", choices=["first", "clear"], default="first", help="Repair mode when --apply is used.")
    args = parser.parse_args()

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        flagged = find_polluted_doi_papers(session)

        print(f"DOI dry-run: found {len(flagged)} abnormal records.")
        for paper, dois in flagged:
            proposed = proposed_doi(dois, args.mode)
            print(
                "- "
                f"paper_id={paper.id} | "
                f"title={paper.title or '-'} | "
                f"old_doi={paper.doi!r} | "
                f"parsed={dois} | "
                f"proposed_doi={proposed!r}"
            )

        if not args.apply:
            session.rollback()
            print("No changes written. Re-run with --apply --mode first or --apply --mode clear to repair.")
            return 0

        repair_polluted_dois(session, apply=True, mode=args.mode)
        print(f"Applied DOI repair to {len(flagged)} records with mode={args.mode}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
