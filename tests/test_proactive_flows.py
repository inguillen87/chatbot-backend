import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import json

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app, db
from config import TestConfig
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio, ConversationState, ReclamoState

class TestProactiveFlows(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        rubro = Rubro(id=1, clave='municipios', nombre='municipios')
        owner_user = User(id=1, tipo_chat='municipio', rol='admin', email='admin@test.com', name='Admin', rubro=rubro)
        owner_user.set_password('password')
        db.session.add_all([rubro, owner_user])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.municipio_responder.ReclamoFlowHandler.start_flow')
    @patch('services.municipio_responder.analizar_imagen_con_fallback')
    def test_image_upload_triggers_proactive_flow(self, mock_analizar_imagen, mock_start_flow):
        # Arrange
        mock_analizar_imagen.return_value = {
            "raw_response": json.dumps({
                "intent": "crear_reclamo",
                "data": {
                    "categoria": "Arreglo de calle",
                    "descripcion": "Hay un bache grande en la calle."
                }
            })
        }
        mock_start_flow.return_value = {"message_body": "OK, starting claim."}

        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_session_image', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        image_payload = {
            "pregunta": "",
            "es_foto": True,
            "foto_url": "http://example.com/bache.jpg"
        }

        # Act
        responder_municipio(
            pregunta_original=image_payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        # Assert
        mock_analizar_imagen.assert_called_once()
        mock_start_flow.assert_called_once()
        call_args = mock_start_flow.call_args[1]
        self.assertEqual(call_args['datos_iniciales']['categoria'], "Arreglo de calle")
        self.assertEqual(call_args['datos_iniciales']['descripcion'], "Hay un bache grande en la calle.")
        self.assertEqual(call_args['datos_iniciales']['foto_url'], "http://example.com/bache.jpg")


    def test_location_upload_triggers_proactive_flow(self):
        # Arrange
        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_session_location', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        location_payload = {
            "pregunta": "",
            "es_ubicacion": True,
            "ubicacion_usuario": {
                "latitude": -32.889,
                "longitude": -68.845,
                "address": "Plaza Independencia, Mendoza"
            }
        }

        # Act
        response = responder_municipio(
            pregunta_original=location_payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        # Assert
        self.assertIn("Recibí tu ubicación", response["message_body"])
        self.assertIn("Iniciar un Reclamo", [btn["texto"] for btn in response["options_list"]])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['estado_conversacion'],
            ConversationState.ESPERANDO_INTENCION_UBICACION.name
        )
        self.assertIsNotNone(chat_context.context_data['contexto_municipio_v2'].get('ubicacion_contextual'))

        # Now, simulate user choosing to create a claim
        with patch('services.municipio_responder.ReclamoFlowHandler.start_flow') as mock_start_flow:
            mock_start_flow.return_value = {"message_body": "OK, starting claim."}

            response_2 = responder_municipio(
                pregunta_original="iniciar_reclamo_con_ubicacion",
                owner_user=owner_user,
                rubro_obj=rubro_obj,
                viewer_user=owner_user,
                chat_db_context=chat_context,
                channel="whatsapp",
            )

            mock_start_flow.assert_called_once()
            call_args = mock_start_flow.call_args[1]
            self.assertIn("Plaza Independencia, Mendoza", call_args['datos_iniciales']['direccion'])
            self.assertEqual(response_2["message_body"], "OK, starting claim.")

    def test_location_menu_accepts_numeric_selection(self):
        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        initial_context = {
            'contexto_municipio_v2': {
                'menu_opciones': [
                    {'texto': '📝 Iniciar un Reclamo', 'action_id': 'iniciar_reclamo'},
                    {'texto': '💡 Enviar una Sugerencia', 'action_id': 'enviar_sugerencia'},
                    {'texto': 'Cancelar', 'action_id': 'cancelar'},
                ]
            }
        }
        chat_context = ChatSessionContext(chat_session_id='test_session_location_numeric', user_id=1, context_data=initial_context)
        db.session.add(chat_context)
        db.session.commit()

        location_payload = {
            "pregunta": "",
            "es_ubicacion": True,
            "ubicacion_usuario": {
                "latitude": -32.889,
                "longitude": -68.845,
                "address": "Plaza Independencia, Mendoza",
            },
        }

        responder_municipio(
            pregunta_original=location_payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        opciones_guardadas = chat_context.context_data['contexto_municipio_v2']['menu_opciones']
        self.assertEqual(
            [opt['action_id'] for opt in opciones_guardadas],
            ['iniciar_reclamo_con_ubicacion', 'enviar_sugerencia_con_ubicacion', 'cancelar']
        )

        with patch('services.municipio_responder.ReclamoFlowHandler.start_flow') as mock_start_flow:
            mock_start_flow.return_value = {"message_body": "OK, starting claim."}

            responder_municipio(
                pregunta_original="1",
                owner_user=owner_user,
                rubro_obj=rubro_obj,
                viewer_user=owner_user,
                chat_db_context=chat_context,
                channel="whatsapp",
            )

            mock_start_flow.assert_called_once()

    def test_location_menu_reprompts_on_invalid_selection(self):
        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_session_location_invalid', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        location_payload = {
            "pregunta": "",
            "es_ubicacion": True,
            "ubicacion_usuario": {
                "latitude": -32.889,
                "longitude": -68.845,
                "address": "Plaza Independencia, Mendoza",
            },
        }

        responder_municipio(
            pregunta_original=location_payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        response = responder_municipio(
            pregunta_original="xyz123",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['estado_conversacion'],
            ConversationState.ESPERANDO_INTENCION_UBICACION.name,
        )
        option_texts = [opt["texto"] for opt in response["options_list"]]
        self.assertIn("Iniciar un Reclamo", option_texts)

    @patch('services.municipio_responder.analizar_imagen_con_fallback')
    def test_image_then_location_advances_claim_flow(self, mock_analizar_imagen):
        mock_analizar_imagen.return_value = {
            "raw_response": json.dumps({
                "intent": "crear_reclamo",
                "data": {
                    "categoria": "Arreglo de calle",
                    "descripcion": "Bache en la calle"
                }
            })
        }

        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='session_img_loc', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        image_payload = {"pregunta": "", "es_foto": True, "foto_url": "http://example.com/bache.jpg"}
        response = responder_municipio(
            pregunta_original=image_payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        self.assertIn("dirección", response["message_body"])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['reclamo_flow_v2']['state'],
            ReclamoState.ESPERANDO_DIRECCION.name,
        )

        location_payload = {
            "pregunta": "",
            "es_ubicacion": True,
            "ubicacion_usuario": {
                "address": "Calle Falsa 123",
                "latitude": -32.889,
                "longitude": -68.845,
            },
        }

        response2 = responder_municipio(
            pregunta_original=location_payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        self.assertIn("Para finalizar", response2["message_body"])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['reclamo_flow_v2']['datos_reclamo']['direccion'],
            "Calle Falsa 123",
        )
        self.assertNotEqual(
            chat_context.context_data['contexto_municipio_v2'].get('estado_conversacion'),
            ConversationState.ESPERANDO_INTENCION_UBICACION.name,
        )

    def test_location_link_triggers_proactive_menu(self):
        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_location_link', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        response = responder_municipio(
            pregunta_original="https://maps.app.goo.gl/example",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        self.assertIn("Recibí tu ubicación", response["message_body"])
        opciones = [opt["texto"] for opt in response.get("options_list", [])]
        self.assertIn("Iniciar un Reclamo", opciones)
        contexto = chat_context.context_data['contexto_municipio_v2']
        self.assertEqual(
            contexto['estado_conversacion'],
            ConversationState.ESPERANDO_INTENCION_UBICACION.name,
        )
        self.assertEqual(contexto['ubicacion_contextual'].get('source'), 'link')

    @patch('services.municipio_responder.extract_multiple_contact_details_llm', return_value={})
    @patch('services.municipio_responder.extract_complaint_details_llm', return_value={})
    @patch('services.municipio_responder.ReclamoFlowHandler.start_flow')
    def test_free_text_claim_bootstrap(self, mock_start_flow, _mock_complaint, _mock_contacts):
        mock_start_flow.return_value = {"message_body": "OK"}

        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        initial_context = {
            'contexto_municipio_v2': {
                'estado_conversacion': ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
            }
        }
        chat_context = ChatSessionContext(chat_session_id='test_auto_text', user_id=1, context_data=initial_context)
        db.session.add(chat_context)
        db.session.commit()

        response = responder_municipio(
            pregunta_original="quiero que corten las ramas de un arbol caido en barrio jardin",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context,
            channel="whatsapp",
        )

        mock_start_flow.assert_called_once()
        kwargs = mock_start_flow.call_args.kwargs
        self.assertEqual(kwargs.get('categoria_inicial'), 'Arbolado')
        self.assertTrue(response.get("message_body"))

if __name__ == '__main__':
    unittest.main()
