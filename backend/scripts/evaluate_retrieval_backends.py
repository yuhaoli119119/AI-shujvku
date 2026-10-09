from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import get_settings
from app.db.session import session_scope
from app.services.retrieval_service import RetrievalService
from app.schemas.retrieval import RetrievalSearchRequest
from app.services.paperqa2_adapter import PaperQA2Adapter, PaperQA2UnavailableError


def load_questions(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Question file must be a JSON list")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Literature AI retrieval backends on a JSON question set.")
    parser.add_argument("questions", type=Path, help="JSON list with objects containing at least a 'query' field")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--paperqa2", action="store_true", help="Also build a PaperQA2 docs object when paper-qa is installed")
    args = parser.parse_args()

    settings = get_settings()
    questions = load_questions(args.questions)
    report = []
    with session_scope(settings.database_url) as session:
        retrieval = RetrievalService(session)
        for item in questions:
            query = str(item.get("query") or "").strip()
            if not query:
                continue
            result = retrieval.search(RetrievalSearchRequest(query=query, limit=args.limit))
            report.append(
                {
                    "query": query,
                    "local_total": result.total,
                    "local_top": [
                        {
                            "paper_id": str(row.paper_id) if row.paper_id else None,
                            "score": row.score,
                            "source": row.source,
                            "text": row.text[:240],
                        }
                        for row in result.items[: args.limit]
                    ],
                }
            )
        if args.paperqa2:
            try:
                docs = PaperQA2Adapter(session).build_docs(limit=5000)
                report.append({"paperqa2_docs_built": True, "paperqa2_docs_type": type(docs).__name__})
            except PaperQA2UnavailableError as exc:
                report.append({"paperqa2_docs_built": False, "error": str(exc)})

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
