from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from collections.abc import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Paper, PaperChunk, PaperSection, WritingCard
from app.db.session import session_scope
from app.services.embedding import (
    EmbeddingUnavailableError,
    OpenAICompatibleEmbeddingService,
    _l2_normalize,
    get_embedding_service,
)

EMBEDDING_KEYS = {
    "embedding_provider",
    "embedding_api_base",
    "embedding_api_key",
    "embedding_model",
    "embedding_dimension",
}


def _read_persisted_embedding_settings(session: Session) -> dict[str, str]:
    try:
        rows = session.execute(
            sa.text("select key, value from app_settings where key = any(:keys)"),
            {"keys": list(EMBEDDING_KEYS)},
        ).all()
    except Exception:
        return {}
    return {str(key): str(value) for key, value in rows if value is not None}


def _runtime_settings(session: Session) -> Settings:
    settings = get_settings()
    persisted = _read_persisted_embedding_settings(session)
    update: dict[str, Any] = {}
    for key in ("embedding_provider", "embedding_api_base", "embedding_api_key", "embedding_model"):
        value = persisted.get(key)
        if value:
            update[key] = value
    if persisted.get("embedding_dimension"):
        update["embedding_dimension"] = int(persisted["embedding_dimension"])
    return settings.model_copy(update=update) if update else settings


