from services.pymes import _normalize_order_tracking_base_url, extraer_productos_pedido
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
