import unittest
import os
from unittest.mock import patch, MagicMock

from services.openai_maps_service import geocodificar_inversa_llm
from services.municipio_responder import (
    responder_municipio,
    CONTEXTO_MUNICIPIO,
    detect_modalidad,
    ReclamoFlowHandler,
    ReclamoState,
    ConversationState,
)

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
        os.environ['GOOGLE_MAPS_API_KEY'] = 'TEST'
        chat_context.context_data = {
            CONTEXTO_MUNICIPIO: {
                'estado_conversacion': 'ESPERANDO_CONFIRMACION_UBICACION',
                'datos_parciales_llm_reclamo': {
                    'ubicacion': 'Calle Falsa 123',
                    'coordenadas': {'lat': 1.0, 'lng': 2.0},
                    'label_ubicacion': 'Casa'
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
        assert 'https://maps.google.com/?q=1.0,2.0' in resp['message_body']
        assert '1) Sí' in resp['message_body']
        assert resp.get('image_url')
        assert 'Casa' in resp['message_body']
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
        os.environ['GOOGLE_MAPS_API_KEY'] = 'TEST'

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
        assert resp.get('image_url')

    def test_handle_direccion_confirms_and_links(self):
        chat_context = MagicMock()
        chat_context.context_data = {}
        flow_context = {
            'state': ReclamoState.ESPERANDO_DIRECCION.name,
            'datos_reclamo': {'categoria': 'Bache', 'descripcion': 'desc'},
        }
        context = {
            'chat_db_context_data': {CONTEXTO_MUNICIPIO: {'reclamo_flow_v2': flow_context}}
        }
        handler = ReclamoFlowHandler(context, chat_context)
        with patch('services.municipio_responder.handle_direccion') as mock_resolver:
            mock_resolver.return_value = {
                'ubicacion': 'Calle Falsa 123',
                'coordenadas': {'lat': 1.0, 'lng': 2.0},
                'maps_search_url': 'https://maps.google.com/?q=1.0,2.0',
            }
            resp = handler.handle_direccion('Calle Falsa 123', {})
        assert (
            context['chat_db_context_data'][CONTEXTO_MUNICIPIO]['estado_conversacion']
            == ConversationState.ESPERANDO_CONFIRMACION_UBICACION.name
        )
        assert 'https://maps.google.com/?q=1.0,2.0' in resp['message_body']
        assert any(
            o.get('action_id') == 'confirmar_ubicacion' for o in resp.get('options_list', [])
        )
        resp2 = handler.handle_direccion('1', {'action_id': 'confirmar_ubicacion'})
        assert handler.municipal_ctx.get('estado_conversacion') is None
        assert handler.flow_context['state'] == ReclamoState.ESPERANDO_FOTO.name
        assert 'foto' in resp2['message_body'].lower()

    @patch('services.municipio_responder.AddressResolver.resolve')
    def test_handle_direccion_requires_coords(self, mock_resolve):
        mock_resolve.return_value = {
            'formatted': 'Don Bosco 55',
            'lat': None,
            'lon': None,
        }
        municipio_cfg = {
            'ciudad': 'Junín',
            'provincia': 'Mendoza',
            'pais': 'AR',
            'bounds': (0, 0, 1, 1),
        }
        from services.municipio_responder import handle_direccion
        result = handle_direccion('Don Bosco 55', {}, municipio_cfg)
        assert result is None

    def test_no_duplicate_prompts(self):
        owner_user = MagicMock(); owner_user.id = 1
        chat_context = MagicMock()
        chat_context.context_data = {
            CONTEXTO_MUNICIPIO: {
                'media_recibida': True,
                'ubicacion_confirmada': True,
                'contacto_usuario': {'nombre': 'Juan'},
                'estado_conversacion': None,
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
                channel='whatsapp',
            )

        # Should only ask for remaining contact data, not for media or location again
        body = resp['message_body'].lower()
        self.assertIn('necesito estos datos', body)
        self.assertNotIn('ubicación', body)
        self.assertNotIn('foto', body)

    @patch('services.actions.municipio_actions.validar_y_formatear_direccion')
    def test_estado_barrio_reclamo(self, mock_geo):
        mock_geo.return_value = {"formatted_address": "Calle Falsa 123"}
        datos_llm = {
            "categoria": "Alumbrado",
            "descripcion": "Luz rota",
            "ubicacion": "Calle Falsa 123",
            "usuario": "Test User",
        }
        ctx = {
            "viewer_user_obj": None,
            "user_obj": MagicMock(id=1, municipio_id="testmuni"),
            "anon_id": "test",
            CONTEXTO_MUNICIPIO: {},
        }
        handler = CrearReclamoActionHandler(ctx)
        handler.execute(datos_llm)
        self.assertEqual(
            ctx[CONTEXTO_MUNICIPIO]["estado_conversacion"],
            "ESPERANDO_BARRIO_RECLAMO",
        )

    @patch('services.municipio_responder.validar_y_formatear_direccion')
    @patch('services.actions.municipio_actions.validar_y_formatear_direccion')
    def test_handle_barrio_reintento_exitoso(self, mock_geo_action, mock_geo_responder):
        mock_geo_action.return_value = {"formatted_address": "Calle Falsa 123"}
        mock_geo_responder.return_value = {
            "formatted_address": "Calle Falsa 123, Centro",
            "lat": 1.0,
            "lng": 2.0,
            "barrio": "Centro",
        }
        datos_llm = {
            "categoria": "Alumbrado",
            "descripcion": "Luz rota",
            "ubicacion": "Calle Falsa 123",
            "usuario": "Test User",
        }
        muni_ctx = {}
        context = {
            "viewer_user_obj": None,
            "user_obj": MagicMock(id=1, municipio_id="testmuni"),
            "anon_id": "test",
            CONTEXTO_MUNICIPIO: muni_ctx,
            "chat_db_context_data": {CONTEXTO_MUNICIPIO: muni_ctx},
        }
        CrearReclamoActionHandler(context).execute(datos_llm)
        self.assertEqual(muni_ctx["estado_conversacion"], "ESPERANDO_BARRIO_RECLAMO")
        handler = ReclamoFlowHandler(context, MagicMock())
        resp = handler.handle_barrio_reclamo("Centro")
        self.assertEqual(muni_ctx["estado_conversacion"], "ESPERANDO_DATOS_CONTACTO")
        self.assertEqual(
            muni_ctx.get("coordenadas_reclamo"),
            {"lat": 1.0, "lng": 2.0},
        )
        self.assertEqual(
            muni_ctx.get("datos_parciales_llm_reclamo", {}).get("distrito"),
            "Centro",
        )
        self.assertEqual(resp.get("next_state_hint"), "ESPERANDO_DATOS_CONTACTO")


if __name__ == '__main__':
    unittest.main()
