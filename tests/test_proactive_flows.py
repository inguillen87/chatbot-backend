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
from services.municipio_responder import responder_municipio, ConversationState

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
            chat_db_context=chat_context
        )

        # Assert
        mock_analizar_imagen.assert_called_once()
        mock_start_flow.assert_called_once()
        call_args = mock_start_flow.call_args[1]
        self.assertEqual(call_args['datos_iniciales']['categoria'], "Arreglo de calle")
        self.assertEqual(call_args['datos_iniciales']['descripcion'], "Hay un bache grande en la calle.")


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
            chat_db_context=chat_context
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

            action_payload = {"action": "iniciar_reclamo_con_ubicacion"}

            response_2 = responder_municipio(
                pregunta_original=action_payload,
                owner_user=owner_user,
                rubro_obj=rubro_obj,
                viewer_user=owner_user,
                chat_db_context=chat_context
            )

            mock_start_flow.assert_called_once()
            call_args = mock_start_flow.call_args[1]
            self.assertIn("Plaza Independencia, Mendoza", call_args['datos_iniciales']['direccion'])

if __name__ == '__main__':
    unittest.main()
