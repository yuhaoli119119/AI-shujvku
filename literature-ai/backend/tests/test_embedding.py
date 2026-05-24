from app.services.embedding import DeterministicEmbeddingService


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
