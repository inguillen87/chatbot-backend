import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from services.openai_maps_service import geocodificar_inversa_llm
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO

class GeoFlowTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch('services.openai_maps_service.openai.OpenAI')
    def test_geocodificar_inversa_llm_normalizes(self, mock_openai):
        client = MagicMock()
        client.responses.create.return_value = SimpleNamespace(
            output=[
                SimpleNamespace(
                    content=[
                        SimpleNamespace(text='{"formatted_address":"Addr Norm"}')
                    ]
                )
            ]
        )
        mock_openai.return_value = client
        data = geocodificar_inversa_llm(1.0, 2.0)
        assert data['formatted_address'] == 'Addr Norm'

    def test_confirmacion_ubicacion_prompt(self):
        owner_user = MagicMock(); owner_user.id = 1
        chat_context = MagicMock()
        chat_context.context_data = {
            CONTEXTO_MUNICIPIO: {
                'estado_conversacion': 'ESPERANDO_CONFIRMACION_UBICACION',
                'datos_parciales_llm_reclamo': {'ubicacion': 'Calle Falsa 123'}
            }
        }
        from app import create_app
        from config import TestConfig

        flask_app = create_app(TestConfig)
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
        assert 'Escribí los datos que querés corregir' in response['message_body']
        instance.execute.assert_not_called()

    def test_contacto_compacto(self):
        from services.municipio_responder import procesar_datos_contacto_compacto
        texto = "Juan Perez juan@mail.com 2615551234 30123456 Don Bosco 55 Junin"
        datos = procesar_datos_contacto_compacto(texto, {})
        assert datos['email'] == 'juan@mail.com'
        assert datos['telefono'].endswith('2615551234')
        assert datos['dni'] == '30123456'
        assert datos['nombre'].startswith('Juan')
        assert 'Don Bosco' in datos['direccion_contacto']

if __name__ == '__main__':
    unittest.main()
