import unittest
from unittest.mock import patch, MagicMock

from services.openai_maps_service import geocodificar_inversa_llm
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO

class GeoFlowTests(unittest.TestCase):
    @patch('services.openai_maps_service.reverse_geocode')
    @patch('services.openai_maps_service.OpenAI')
    def test_geocodificar_inversa_llm_normalizes(self, mock_openai, mock_reverse):
        mock_reverse.return_value = {'display': 'Raw Addr'}
        client = MagicMock()
        client.responses.create.return_value = MagicMock(output_text='Addr Norm')
        mock_openai.return_value = client
        data = geocodificar_inversa_llm(1.0, 2.0)
        assert data['display'] == 'Addr Norm'

    def test_confirmacion_ubicacion_prompt(self):
        owner_user = MagicMock(); owner_user.id = 1
        chat_context = MagicMock()
        chat_context.context_data = {
            CONTEXTO_MUNICIPIO: {
                'estado_conversacion': 'ESPERANDO_CONFIRMACION_UBICACION',
                'datos_parciales_llm_reclamo': {'ubicacion': 'Calle Falsa 123'}
            }
        }
        from app import app as flask_app
        with flask_app.app_context():
            resp = responder_municipio(
                pregunta_original='',
                owner_user=owner_user,
                rubro_obj=None,
                viewer_user=None,
                chat_db_context=chat_context,
                anon_id='test',
                channel='whatsapp'
            )
        assert '¿Es esta tu dirección?' in resp['message_body']
        assert 'Calle Falsa 123' in resp['message_body']
        ids = [o.get('action_id') for o in resp.get('options_list', [])]
        assert 'confirmar_ubicacion' in ids and 'editar_ubicacion' in ids

    def test_editar_datos_no_crea_ticket(self):
        chat_context = MagicMock()
        chat_context.context_data = {}
        flow_data = {
            'state': 'ESPERANDO_CONFIRMACION',
            'datos_reclamo': {
                'categoria': 'Arbolado',
                'direccion': 'Calle 1',
                'descripcion': 'Desc',
                'nombre': 'Juan',
                'dni': '1',
                'email': 'a@a.com',
                'telefono': '123'
            }
        }
        context = {'chat_db_context_data': {CONTEXTO_MUNICIPIO: {'reclamo_flow_v2': flow_data}}}
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            instance = mock_handler.return_value
            instance.execute.return_value = {'success': True, 'data': {}}
            from services.municipio_responder import ReclamoFlowHandler
            handler = ReclamoFlowHandler(context, chat_context)
            response = handler.handle_confirmacion('', {'action': 'reclamo_confirmar_no'})
        assert '¿Qué querés editar?' in response['message_body']
        instance.execute.assert_not_called()

    def test_confirmacion_numero_confirma(self):
        chat_context = MagicMock()
        chat_context.context_data = {}
        flow_data = {
            'state': 'ESPERANDO_CONFIRMACION',
            'datos_reclamo': {
                'categoria': 'Arbolado',
                'direccion': 'Calle 1',
                'descripcion': 'Desc',
                'nombre': 'Juan',
                'dni': '1',
                'email': 'a@a.com',
                'telefono': '123'
            }
        }
        context = {
            'chat_db_context_data': {CONTEXTO_MUNICIPIO: {'reclamo_flow_v2': flow_data}},
            'municipio_config_actual': {'ciudad': 'Junín, Mendoza'},
        }
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            instance = mock_handler.return_value
            instance.execute.return_value = {'success': True, 'message_to_user': 'ok', 'data': {}}
            from services.municipio_responder import ReclamoFlowHandler
            handler = ReclamoFlowHandler(context, chat_context)
            response = handler.handle_confirmacion('1', {})
        instance.execute.assert_called_once()
        call_args, _ = instance.execute.call_args
        action_payload = call_args[0]
        assert 'Junín' in action_payload.get('ubicacion', '')
        assert action_payload.get('distrito') == 'Junín, Mendoza'
        assert 'ok' in response['message_body'].lower()

    def test_confirmacion_texto_invalido_reitera(self):
        chat_context = MagicMock()
        chat_context.context_data = {}
        flow_data = {
            'state': 'ESPERANDO_CONFIRMACION',
            'datos_reclamo': {
                'categoria': 'Arbolado',
                'direccion': 'Calle 1',
                'descripcion': 'Desc',
                'nombre': 'Juan',
                'dni': '1',
                'email': 'a@a.com',
                'telefono': '123'
            }
        }
        context = {'chat_db_context_data': {CONTEXTO_MUNICIPIO: {'reclamo_flow_v2': flow_data}}}
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            instance = mock_handler.return_value
            instance.execute.return_value = {'success': True, 'message_to_user': 'ok', 'data': {}}
            from services.municipio_responder import ReclamoFlowHandler
            handler = ReclamoFlowHandler(context, chat_context)
            response = handler.handle_confirmacion('tal vez', {})
        instance.execute.assert_not_called()
        assert 'No entendí tu respuesta'.lower() in response['message_body'].lower()

    def test_handle_numeric_confirmation_via_handle(self):
        chat_context = MagicMock()
        chat_context.context_data = {}
        flow_data = {
            'state': 'ESPERANDO_CONFIRMACION',
            'datos_reclamo': {
                'categoria': 'Arbolado',
                'direccion': 'Calle 1',
                'descripcion': 'Desc',
                'nombre': 'Juan',
                'dni': '1',
                'email': 'a@a.com',
                'telefono': '123'
            }
        }
        context = {
            'chat_db_context_data': {CONTEXTO_MUNICIPIO: {'reclamo_flow_v2': flow_data}},
            'municipio_config_actual': {'ciudad': 'Junín'},
        }
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            instance = mock_handler.return_value
            instance.execute.return_value = {'success': True, 'message_to_user': 'ok', 'data': {}}
            from services.municipio_responder import ReclamoFlowHandler
            handler = ReclamoFlowHandler(context, chat_context)
            response = handler.handle('1', {})
        instance.execute.assert_called_once()
        assert 'ok' in response['message_body'].lower()
        assert 'cancelado' not in response['message_body'].lower()

    def test_handle_negative_phrase_with_digit_requests_edit(self):
        chat_context = MagicMock()
        chat_context.context_data = {}
        flow_data = {
            'state': 'ESPERANDO_CONFIRMACION',
            'datos_reclamo': {
                'categoria': 'Arbolado',
                'direccion': 'Calle 1',
                'descripcion': 'Desc',
                'nombre': 'Juan',
                'dni': '1',
                'email': 'a@a.com',
                'telefono': '123'
            }
        }
        context = {'chat_db_context_data': {CONTEXTO_MUNICIPIO: {'reclamo_flow_v2': flow_data}}}
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            instance = mock_handler.return_value
            instance.execute.return_value = {'success': True, 'message_to_user': 'ok', 'data': {}}
            from services.municipio_responder import ReclamoFlowHandler
            handler = ReclamoFlowHandler(context, chat_context)
            handler.ask_for_contact_details = MagicMock(return_value={'message_body': 'editar'})
            response = handler.handle('no, tengo 1 dato mal', {})
        handler.ask_for_contact_details.assert_called_once()
        instance.execute.assert_not_called()
        assert 'editar' in response['message_body']

    def test_handle_numeric_three_cancels(self):
        chat_context = MagicMock()
        chat_context.context_data = {}
        flow_data = {
            'state': 'ESPERANDO_CONFIRMACION',
            'datos_reclamo': {
                'categoria': 'Arbolado',
                'direccion': 'Calle 1',
                'descripcion': 'Desc',
                'nombre': 'Juan',
                'dni': '1',
                'email': 'a@a.com',
                'telefono': '123'
            }
        }
        context = {'chat_db_context_data': {CONTEXTO_MUNICIPIO: {'reclamo_flow_v2': flow_data}}}
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            instance = mock_handler.return_value
            instance.execute.return_value = {'success': True, 'message_to_user': 'ok', 'data': {}}
            from services.municipio_responder import ReclamoFlowHandler
            handler = ReclamoFlowHandler(context, chat_context)
            response = handler.handle('3', {})
        instance.execute.assert_not_called()
        assert 'cancelado' in response['message_body'].lower()

    def test_contacto_compacto(self):
        from services.municipio_responder import procesar_datos_contacto_compacto
        texto = "Juan Perez juan@mail.com 2615551234 30123456 Don Bosco 55 Junin"
        datos = procesar_datos_contacto_compacto(texto, {})
        assert datos['email'] == 'juan@mail.com'
        assert datos['telefono'] == '2615551234'
        assert datos['dni'] == '30123456'
        assert datos['nombre'].startswith('Juan')
        assert 'Don Bosco' in datos['direccion_contacto']

if __name__ == '__main__':
    unittest.main()
