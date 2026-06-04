import pytest

from app.services.embedding import (
    DeterministicEmbeddingService,
    EmbeddingUnavailableError,
    OpenAICompatibleEmbeddingService,
    get_embedding_service,
)


def test_embedding_is_stable():
    service = DeterministicEmbeddingService(dimension=8)
    first = service.embed_text("Li2S adsorption energy")
    second = service.embed_text("Li2S adsorption energy")

    assert first == second
    assert len(first) == 8


def test_embedding_similarity_prefers_related_text():
    service = DeterministicEmbeddingService(dimension=32)
    query = service.embed_text("Li2S4 adsorption energy on Fe-N4")
    related = service.embed_text("Fe-N4 shows strong Li2S4 adsorption energy")
    unrelated = service.embed_text("pyrolysis synthesis on carbon support")

    assert service.cosine_similarity(query, related) > service.cosine_similarity(query, unrelated)


def test_openai_compatible_embedding_missing_key_fails_strictly():
    service = OpenAICompatibleEmbeddingService(
        api_base="https://api.openai.com/v1",
        api_key="",
        model="text-embedding-3-small",
        dimension=1536,
    )

    with pytest.raises(EmbeddingUnavailableError):
        service.embed_text("Li2S4 adsorption energy")


def test_openai_compatible_factory_rejects_non_1536_dimensions():
    with pytest.raises(ValueError, match="1536"):
        get_embedding_service(
            provider="openai_compatible",
            api_base="https://api.openai.com/v1",
            api_key="sk-test",
            model="text-embedding-3-small",
            dimension=64,
        )
