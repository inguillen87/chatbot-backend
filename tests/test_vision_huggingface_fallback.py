from services import vision_fallback_service


def test_analyze_image_smart_can_use_huggingface_fallback(monkeypatch):
    monkeypatch.setenv("VISION_HUGGINGFACE_ENABLED", "true")
    monkeypatch.setattr(vision_fallback_service, "_call_openai", lambda *args, **kwargs: None)
    monkeypatch.setattr(vision_fallback_service, "_cohere_vision_enabled", lambda: False)
    monkeypatch.setattr(
        vision_fallback_service,
        "_call_huggingface",
        lambda image_bytes: {"labels": ["pozo"], "objects": ["calle"], "text": ""},
    )

    result = vision_fallback_service.analyze_image_smart(b"image")

    assert result["labels"] == [{"description": "pozo"}]
    assert result["objects"] == [{"name": "calle"}]
