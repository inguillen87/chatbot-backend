import unittest
from unittest.mock import patch, MagicMock
import json
import sys
import os
from datetime import datetime

# Add project root to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipios import responder_municipio, CONTEXTO_MUNICIPIO, ConversationState
from tests.mocks import MockGeminiResponse
from app import create_app, db
from config import Config
from models import User

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

        self.chat_db_context = MagicMock()
        self.chat_db_context.context_data = {}
        self.chat_db_context.last_updated = datetime.now()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.municipios.llamar_gemini')
    def test_full_claim_flow(self, mock_llamar_gemini):
        # Step 1: User says "Hola" - This now triggers the GreetingHandler directly, bypassing the LLM.
        # The test needs to reflect this new, simpler logic.
        response = responder_municipio(
            pregunta_original="Hola",
            owner_user=self.owner_user,
            viewer_user=self.viewer_user,
            chat_db_context=self.chat_db_context,
            anon_id="test_anon_id",
            rubro_obj=self.owner_user.rubro
        )

        self.assertIn("¡Hola! Soy JuniA, el asistente virtual", response['message_body'])
        self.assertEqual(response['fuente'], "greeting_handler_v7_junin")

        # Step 2: User wants to make a claim
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Entendido. Para registrar tu reclamo por el contenedor de basura, ¿podrías decirme la dirección exacta donde se encuentra?",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "categoria": "Limpieza/Basura",
                "descripcion": "Contenedor de basura rebalsado"
            },
            "pedir_info": "ubicacion",
            "botones": []
        }

        response = responder_municipio(
            pregunta_original="Quiero hacer un reclamo por un contenedor de basura que está rebalsado.",
            owner_user=self.owner_user,
            viewer_user=self.viewer_user,
            chat_db_context=self.chat_db_context,
            anon_id="test_anon_id",
            rubro_obj=self.owner_user.rubro
        )

        self.assertEqual(response['message_body'], "Entendido. Para registrar tu reclamo por el contenedor de basura, ¿podrías decirme la dirección exacta donde se encuentra?")
        self.assertEqual(self.chat_db_context.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'], ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name)

        # Step 3: User provides location
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Perfecto. Registré tu reclamo por un contenedor lleno en Don Bosco 55, Junín, Mendoza. Para finalizar, ¿me podrías dar tu nombre completo y un teléfono?",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "categoria": "Limpieza/Basura",
                "descripcion": "Contenedor de basura rebalsado",
                "ubicacion": "Don Bosco 55, Junín, Mendoza"
            },
            "pedir_info": "nombre_y_telefono",
            "botones": []
        }

        response = responder_municipio(
            pregunta_original="don bosco 55 junin mendoza",
            owner_user=self.owner_user,
            viewer_user=self.viewer_user,
            chat_db_context=self.chat_db_context,
            anon_id="test_anon_id",
            rubro_obj=self.owner_user.rubro
        )

        self.assertEqual(response['message_body'], "Perfecto. Registré tu reclamo por un contenedor lleno en Don Bosco 55, Junín, Mendoza. Para finalizar, ¿me podrías dar tu nombre completo y un teléfono?")
        self.assertEqual(self.chat_db_context.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'], ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name)

        # Step 4: User provides personal data
        with patch('services.ticket_service.db.session.add'), \
             patch('services.ticket_service.db.session.flush'), \
             patch('services.ticket_service.db.session.commit'), \
             patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
            mock_ticket = MagicMock()
            mock_ticket.id = 123
            mock_ticket.nro_ticket = "M-12345"
            mock_crear_ticket.return_value = mock_ticket

            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "¡Listo! Tu reclamo por la luminaria en Mitre y Belgrano fue registrado con el número M-12345. Te avisaremos sobre cualquier novedad. ¿Necesitas algo más?",
                "accion_backend": "crear_reclamo",
                "datos_estructura": {
                    "target": "municipio",
                    "categoria": "Limpieza/Basura",
                    "descripcion": "Contenedor de basura rebalsado",
                    "ubicacion": "Don Bosco 55, Junín, Mendoza",
                    "nombre_usuario_detectado": "Juan Perez",
                    "telefono_detectado": "2611234567",
                    "email_detectado": "juan.perez@example.com"
                },
                "pedir_info": None,
                "botones": [
                    {"texto": "Consultar otro reclamo", "id_accion": "consultar_estado_ticket"},
                    {"texto": "Hacer otro reclamo", "id_accion": "iniciar_reclamo"}
                ]
            }

            response = responder_municipio(
                pregunta_original="Juan Perez 2611234567",
                owner_user=self.owner_user,
                viewer_user=self.viewer_user,
                chat_db_context=self.chat_db_context,
                anon_id="test_anon_id",
                rubro_obj=self.owner_user.rubro
            )

            self.assertIn("¡Reclamo recibido, Juan Perez!", response['message_body'])
            self.assertIn("M-M-12345", response['message_body'])
            # The context is now cleared by the action handler, so we expect it to be gone
            self.assertNotIn(CONTEXTO_MUNICIPIO, self.chat_db_context.context_data)
            mock_crear_ticket.assert_called_once()
            # Get the actual call arguments
            call_args, call_kwargs = mock_crear_ticket.call_args
            ticket_data = call_kwargs['ticket_data']
            self.assertEqual(ticket_data['nombre_vecino'], 'Juan Perez')
            self.assertEqual(ticket_data['telefono_vecino'], '+5492611234567')
            self.assertEqual(ticket_data['email_vecino'], 'juan.perez@example.com')

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
