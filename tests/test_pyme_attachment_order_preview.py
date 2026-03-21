from unittest.mock import patch

from services.order_attachment_preview import build_order_attachment_preview


@patch('services.order_attachment_preview.extraer_lista_pedido_de_texto_con_llm')
def test_build_order_attachment_preview_returns_structured_preview(mock_extract):
    mock_extract.return_value = [
        {'nombre_producto_ocr': 'Malbec Reserva', 'cantidad_ocr': 2},
        {'nombre_producto_ocr': 'Cabernet', 'cantidad_ocr': 1},
    ]

    preview = build_order_attachment_preview(
        texto_extraido='2 Malbec Reserva\n1 Cabernet',
        pyme_id_context=10,
        telefono='+5492617778888',
        direccion='Mitre 200',
        channel='web',
    )

    assert 'pedido preliminar' in preview['message'].lower()
    assert len(preview['items_detectados']) == 2
    assert preview['confirmation_card']['items_count'] == 2
    assert preview['options_list'][0]['action_id'] == 'finalizar_pedido_pyme'


@patch('services.order_attachment_preview.extraer_lista_pedido_de_texto_con_llm')
def test_build_order_attachment_preview_falls_back_when_items_are_not_detected(mock_extract):
    mock_extract.return_value = []

    preview = build_order_attachment_preview(
        texto_extraido='nota borrosa sin items claros',
        pyme_id_context=10,
        channel='web',
    )

    assert 'todavía no logré separar productos' in preview['message'].lower()
    assert preview['items_detectados'] == []
    assert preview['options_list'][0]['action_id'] == 'pyme_hacer_pedido'
