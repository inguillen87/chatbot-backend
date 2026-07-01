from services.purchase_order_processor import extraer_datos_orden_de_compra_con_llm


def test_purchase_order_processor_parses_clean_json(monkeypatch):
    monkeypatch.setattr(
        "services.purchase_order_processor.llamar_llm_para_generacion_texto",
        lambda **kwargs: '{"numero_orden":"OC-1","items":[],"total":10}',
    )

    result = extraer_datos_orden_de_compra_con_llm("Orden OC-1 total 10")

    assert result == {"numero_orden": "OC-1", "items": [], "total": 10}


def test_purchase_order_processor_returns_none_for_invalid_json(monkeypatch):
    monkeypatch.setattr(
        "services.purchase_order_processor.llamar_llm_para_generacion_texto",
        lambda **kwargs: "no es json",
    )

    assert extraer_datos_orden_de_compra_con_llm("texto de orden") is None


def test_purchase_order_processor_skips_blank_text(monkeypatch):
    called = {"value": False}

    def fake_llm(**kwargs):
        called["value"] = True
        return "{}"

    monkeypatch.setattr("services.purchase_order_processor.llamar_llm_para_generacion_texto", fake_llm)

    assert extraer_datos_orden_de_compra_con_llm("   ") is None
    assert called["value"] is False
