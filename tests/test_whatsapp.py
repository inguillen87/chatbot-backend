import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import json

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.chat_orchestrator import ChatOrchestrator
from models import User, Rubro, ChatSessionContext

class TestWhatsAppFlow(unittest.TestCase):

    def setUp(self):
        from app import create_app, db
        self.app = create_app('testing')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Mock owner user (municipio)
        self.owner_user = User(id=1, name="Municipio Test", email="test@municipio.com", tipo_chat="municipio", rol="admin")
        self.owner_user.set_password("admin")

        # Mock rubro
        self.rubro = Rubro(id=1, clave="municipios", nombre="Municipios")

        db.session.add(self.owner_user)
        db.session.add(self.rubro)
        db.session.commit()

        self.owner_user.rubro_id = self.rubro.id
        db.session.commit()

        # Context for the orchestrator
        self.initial_context = {
            "owner_user_id": self.owner_user.id,
            "viewer_user_id": None, # Anonymous user
            "anon_id": "whatsapp:+1234567890",
            "rubro_clave": "municipios",
            "channel": "whatsapp",
            "chat_session_uuid": "test-whatsapp-session-123",
            "municipio_config_actual": {"categorias_reclamo": ["Alumbrado", "Bacheo", "Residuos"]},
            "nombre_pyme_o_municipio": "Municipio Test"
        }

    def tearDown(self):
        from app import db
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.gemini_bridge.llamar_gemini')
    def test_full_whatsapp_claim_flow(self, mock_llamar_gemini):
        orchestrator = ChatOrchestrator(self.initial_context)

        # 1. User sends initial message
        user_message_1 = "Hola, hay un poste de luz roto en mi calle"

        # Mock LLM response for initial message (intent recognition)
        mock_llamar_gemini.return_value = {
            "accion_backend": "iniciar_reclamo",
            "respuesta_usuario": "Entendido, quieres iniciar un reclamo por un poste de luz. Primero, ¿podrías decirme la dirección exacta?",
            "datos_estructura": {
                "categoria": "Alumbrado",
                "descripcion": "Poste de luz roto"
            },
            "pedir_info": "ubicacion",
            "botones": []
        }

        response_1 = orchestrator.handle_message(user_message_1)

        self.assertEqual("Entendido, quieres iniciar un reclamo por un poste de luz. Primero, ¿podrías decirme la dirección exacta?", response_1["message_to_user"])
        mock_llamar_gemini.assert_called_once()

        # Verify context was saved
        context_from_db = ChatSessionContext.query.filter_by(chat_session_id="test-whatsapp-session-123").first()
        self.assertIsNotNone(context_from_db)
        saved_data = json.loads(context_from_db.context_data)
        self.assertEqual(saved_data['municipio']['estado_conversacion'], 'ESPERANDO_DIRECCION_RECLAMO')
        self.assertEqual(saved_data['municipio']['categoria_reclamo'], 'Alumbrado')

        # 2. User provides location
        user_message_2 = "Es en Av. Siempre Viva 123"

        # Mock LLM response for location
        mock_llamar_gemini.return_value = {
            "accion_backend": "crear_reclamo",
            "respuesta_usuario": "Gracias. He registrado tu reclamo por el poste en Av. Siempre Viva 123. ¿Te gustaría añadir una foto?",
            "datos_estructura": {
                "ubicacion": "Av. Siempre Viva 123, Springfield"
            },
            "pedir_info": "foto",
            "botones": [{"texto": "Sí, añadir foto", "id_accion": "adjuntar_foto_reclamo"}, {"texto": "No, gracias", "id_accion": "finalizar_reclamo_sin_foto"}]
        }

        # We need a new orchestrator instance to load the updated context, or have a method to update it
        orchestrator_step_2 = ChatOrchestrator(self.initial_context) # Re-instantiating simulates a new request
        response_2 = orchestrator_step_2.handle_message(user_message_2)

        self.assertIn("He registrado tu reclamo", response_2["message_to_user"])
        self.assertIn("Av. Siempre Viva 123", response_2["message_to_user"])
        self.assertIn("añadir una foto", response_2["message_to_user"])

        # Verify context update
        context_from_db_2 = ChatSessionContext.query.filter_by(chat_session_id="test-whatsapp-session-123").first()
        saved_data_2 = json.loads(context_from_db_2.context_data)
        self.assertEqual(saved_data_2['municipio']['estado_conversacion'], 'ESPERANDO_FOTO_RECLAMO')
        self.assertEqual(saved_data_2['municipio']['direccion_reclamo'], 'Av. Siempre Viva 123, Springfield')

        # 3. User declines to send a photo
        user_message_3 = "No, gracias"

        # Mock LLM response for finishing claim
        mock_llamar_gemini.return_value = {
            "accion_backend": "finalizar_reclamo",
            "respuesta_usuario": "Perfecto. Tu reclamo ha sido generado con el número #M-TEST123. Recibirás notificaciones sobre su estado. ¿Algo más?",
            "datos_estructura": {
                "confirmacion_final": True
            },
            "pedir_info": None,
            "botones": []
        }

        # Mock the ticket creation in the action handler
        with patch('services.actions.municipio_actions.servicio_tickets.crear_ticket_anonimo') as mock_crear_ticket:
            mock_crear_ticket.return_value = "M-TEST123"

            orchestrator_step_3 = ChatOrchestrator(self.initial_context)
            response_3 = orchestrator_step_3.handle_message(user_message_3)

            mock_crear_ticket.assert_called_once()
            args, kwargs = mock_crear_ticket.call_args
            self.assertEqual(kwargs['municipio_id'], self.owner_user.id)
            self.assertEqual(kwargs['anon_id'], "whatsapp:+1234567890")
            self.assertEqual(kwargs['categoria'], "Alumbrado")
            self.assertEqual(kwargs['descripcion'], "Poste de luz roto")
            self.assertEqual(kwargs['direccion'], "Av. Siempre Viva 123, Springfield")

            self.assertIn("reclamo ha sido generado", response_3["message_to_user"])
            self.assertIn("#M-TEST123", response_3["message_to_user"])

        # Verify context is cleared/reset
        context_from_db_3 = ChatSessionContext.query.filter_by(chat_session_id="test-whatsapp-session-123").first()
        saved_data_3 = json.loads(context_from_db_3.context_data)
        self.assertNotIn('estado_conversacion', saved_data_3.get('municipio', {}))
        self.assertNotIn('categoria_reclamo', saved_data_3.get('municipio', {}))


if __name__ == '__main__':
    unittest.main()
