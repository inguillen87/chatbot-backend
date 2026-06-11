from services import embedding_service


def test_embedding_service_uses_huggingface_when_openai_missing(monkeypatch):
    monkeypatch.setattr(embedding_service, "openai_client", None)
    monkeypatch.setenv("HUGGINGFACE_EMBEDDINGS_ENABLED", "true")
    monkeypatch.setattr(
        "services.huggingface_inference_service.embed_texts",
        lambda textos, expected_dimension=None: [[0.1] * expected_dimension],
    )

    result = embedding_service.embed_textos_llm(["hola"])

    assert len(result[0]) == 1024
