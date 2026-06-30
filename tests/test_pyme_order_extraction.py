from services.pymes import (
    CONTEXTO_PYME,
    _build_pyme_order_success_payload,
    _normalize_order_tracking_base_url,
    extraer_productos_pedido,
)
from services.pyme_multimodal import _extract_quantity_from_text
from services.ticket_utils import formatear_ticket_respuesta


def test_extraer_productos_pedido_ignora_contacto_y_direccion():
    items = extraer_productos_pedido(
        "Hola, quiero comprar 2 botellas de Malbec y 1 Cabernet. "
        "Soy QA Bodega, telefono 2615550201. Enviar a Godoy Cruz 456."
    )

    assert items == [
        {"nombre": "Malbec", "cantidad": 2, "unidad": "botellas"},
        {"nombre": "Cabernet", "cantidad": 1},
    ]


def test_quantity_fallback_no_toma_numero_de_direccion_como_cantidad():
    assert _extract_quantity_from_text("Enviar a Godoy Cruz 456") == 1
    assert _extract_quantity_from_text("2 botellas de Malbec") == 2


def test_tracking_pedido_usa_base_publica_sin_duplicar_path():
    assert _normalize_order_tracking_base_url("https://chatboc.ar/pyme/pedidos") == "https://chatboc.ar"

    _, buttons = formatear_ticket_respuesta(
        "pedido",
        "QA Bodega",
        "Pedido registrado",
        "Bodega",
        "PED-20260514-ABC",
        base_chat_url="https://chatboc.ar/pyme/pedidos",
    )

    assert any(
        button.get("url") == "https://chatboc.ar/tracking/order/PED-20260514-ABC"
        for button in buttons
    )


def test_payload_pedido_prioriza_profile_name_sobre_vecino_placeholder():
    payload = _build_pyme_order_success_payload(
        {
            CONTEXTO_PYME: {"nombre_cliente": "Vecino/a"},
            "profile_name": "QA Bodega",
            "chat_db_context_data": {
                "profile_name": "QA Bodega",
                "contact_cache": {"nombre": "QA Bodega"},
            },
        },
        {
            "data": {
                "nro_pedido": "PED-1",
                "pedido_id": 10,
                "monto_total": 1000,
                "cart_summary": {"items_detalle": []},
                "cliente": {"nombre": "Vecino/a"},
            },
            "message_body": "Pedido registrado",
        },
    )

    assert "Pedido recibido, QA Bodega" in payload["message_body"]


def test_payload_pedido_expone_whatsapp_order_catalog_contract():
    payload = _build_pyme_order_success_payload(
        {
            CONTEXTO_PYME: {"nombre_cliente": "Cliente"},
            "profile_name": "Cliente Demo",
            "chat_db_context_data": {"profile_name": "Cliente Demo"},
        },
        {
            "data": {
                "nro_pedido": "PED-20260630-ABC",
                "pedido_id": 10,
                "monto_total": 25000,
                "cart_summary": {"items_detalle": []},
                "cliente": {"nombre": "Cliente Demo"},
            },
            "message_body": "Pedido registrado",
        },
    )

    assert payload["whatsapp_flow"] == "order_catalog"
    assert payload["cta_label"] == "Abrir catálogo"
    expected_url = "https://www.chatboc.ar/tracking/order/PED-20260630-ABC"
    assert payload["order_url"] == expected_url
    assert payload["tracking_url"] == expected_url
    assert payload["webview_url"] == expected_url
    assert payload["data"]["tracking_url"] == expected_url
