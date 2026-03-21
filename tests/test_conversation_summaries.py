from services.conversation_summaries import (
    build_claim_confirmation_payload,
    build_order_confirmation_payload,
)


def test_build_claim_confirmation_payload_compacts_relevant_fields():
    payload = build_claim_confirmation_payload(
        categoria="Luminaria",
        ubicacion="San Martín 123",
        descripcion="poste caído",
        telefono="+5492615551111",
        channel="whatsapp",
    )

    assert payload["channel"] == "whatsapp"
    assert "Categoría: Luminaria" in payload["summary_text"]
    assert "Ubicación: San Martín 123" in payload["summary_text"]
    assert "Contacto: +5492615551111" in payload["summary_text"]
    assert "Confirmo categoría Luminaria" in payload["summary_voice"]


def test_build_order_confirmation_payload_summarizes_items_delivery_and_contact():
    payload = build_order_confirmation_payload(
        cart_summary={
            "items_detalle": [
                {"cantidad": 2, "nombre_producto": "Malbec Reserva"},
                {"cantidad": 1, "nombre_producto": "Cabernet"},
            ],
            "total_final_con_descuento": 25500.0,
        },
        customer={"telefono": "+5492617778888", "direccion": "Mitre 200"},
        delivery_address="Mitre 200",
        channel="voice",
    )

    assert payload["channel"] == "voice"
    assert payload["items_count"] == 2
    assert "2 x Malbec Reserva" in payload["summary_text"]
    assert "Entrega: Mitre 200" in payload["summary_text"]
    assert "Contacto: +5492617778888" in payload["summary_text"]
    assert "Confirmo pedido con 2 items" in payload["summary_voice"]
