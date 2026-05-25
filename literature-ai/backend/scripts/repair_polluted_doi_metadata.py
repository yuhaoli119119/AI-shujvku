from __future__ import annotations

import argparse
import re

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Paper
from app.db.session import session_scope


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List and optionally repair Paper.doi values polluted by multiple DOI strings."
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes. Without this flag, only dry-run output is printed.")
    parser.add_argument("--mode", choices=["first", "clear"], default="first", help="Repair mode when --apply is used.")
    args = parser.parse_args()

    settings = get_settings()
    with session_scope(settings.database_url) as session:
        papers = session.scalars(select(Paper).where(Paper.doi.is_not(None))).all()
        flagged: list[tuple[Paper, list[str]]] = []
        for paper in papers:
            dois = extract_dois(paper.doi)
            if len(dois) != 1 or (paper.doi or "").strip().lower() != dois[0]:
                flagged.append((paper, dois))

        print(f"DOI dry-run: found {len(flagged)} abnormal records.")
        for paper, dois in flagged:
            proposed = dois[0] if (args.mode == "first" and dois) else None
            print(f"- {paper.id} | {paper.title or '-'} | current={paper.doi!r} | parsed={dois} | proposed={proposed!r}")

        if not args.apply:
            print("No changes written. Re-run with --apply --mode first or --apply --mode clear to repair.")
            return 0

        for paper, dois in flagged:
            paper.doi = dois[0] if (args.mode == "first" and dois) else None
        print(f"Applied DOI repair to {len(flagged)} records with mode={args.mode}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
