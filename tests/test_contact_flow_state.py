from unittest.mock import MagicMock, patch
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO

def test_contact_message_parsed_and_creates_ticket(init_database, owner_user):
    chat_ctx = MagicMock()
    chat_ctx.context_data = {
        CONTEXTO_MUNICIPIO: {
            'estado_conversacion': 'ESPERANDO_DATOS_CONTACTO',
            'datos_parciales_llm_reclamo': {
                'categoria': 'Arbolado',
                'descripcion': 'desc',
                'ubicacion': 'Calle 1',
            },
        }
    }
    viewer = MagicMock()
    with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
        inst = mock_handler.return_value
        inst.execute.return_value = {'success': True, 'message_to_user': 'ok', 'data': {}}
        resp = responder_municipio(
            pregunta_original='Juan Perez, juan@mail.com, +5492611111111, 30123456, Calle 1 Junin',
            owner_user=owner_user,
            rubro_obj=None,
            viewer_user=viewer,
            chat_db_context=chat_ctx,
            anon_id='a',
            channel='whatsapp',
        )
    assert 'ok' in resp['message_body']
    inst.execute.assert_called_once()
    contacto = chat_ctx.context_data[CONTEXTO_MUNICIPIO]['contacto_usuario']
    assert contacto['nombre'].startswith('Juan')
