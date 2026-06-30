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


def test_infer_reclamo_category_sorts_candidates_before_threshold(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.70")
    monkeypatch.setattr(
        "services.huggingface_inference_service.classify_zero_shot",
        lambda text, labels, multi_label=False: [
            {"label": "Limpieza", "score": 0.11},
            {"label": "Luminaria", "score": 0.88},
        ],
    )

    result = classifier.infer_reclamo_category("luz rota en la esquina", ["Luminaria", "Limpieza"])

    assert result["categoria"] == "Luminaria"
    assert result["threshold"] == 0.7
    assert result["meets_threshold"] is True


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


def test_infer_reclamo_operational_signals_marks_risk_and_evidence(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE", "0.60")
    monkeypatch.setattr(
        "services.huggingface_inference_service.classify_zero_shot",
        lambda text, labels, multi_label=False: [
            {"label": "riesgo para personas", "score": 0.86},
            {"label": "requiere foto o evidencia", "score": 0.79},
            {"label": "consulta administrativa", "score": 0.08},
        ],
    )

    result = classifier.infer_reclamo_operational_signals("hay un poste por caer sobre la calle")

    assert result["risk_level"] == "critico"
    assert result["requires_photo"] is True
    assert result["requires_human_attention"] is True
    assert result["signals"][0]["code"] == "riesgo_personas"


def test_build_reclamo_ai_enrichment_combines_hf_hints(monkeypatch):
    def fake_zero_shot(text, labels, multi_label=False):
        if "riesgo para personas" in labels:
            return [
                {"label": "riesgo vial o transito", "score": 0.77},
                {"label": "requiere ubicacion exacta", "score": 0.72},
            ]
        if "frustrado o enojado" in labels:
            return [{"label": "preocupado", "score": 0.71}]
        if "urgente" in labels:
            return [{"label": "alta", "score": 0.7}]
        return [{"label": "Luminaria", "score": 0.92}]

    monkeypatch.setenv("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.60")
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE", "0.60")
    monkeypatch.setenv("HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE", "0.60")
    monkeypatch.setenv("HUGGINGFACE_SENTIMENT_MIN_SCORE", "0.60")
    monkeypatch.setattr("services.huggingface_inference_service.classify_zero_shot", fake_zero_shot)

    result = classifier.build_reclamo_ai_enrichment("semaforo apagado en una esquina peligrosa", ["Luminaria"])

    assert result["contract_version"] == "municipio.reclamo_ai_enrichment.v1"
    assert result["category"]["categoria"] == "Luminaria"
    assert result["priority"]["prioridad"] == "alta"
    assert result["advisory_policy"]["mutates_operational_state"] is False
    assert result["state_mutation"]["applied"] is False
    assert result["thresholds"]["signal_min_score"] == 0.6
    assert result["crm_hints"]["requires_exact_location"] is True
    assert result["crm_hints"]["mutates_operational_state"] is False
    assert "sentiment:preocupacion" in result["crm_hints"]["tags"]
