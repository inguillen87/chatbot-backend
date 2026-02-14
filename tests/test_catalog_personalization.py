from routes.catalogo import _formatear_producto
from routes.admin_tenant import _sanitize_personalization_options_for_storage


def test_formatear_producto_exposes_personalization_options():
    product = _formatear_producto(
        {
            "nombre": "Caja de vino personalizada",
            "precio_float": 12000,
            "moneda": "ARS",
            "extra_metadata": {
                "personalization_options": [
                    {
                        "id": "grabado",
                        "label": "Texto grabado",
                        "type": "text",
                        "required": True,
                        "max_length": 30,
                    },
                    {
                        "id": "packaging",
                        "label": "Packaging",
                        "type": "select",
                        "values": [
                            {"value": "Estándar", "price_delta": 0},
                            {"value": "Premium", "price_delta": 500},
                        ],
                    },
                ]
            },
        }
    )

    assert product["personalization_enabled"] is True
    assert len(product["personalization_options"]) == 2
    assert product["personalization_options"][1]["values"][1]["price_delta"] == 500


def test_formatear_producto_without_personalization_options():
    product = _formatear_producto({"nombre": "Producto simple", "precio_float": 1000, "moneda": "ARS"})
    assert product["personalization_enabled"] is False
    assert product["personalization_options"] == []


def test_sanitize_personalization_options_for_storage_rejects_invalid_type():
    try:
        _sanitize_personalization_options_for_storage(
            [{"id": "x", "label": "X", "type": "unsupported"}]
        )
    except ValueError as exc:
        assert "invalid personalization option type" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid type")


def test_sanitize_personalization_options_for_storage_normalizes_payload():
    payload = [
        {
            "id": "gift",
            "label": "Gift",
            "type": "multiselect",
            "required": False,
            "values": ["Tarjeta", {"label": "Moño", "price_delta": "150"}],
            "max_select": 3,
            "help_text": "Elegí extras",
        }
    ]

    data = _sanitize_personalization_options_for_storage(payload)
    assert data[0]["type"] == "multiselect"
    assert data[0]["values"][1]["price_delta"] == 150.0
    assert data[0]["max_select"] == 3
