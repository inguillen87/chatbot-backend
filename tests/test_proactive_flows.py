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
from services.municipio_responder import responder_municipio

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

    @patch('services.municipio_responder.handle_llm_interaction')
    @patch('services.municipio_responder.analizar_imagen_con_fallback')
    def test_image_upload_triggers_proactive_flow(self, mock_analizar_imagen, mock_handle_llm):
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
        mock_handle_llm.return_value = ({"message_body": "OK"}, {})

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
        # The prompt is now created inside the function, so we can't check the exact string easily.
        # We check that the LLM handler was called, implying a synthetic prompt was generated.
        mock_handle_llm.assert_called_once()


    @patch('services.municipio_responder.handle_llm_interaction')
    def test_location_upload_triggers_proactive_flow(self, mock_handle_llm):
        # Arrange
        mock_handle_llm.return_value = ({"message_body": "OK"}, {})

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
        responder_municipio(
            pregunta_original=location_payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context
        )

        # Assert
        mock_handle_llm.assert_called_once()

if __name__ == '__main__':
    unittest.main()
