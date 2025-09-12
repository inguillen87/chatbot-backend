import unittest
import os
from unittest.mock import patch, MagicMock

from services.openai_maps_service import geocodificar_inversa_llm
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO, detect_modalidad

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
                'datos_parciales_llm_reclamo': {
                    'ubicacion': 'Calle Falsa 123',
                    'maps_search_url': 'http://maps.example'
                }
            }
        }
        from flask import Flask
        flask_app = Flask(__name__)
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
        assert '📍 Ubicación detectada' in resp['message_body']
        assert 'Calle Falsa 123' in resp['message_body']
        assert 'http://maps.example' in resp['message_body']
        assert '1) Sí, es acá' in resp['message_body']
        assert resp.get('image_url')
        assert resp.get('image_alt_text')
        ids = [o.get('action_id') for o in resp.get('options_list', [])]
        assert 'confirmar_ubicacion' in ids and 'editar_ubicacion' in ids

    def test_si_no_dispara_fuzzy_en_confirmacion(self):
        owner_user = MagicMock(); owner_user.id = 1
        chat_context = MagicMock()
        chat_context.context_data = {
            CONTEXTO_MUNICIPIO: {
                'estado_conversacion': 'ESPERANDO_CONFIRMACION_UBICACION',
                'datos_parciales_llm_reclamo': {'ubicacion': 'Calle Falsa 123'}
            }
        }
        from flask import Flask
        flask_app = Flask(__name__)
        with flask_app.app_context():
            resp = responder_municipio(
                pregunta_original='Si',
                owner_user=owner_user,
                rubro_obj=None,
                viewer_user=None,
                chat_db_context=chat_context,
                anon_id='test',
                channel='whatsapp'
            )
        assert resp.get('fuente') != 'info_veterinaria_json'

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

    def test_location_first_triggers_confirmation(self):
        owner_user = MagicMock()
        owner_user.id = 1
        owner_user.municipio_id = 'default'

        chat_context = MagicMock()
        chat_context.context_data = {}

        location = {'latitude': -32.9, 'longitude': -68.8, 'address': 'Calle Falsa 123'}

        from flask import Flask
        flask_app = Flask(__name__)
        with flask_app.app_context():
            resp = responder_municipio(
                pregunta_original='',
                owner_user=owner_user,
                rubro_obj=None,
                viewer_user=None,
                chat_db_context=chat_context,
                anon_id='test',
                channel='whatsapp',
                location=location
            )

        self.assertEqual(detect_modalidad({'location': location}), 'location')
        self.assertIn('Recibí tu ubicación', resp['message_body'])
        self.assertIn('Calle Falsa 123', resp['message_body'])


if __name__ == '__main__':
    unittest.main()
