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


def test_openai_compatible_embedding_does_not_fallback_without_credentials():
    service = get_embedding_service(provider="openai_compatible", api_base="", api_key="", dimension=1024)

    with pytest.raises(EmbeddingUnavailableError):
        service.embed_text("Li2S adsorption energy")


def test_openai_compatible_embedding_defaults_match_siliconflow_bge_m3():
    service = get_embedding_service(provider="openai_compatible", api_base="https://api.siliconflow.cn/v1", api_key="sk-test")

    assert isinstance(service, OpenAICompatibleEmbeddingService)
    assert service.model == "BAAI/bge-m3"
    assert service.dimension == 1024


def test_qwen3_embedding_adds_dimensions_payload_for_siliconflow():
    service = OpenAICompatibleEmbeddingService(
        api_base="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        model="Qwen/Qwen3-Embedding-4B",
        dimension=768,
    )

    assert service._resolve_dimensions_payload() == 768


def test_bge_m3_embedding_does_not_send_dimensions_payload():
    service = OpenAICompatibleEmbeddingService(
        api_base="https://api.siliconflow.cn/v1",
        api_key="sk-test",
        model="BAAI/bge-m3",
        dimension=1024,
    )

    assert service._resolve_dimensions_payload() is None
