from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class EmbeddingService(Protocol):
    """Protocol that all embedding services must satisfy."""

    dimension: int

    def embed_text(self, text: str) -> list[float]: ...
    def cosine_similarity(self, left: list[float] | None, right: list[float] | None) -> float: ...


class DeterministicEmbeddingService:
    """Offline-safe hashed bag-of-words embeddings for MVP retrieval plumbing."""

    dimension: int

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def embed_text(self, text: str) -> list[float]:
        tokens = self._tokenize(text)
        vector = [0.0] * self.dimension
        if not tokens:
            return vector

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big", signed=False) % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.0 + ((digest[5] / 255.0) * 0.25)
            vector[index] += sign * weight

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [round(value / norm, 8) for value in vector]

    def cosine_similarity(self, left: list[float] | None, right: list[float] | None) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        return round(sum(a * b for a, b in zip(left, right)), 6)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        raw = re.findall(r"[A-Za-z0-9_+\-]+", (text or "").lower())
        return [token for token in raw if len(token) > 1]


class OpenAICompatibleEmbeddingService:
    """Embedding service that calls OpenAI-compatible APIs (DeepSeek, OpenAI, etc.).

    Falls back to DeterministicEmbeddingService when API is unavailable.
    """

    dimension: int

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds
        self._fallback = DeterministicEmbeddingService(dimension=64)

    def embed_text(self, text: str) -> list[float]:
        """Call the embedding API. Falls back to deterministic on failure."""
        if not self.api_base or not self.api_key:
            return self._fallback.embed_text(text)
        try:
            import httpx

            url = self._build_embeddings_url()
            payload = {
                "model": self.model,
                "input": text,
            }
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            embedding = self._extract_embedding(data)
            if embedding and len(embedding) == self.dimension:
                return embedding
            logger.warning(
                "Embedding dimension mismatch: expected %d, got %d; falling back",
                self.dimension,
                len(embedding) if embedding else 0,
            )
            return self._fallback.embed_text(text)
        except Exception as exc:
            logger.warning("Embedding API call failed (%s); falling back to deterministic", exc)
            return self._fallback.embed_text(text)

    def cosine_similarity(self, left: list[float] | None, right: list[float] | None) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        return round(sum(a * b for a, b in zip(left, right)), 6)

    def _build_embeddings_url(self) -> str:
        if self.api_base.endswith("/embeddings"):
            return self.api_base
        if self.api_base.endswith("/v1"):
            return self.api_base + "/embeddings"
        return self.api_base + "/v1/embeddings"

    @staticmethod
    def _extract_embedding(data: dict[str, Any]) -> list[float] | None:
        """Extract the first embedding vector from an OpenAI-compatible response."""
        data_obj = data.get("data")
        if not isinstance(data_obj, list) or not data_obj:
            return None
        embedding = data_obj[0].get("embedding")
        if isinstance(embedding, list):
            return embedding
        return None


def get_embedding_service(
    provider: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    dimension: int = 64,
) -> EmbeddingService:
    """Factory: return the appropriate embedding service based on configuration.

    - provider="deterministic" or provider is None → DeterministicEmbeddingService
    - provider="openai_compatible" → OpenAICompatibleEmbeddingService
    """
    provider = (provider or "deterministic").lower()
    if provider == "openai_compatible":
        if api_base and api_key:
            return OpenAICompatibleEmbeddingService(
                api_base=api_base,
                api_key=api_key,
                model=model or "text-embedding-3-small",
                dimension=dimension,
            )
        logger.warning("openai_compatible embedding requested but api_base/api_key missing; falling back")
    return DeterministicEmbeddingService(dimension=dimension)
