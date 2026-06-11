from services import municipio_ai_classifier as classifier


def test_infer_reclamo_category_uses_high_confidence_hf_result(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.70")
    monkeypatch.setattr(
        "services.huggingface_inference_service.classify_zero_shot",
        lambda text, labels, multi_label=False: [
            {"label": "Luminaria", "score": 0.91},
            {"label": "Limpieza", "score": 0.04},
        ],
    )

    result = classifier.infer_reclamo_category("la calle queda oscura de noche", ["Luminaria", "Limpieza"])

    assert result["categoria"] == "Luminaria"
    assert result["score"] == 0.91
    assert result["provider"] == "huggingface_zero_shot"
    assert result["candidates"][0] == {"label": "Luminaria", "score": 0.91}


def test_infer_reclamo_category_rejects_low_confidence(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.80")
    monkeypatch.setattr(
        "services.huggingface_inference_service.classify_zero_shot",
        lambda text, labels, multi_label=False: [{"label": "Luminaria", "score": 0.51}],
    )

    assert classifier.infer_reclamo_category("problema raro", ["Luminaria"]) is None


def test_infer_reclamo_priority_is_advisory(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE", "0.60")
    monkeypatch.setattr(
        "services.huggingface_inference_service.classify_zero_shot",
        lambda text, labels, multi_label=False: [
            {"label": "urgente", "score": 0.82},
            {"label": "normal", "score": 0.03},
        ],
    )

    result = classifier.infer_reclamo_priority("hay un cable peligroso en la calle")

    assert result["prioridad"] == "urgente"
    assert result["score"] == 0.82
