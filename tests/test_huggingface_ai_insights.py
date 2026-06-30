from services import huggingface_ai_insights as insights


def test_collection_ai_insights_uses_local_fallback_without_hf(monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_AI_RISK_MIN_SCORE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_AI_INTENT_MIN_SCORE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_AI_SENTIMENT_MIN_SCORE", raising=False)
    monkeypatch.setattr(insights, "classify_zero_shot", lambda *args, **kwargs: None)
    monkeypatch.setattr(insights, "huggingface_configured", lambda: False)
    monkeypatch.setattr(insights, "zero_shot_enabled", lambda: False)

    payload = insights.build_collection_ai_insights(
        [
            {
                "text": "hay un reclamo urgente por luminaria rota y quiero hablar con un agente",
                "source": "ticket",
                "category": "luminaria",
                "channel": "whatsapp",
                "status": "nuevo",
            }
        ],
        domain="operations",
    )

    assert payload["contract_version"] == "huggingface.ai_insights.v1"
    assert payload["mode"] == "deterministic_local_fallback"
    assert payload["hf_status"]["configured"] is False
    assert payload["advisory_policy"]["mutates_operational_state"] is False
    assert payload["thresholds"]["risk_min_score"] == 0.56
    assert payload["summary"]["dominant_intent"] == "public_service_claim"
    assert payload["summary"]["requires_human_attention"] is True
    assert payload["summary"]["mutates_operational_state"] is False
    assert payload["collection"]["channels"][0]["key"] == "whatsapp"
    assert payload["frontend_contract"]["safe_to_render_without_hf_token"] is True


def test_map_ai_layers_builds_visual_layers_from_points(monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_MAP_RISK_MIN_WEIGHT", raising=False)
    payload = insights.build_map_ai_layers(
        [
            {
                "id": "ticket:1",
                "source": "ticket",
                "lat": -33.0,
                "lng": -68.0,
                "weight": 1.8,
                "category": "reclamo",
                "channel": "whatsapp",
                "status": "overdue",
            },
            {
                "id": "survey:1",
                "source": "survey",
                "lat": -33.1,
                "lng": -68.1,
                "w": 1,
                "category": "votacion",
                "channel": "web",
                "status": "submitted",
            },
        ]
    )

    assert payload["contract_version"] == "huggingface.map_ai_layers.v1"
    assert payload["advisory_policy"]["state_mutation_allowed"] is False
    assert payload["thresholds"]["map_risk_min_weight"] == 1.4
    assert payload["preferred_visualization"] == "interactive_globe_heatmap"
    assert payload["layers"]["risk_pulses"]["count"] == 1
    assert payload["layers"]["whatsapp_activity"]["count"] == 1
    assert payload["layers"]["survey_participation"]["count"] == 1
    assert "deckgl" in payload["frontend_contract"]["map_engines"]


def test_whatsapp_ai_runtime_contract_never_exposes_secrets(monkeypatch):
    monkeypatch.setattr(insights, "huggingface_configured", lambda: True)
    monkeypatch.setattr(insights, "zero_shot_enabled", lambda: True)

    payload = insights.build_whatsapp_ai_runtime_contract()

    assert payload["contract_version"] == "huggingface.whatsapp_ai_runtime.v1"
    assert payload["configured"] is True
    assert payload["runtime_policy"]["must_not_expose_tokens"] is True
    assert payload["runtime_policy"]["mutates_operational_state"] is False
    assert "reclamo de servicio publico" in payload["classification_groups"]["intent"]
