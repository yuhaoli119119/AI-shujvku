import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.config import Settings, get_settings
from app.db.models import EMBEDDING_DIMENSION
from app.services.embedding import (
    DeterministicEmbeddingService,
    EmbeddingUnavailableError,
    OpenAICompatibleEmbeddingService,
    get_embedding_service,
)


def test_default_embedding_config_is_database_v1_contract():
    settings = Settings()

    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.embedding_dimension == 1024
    assert EMBEDDING_DIMENSION == 1024


def test_non_1024_embedding_dimension_fails_startup(monkeypatch):
    monkeypatch.setenv("LITAI_EMBEDDING_DIMENSION", "768")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="embedding_dimension=1024"):
        get_settings()

    get_settings.cache_clear()


def test_models_ignore_embedding_dimension_environment_override():
    env = os.environ.copy()
    env["LITAI_EMBEDDING_DIMENSION"] = "768"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.db.models import EMBEDDING_DIMENSION; print(EMBEDDING_DIMENSION)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == "1024"


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


def test_cosine_similarity_handles_unnormalized_vectors():
    service = DeterministicEmbeddingService(dimension=2)

    assert service.cosine_similarity([10.0, 0.0], [2.0, 0.0]) == 1.0
    assert service.cosine_similarity([10.0, 0.0], [0.0, 5.0]) == 0.0


def test_openai_compatible_embedding_normalizes_api_vectors(monkeypatch):
    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [3.0, 4.0]}]}

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return DummyResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", DummyClient)
    service = OpenAICompatibleEmbeddingService(
        api_base="https://example.test/v1",
        api_key="sk-test",
        model="dummy",
        dimension=2,
    )

    assert service.embed_text("test") == [0.6, 0.8]


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