def _build_openai_payload(service: OpenAICompatibleEmbeddingService, texts: Sequence[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": service.model, "input": list(texts)}
    dimensions = service._resolve_dimensions_payload()
    if dimensions is not None:
        payload["dimensions"] = dimensions
    return payload


def _embed_batch(settings: Settings, texts: Sequence[str], *, allow_deterministic: bool) -> list[list[float]]:
    provider = (settings.embedding_provider or "").strip().lower()
    if provider == "openai_compatible":
        service = OpenAICompatibleEmbeddingService(
            api_base=settings.embedding_api_base or "",
            api_key=settings.embedding_api_key or "",
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
            timeout_seconds=60.0,
        )
        if not service.api_key:
            raise EmbeddingUnavailableError("embedding_api_key is required for real embedding rebuild")
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                service._build_embeddings_url(),
                headers={"Authorization": f"Bearer {service.api_key}", "Content-Type": "application/json"},
                json=_build_openai_payload(service, texts),
            )
        response.raise_for_status()
        data = response.json()
        items = data.get("data") or []
        if len(items) != len(texts):
            raise EmbeddingUnavailableError(f"expected {len(texts)} embeddings, got {len(items)}")
        if all(isinstance(item, dict) and "index" in item for item in items):
            items = sorted(items, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in items:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(embedding, list) or len(embedding) != settings.embedding_dimension:
                raise EmbeddingUnavailableError(
                    f"embedding dimension mismatch: expected {settings.embedding_dimension}, "
                    f"got {len(embedding) if isinstance(embedding, list) else 0}"
                )
            vectors.append(_l2_normalize(embedding))
        return vectors

    if not allow_deterministic:
        raise EmbeddingUnavailableError(f"refusing to rebuild production embeddings with provider={settings.embedding_provider!r}")
    service = get_embedding_service(provider=settings.embedding_provider, dimension=settings.embedding_dimension)
    return [service.embed_text(text) for text in texts]


def _writing_card_text(card: WritingCard) -> str:
    parts = [
        card.paper_type,
        card.research_gap,
        card.proposed_solution,
        card.core_hypothesis,
        card.abstract_logic,
        card.introduction_logic,
        card.discussion_logic,
    ]
    if card.evidence_chain:
        parts.append(json.dumps(card.evidence_chain, ensure_ascii=False))
    if card.section_strategy:
        parts.append(json.dumps(card.section_strategy, ensure_ascii=False))
    if card.figure_logic:
        parts.append(card.figure_logic)
    return "\n".join(str(item) for item in parts if item)


def _select_rows(
    session: Session,
    target: str,
    *,
    library_name: str | None,
    paper_code: str | None,
    only_missing: bool,
    limit: int | None,
) -> list[Any]:
    if target == "chunks":
        stmt = sa.select(PaperChunk).join(Paper, PaperChunk.paper_id == Paper.id).order_by(Paper.paper_code, PaperChunk.chunk_index)
        if only_missing:
            stmt = stmt.where(PaperChunk.embedding.is_(None))
    elif target == "sections":
        stmt = sa.select(PaperSection).join(Paper, PaperSection.paper_id == Paper.id).order_by(Paper.paper_code, PaperSection.section_title)
        if only_missing:
            stmt = stmt.where(PaperSection.embedding.is_(None))
    elif target == "writing_cards":
        stmt = sa.select(WritingCard).join(Paper, WritingCard.paper_id == Paper.id).order_by(Paper.paper_code, WritingCard.id)
        if only_missing:
            stmt = stmt.where(WritingCard.embedding.is_(None))
    else:
        raise ValueError(f"unknown target: {target}")
    if library_name:
        stmt = stmt.where(Paper.library_name == library_name)
    if paper_code:
        stmt = stmt.where(Paper.paper_code == paper_code)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def _row_text(target: str, row: Any) -> str:
    if target in {"chunks", "sections"}:
        return row.text or ""
    if target == "writing_cards":
        return _writing_card_text(row)
    raise ValueError(f"unknown target: {target}")


def _assign_embedding(target: str, row: Any, vector: list[float], settings: Settings) -> None:
    row.embedding = vector
    if target == "chunks":
        row.embedding_model = settings.embedding_model
        row.embedding_dimension = settings.embedding_dimension


def rebuild_target(
    session: Session,
    settings: Settings,
    target: str,
    *,
    batch_size: int,
    limit: int | None,
    library_name: str | None,
    paper_code: str | None,
    only_missing: bool,
    dry_run: bool,
    allow_deterministic: bool,
) -> int:
    rows = _select_rows(session, target, library_name=library_name, paper_code=paper_code, only_missing=only_missing, limit=limit)
    total = len(rows)
    print(f"{target}: selected {total} rows", flush=True)
    updated = 0
    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        usable = [(row, _row_text(target, row).strip()) for row in batch]
        usable = [(row, text) for row, text in usable if text]
        if not usable:
            continue
        vectors = _embed_batch(settings, [text for _row, text in usable], allow_deterministic=allow_deterministic)
        for (row, _text), vector in zip(usable, vectors, strict=True):
            _assign_embedding(target, row, vector, settings)
            updated += 1
        if dry_run:
            session.rollback()
        else:
            session.commit()
        print(f"{target}: {min(start + batch_size, total)}/{total} scanned, {updated} updated", flush=True)
        time.sleep(0.05)
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild Literature AI retrieval embeddings with the configured real provider.")
    parser.add_argument("--target", choices=["chunks", "sections", "writing_cards", "all"], default="all")
    parser.add_argument("--library-name")
    parser.add_argument("--paper-code")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-deterministic", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    targets = ["chunks", "sections", "writing_cards"] if args.target == "all" else [args.target]
    base_settings = get_settings()
    with session_scope(base_settings.database_url) as session:
        settings = _runtime_settings(session)
        print(
            "embedding provider={provider} model={model} dimension={dimension} key_present={key}".format(
                provider=settings.embedding_provider,
                model=settings.embedding_model,
                dimension=settings.embedding_dimension,
                key=bool(settings.embedding_api_key),
            ),
            flush=True,
        )
        totals: dict[str, int] = {}
        for target in targets:
            totals[target] = rebuild_target(
                session,
                settings,
                target,
                batch_size=max(1, args.batch_size),
                limit=args.limit,
                library_name=args.library_name,
                paper_code=args.paper_code,
                only_missing=args.only_missing,
                dry_run=args.dry_run,
                allow_deterministic=args.allow_deterministic,
            )
        print("updated", totals, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
