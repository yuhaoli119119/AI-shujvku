from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select

from app.config import get_settings
from app.db.models import PaperSection
from app.db.session import session_scope
from app.schemas.retrieval import RetrievalSearchRequest
from app.services.paperqa2_adapter import PaperQA2Adapter, PaperQA2UnavailableError
from app.services.retrieval_service import RetrievalService


def load_questions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"Question file not found: {path}. Expected JSONL rows like "
            '{"query":"...","relevant_paper_ids":["uuid"],"keywords":["..."]}.'
        )
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def recall_at(items: list[dict[str, Any]], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    found = {str(item.get("paper_id")) for item in items[:k]}
    return 1.0 if found & relevant else 0.0


def run_baseline_lexical(session, query: str, limit: int) -> list[dict[str, Any]]:
    tokens = {token.lower() for token in query.split() if len(token) > 1}
    rows = session.scalars(select(PaperSection).limit(5000)).all()
    scored: list[dict[str, Any]] = []
    for row in rows:
        text = " ".join(filter(None, [row.section_title, row.section_type, row.text])).lower()
        if not text:
            continue
        overlap = sum(1 for token in tokens if token in text)
        if overlap <= 0:
            continue
        scored.append(
            {
                "paper_id": str(row.paper_id),
                "section_id": str(row.id),
                "score": overlap / max(1, len(tokens)),
                "text": (row.text or "")[:500],
            }
        )
    return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]


def run_litai_retrieval(session, query: str, limit: int) -> list[dict[str, Any]]:
    response = RetrievalService(session).search(
        RetrievalSearchRequest(query=query, limit=limit, limit_per_type=min(limit, 20), rerank=True)
    )
    return [
        {
            "paper_id": str(item.paper_id),
            "chunk_id": item.chunk_id,
            "section_id": str(item.section_id) if item.section_id else None,
            "score": item.score,
            "text": item.text[:500],
        }
        for item in response.items
    ]


def run_paperqa2(session, query: str, relevant: set[str], limit_chunks: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        answer = PaperQA2Adapter(session).query(query, limit_chunks=limit_chunks)
    except PaperQA2UnavailableError as exc:
        return {"available": False, "error": str(exc), "elapsed_ms": 0}
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    payload = str(answer)
    matched_relevant = [paper_id for paper_id in relevant if paper_id in payload]
    return {
        "available": True,
        "elapsed_ms": elapsed_ms,
        "matched_relevant_paper_ids": matched_relevant,
        "answer_preview": payload[:1000],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare LitAI retrieval backends on labeled review questions.")
    parser.add_argument("--questions", type=Path, default=Path("scripts/retrieval_benchmark_questions.jsonl"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--paperqa2-limit-chunks", type=int, default=500)
    args = parser.parse_args()

    questions = load_questions(args.questions)
    settings = get_settings()
    results: list[dict[str, Any]] = []
    with session_scope(settings.database_url) as session:
        for row in questions:
            query = str(row["query"])
            relevant = {str(UUID(value)) for value in row.get("relevant_paper_ids", [])}
            started = time.perf_counter()
            baseline = run_baseline_lexical(session, query, args.limit)
            baseline_ms = round((time.perf_counter() - started) * 1000, 2)

            started = time.perf_counter()
            litai = run_litai_retrieval(session, query, args.limit)
            litai_ms = round((time.perf_counter() - started) * 1000, 2)

            paperqa2 = run_paperqa2(session, query, relevant, args.paperqa2_limit_chunks)
            results.append(
                {
                    "query": query,
                    "baseline_lexical": {
                        "recall@5": recall_at(baseline, relevant, 5),
                        "recall@10": recall_at(baseline, relevant, 10),
                        "elapsed_ms": baseline_ms,
                    },
                    "litai_pgvector": {
                        "recall@5": recall_at(litai, relevant, 5),
                        "recall@10": recall_at(litai, relevant, 10),
                        "elapsed_ms": litai_ms,
                    },
                    "paperqa2": paperqa2,
                }
            )

    print(json.dumps({"questions": len(questions), "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
