from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkDraft:
    text: str
    token_count: int
    content_hash: str


TOKEN_PATTERN = re.compile(r"\S+")


def split_text_into_chunks(
    text: str,
    *,
    target_tokens: int = 750,
    overlap_tokens: int = 125,
) -> list[ChunkDraft]:
    """Split text into overlapping approximate-token chunks.

    This deliberately uses a whitespace approximation so ingestion works without
    an optional tokenizer dependency. token_count records that approximation.
    """
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return []
    tokens = list(TOKEN_PATTERN.finditer(normalized))
    if not tokens:
        return []
    if len(tokens) <= target_tokens:
        return [_draft(normalized, len(tokens))]

    step = max(1, target_tokens - overlap_tokens)
    chunks: list[ChunkDraft] = []
    start = 0
    while start < len(tokens):
        end = min(start + target_tokens, len(tokens))
        chunk_text = normalized[tokens[start].start() : tokens[end - 1].end()].strip()
        if chunk_text:
            chunks.append(_draft(chunk_text, end - start))
        if end >= len(tokens):
            break
        start += step
    return chunks


def _draft(text: str, token_count: int) -> ChunkDraft:
    return ChunkDraft(
        text=text,
        token_count=token_count,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
