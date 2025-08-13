import unittest
from unittest.mock import patch, MagicMock
import json
import sys
import os
from datetime import datetime

# Add project root to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO, ConversationState
from tests.mocks import MockGeminiResponse
from app import create_app, db
from config import Config
from models import User, ChatSessionContext

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class TestSimulatedClaimFlow(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.maxDiff = None
        # Mock user and context objects that would be created in a real request
        self.owner_user = MagicMock()
        self.owner_user.id = 1
        self.owner_user.nombre_empresa = "Municipalidad de Junin"
        self.owner_user.rubro.nombre = "municipio"

        self.viewer_user = None # Simulate anonymous user

        # Use a real ChatSessionContext object from the database
        self.chat_session = ChatSessionContext(chat_session_id="test_simulated_flow")
        self.chat_session.context_data = {}
        db.session.add(self.chat_session)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.municipio_responder.llamar_gemini')
    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_full_claim_flow(self, mock_crear_ticket, mock_llamar_gemini):
        with self.app.test_request_context():
            # --- Mock Setup ---
            mock_ticket = MagicMock()
            mock_ticket.id = 123
            mock_ticket.nro_ticket = "54321"
            mock_crear_ticket.return_value = mock_ticket

            mock_llamar_gemini.side_effect = [
                # Step 1: User wants to make a claim
                {
                    "message_body": "Entendido. Para registrar tu reclamo por el contenedor de basura, ¿podrías decirme la dirección exacta donde se encuentra?",
                    "accion_backend": "iniciar_reclamo",
                    "datos_estructura": {"target": "municipio", "categoria": "Limpieza/Basura", "descripcion": "Contenedor de basura rebalsado"},
                    "pedir_info": "ubicacion",
                },
                # Step 2: User provides location
                {
                    "message_body": "Perfecto. Registré tu reclamo por un contenedor lleno en Don Bosco 55, Junín, Mendoza. Para finalizar, ¿me podrías dar tu nombre completo y un teléfono?",
                    "accion_backend": "iniciar_reclamo",
                    "datos_estructura": {"target": "municipio", "ubicacion": "Don Bosco 55, Junín, Mendoza"},
                    "pedir_info": "nombre_y_telefono",
                },
                # Step 3: User provides personal data -> This triggers the ticket creation
                {
                    "accion_backend": "crear_reclamo",
                    "datos_estructura": {
                        "target": "municipio",
                        "nombre_usuario_detectado": "Juan Perez",
                        "telefono_detectado": "2611234567",
                        "email_detectado": "juan.perez@example.com"
                    },
                }
            ]

            # --- Execution ---
            # 1. Start claim
            chat_session_from_db = db.session.get(ChatSessionContext, "test_simulated_flow")
            response1 = responder_municipio(
                pregunta_original="Quiero hacer un reclamo por un contenedor de basura que está rebalsado.",
                owner_user=self.owner_user, viewer_user=self.viewer_user, chat_db_context=chat_session_from_db,
                anon_id="test_anon_id", rubro_obj=self.owner_user.rubro
            )
            db.session.commit() # Commit context changes

            # 2. Provide location
            chat_session_from_db = db.session.get(ChatSessionContext, "test_simulated_flow")
            response2 = responder_municipio(
                pregunta_original="don bosco 55 junin mendoza",
                owner_user=self.owner_user, viewer_user=self.viewer_user, chat_db_context=chat_session_from_db,
                anon_id="test_anon_id", rubro_obj=self.owner_user.rubro
            )
            db.session.commit() # Commit context changes

            # 3. Provide contact info and finalize
            chat_session_from_db = db.session.get(ChatSessionContext, "test_simulated_flow")
            response3 = responder_municipio(
                pregunta_original="Juan Perez 2611234567",
                owner_user=self.owner_user, viewer_user=self.viewer_user, chat_db_context=chat_session_from_db,
                anon_id="test_anon_id", rubro_obj=self.owner_user.rubro
            )
            db.session.commit() # Commit context changes


            # --- Assertions ---
            # Step 1 Assertions
            chat_session_after_step1 = db.session.get(ChatSessionContext, "test_simulated_flow")
            self.assertEqual(response1['message_body'], "Entendido. Para registrar tu reclamo por el contenedor de basura, ¿podrías decirme la dirección exacta donde se encuentra?")
            self.assertEqual(chat_session_after_step1.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'], 'ESPERANDO_INFO_RECLAMO_LLM')

            # Step 2 Assertions
            self.assertEqual(response2['message_body'], "Perfecto. Registré tu reclamo por un contenedor lleno en Don Bosco 55, Junín, Mendoza. Para finalizar, ¿me podrías dar tu nombre completo y un teléfono?")

            # Step 3 Assertions
            chat_session_after_step3 = db.session.get(ChatSessionContext, "test_simulated_flow")
            self.assertIn("¡Reclamo recibido, Juan Perez!", response3['message_body'])
            self.assertIn("M-54321", response3['message_body'])

            final_context_data = chat_session_after_step3.context_data.get(CONTEXTO_MUNICIPIO, {})
            self.assertEqual(final_context_data.get('estado_conversacion'), 'CONVERSACION_GENERAL_LLM')
            self.assertFalse(final_context_data.get('datos_parciales_llm_reclamo'))

            mock_crear_ticket.assert_called_once()
            call_args, call_kwargs = mock_crear_ticket.call_args
            ticket_data = call_kwargs['ticket_data']
            self.assertEqual(ticket_data['nombre_vecino'], 'Juan Perez')
            self.assertEqual(ticket_data['telefono_vecino'], '+5492611234567')
            self.assertEqual(ticket_data['direccion'], 'Don Bosco 55, Junín, Mendoza')
            self.assertEqual(ticket_data['categoria'], 'Limpieza/Basura')

    def test_get_tickets_del_usuario_logic(self):
        from datetime import datetime
        from models import MunicipioTicket
        with self.app.app_context():
            # Create a real user and ticket
            admin_user = User(
                email='admin@junin.com',
                name='Admin Junin',
                rol='admin',
                municipio_id=1,
                tipo_chat='municipio'
            )
            admin_user.set_password('adminpass')
            db.session.add(admin_user)
            db.session.commit()

            ticket1 = MunicipioTicket(
                municipio_id=1,
                user_id=admin_user.id,
                asunto='Bache en la calle',
                categoria='calle',
                pregunta='Hay un bache grande en la calle principal.',
                fecha=datetime.fromisoformat("2025-07-30T22:40:08.214704")
            )
            db.session.add(ticket1)
            db.session.commit()

            from routes.ticket import get_tickets_del_usuario_logic
            with self.app.test_request_context('/tickets'):
                response = get_tickets_del_usuario_logic(admin_user)
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                self.assertIn('tickets', data)
                self.assertEqual(len(data['tickets']), 1)
                self.assertEqual(data['tickets'][0]['asunto'], 'Bache en la calle')
                self.assertIn('id', data['tickets'][0]) # FIX: The new serialization uses 'id'

if __name__ == '__main__':
    unittest.main()
