# -*- coding: utf-8 -*-
"""Recompute ALL existing retrieval vectors with the live bge-m3 semantic backend.

Deterministic (hashed) vectors are permanently retired; this backfills the real
semantic embeddings for paper_chunks / paper_sections / content_evidence_items /
writing_cards so every stored vector matches the active openai_compatible provider.
Run inside the backend container:  docker exec -w /app literature-ai-backend-1 python reembed_all.py
"""
import sys
import logging

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")

from app.config import get_settings
from app.services.embedding import get_embedding_service
from app.db.session import get_engine
from app.db.models import PaperChunk, PaperSection, ContentEvidenceItem, WritingCard
from app.utils.writing_card_content import normalized_evidence_chain
from sqlalchemy.orm import Session

settings = get_settings()
svc = get_embedding_service(
    provider=settings.embedding_provider,
    api_base=settings.embedding_api_base,
    api_key=settings.embedding_api_key,
    model=settings.embedding_model,
    dimension=settings.embedding_dimension,
)
print(f"embedding backend: {type(svc).__name__}  model={settings.embedding_model}  dim={settings.embedding_dimension}")

MAX_LEN = 6000  # safe truncation for the embedding API


def embed(text):
    if not text or not str(text).strip():
        return None
    t = str(text).strip()[:MAX_LEN]
    try:
        v = svc.embed_text(t)
        if len(v) != settings.embedding_dimension:
            print("  !! dim mismatch, skip")
            return None
        return v
    except Exception as e:  # noqa: BLE001 - keep the backfill running
        print(f"  !! FAIL {type(e).__name__}: {str(e)[:100]}")
        return None


def run_loop(label, rows, total, text_of):
    done = ok = fail = 0
    for r in rows:
        v = embed(text_of(r))
        if v is not None:
            r.embedding = v
            if hasattr(r, "embedding_model"):
                r.embedding_model = settings.embedding_model
            if hasattr(r, "embedding_dimension"):
                r.embedding_dimension = settings.embedding_dimension
            ok += 1
        else:
            fail += 1
        done += 1
        if done % 50 == 0 or done == total:
            session.commit()
            print(f"  [{label}] {done}/{total}  ok={ok} fail={fail}")
    session.commit()
    print(f"  [{label}] DONE total={total} ok={ok} fail={fail}")


engine = get_engine(settings.database_url)
session = Session(engine)

# 1) paper_chunks
t = session.query(PaperChunk).count()
print(f"[paper_chunks] total={t}")
run_loop("paper_chunks", session.query(PaperChunk).all(), t, lambda r: r.text)

# 2) paper_sections
t = session.query(PaperSection).count()
print(f"[paper_sections] total={t}")
run_loop("paper_sections", session.query(PaperSection).all(), t, lambda r: r.text)

# 3) content_evidence_items
t = session.query(ContentEvidenceItem).count()
print(f"[content_evidence_items] total={t}")
run_loop("content_evidence_items", session.query(ContentEvidenceItem).all(), t,
         lambda r: f"{r.content or ''}\n{r.evidence_text or ''}")


# 4) writing_cards
def wc_text(r):
    chain_text = "\n".join(
        item.get("text", "") for item in normalized_evidence_chain(r.evidence_chain, limit=20)
    )
    return "\n".join(
        filter(None, [r.paper_type, r.research_gap, r.proposed_solution, r.core_hypothesis, chain_text])
    )


t = session.query(WritingCard).count()
print(f"[writing_cards] total={t}")
run_loop("writing_cards", session.query(WritingCard).all(), t, wc_text)

session.close()
print("ALL_REEMBED_DONE")
