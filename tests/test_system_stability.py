import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from config import Config
from models import User, Rubro, ChatSessionContext
from services.actions.municipio_actions import CrearReclamoActionHandler

class TestSystemStability(unittest.TestCase):

    def setUp(self):
        class TestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            WTF_CSRF_ENABLED = False

        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Create mock owner user
        self.owner_user = User(id=1, name="Municipio Test", email="municipio@test.com", municipio_id="test_muni")
        self.owner_user.set_password("password")
        db.session.add(self.owner_user)
        db.session.commit()


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_create_claim_without_name_falls_back_to_profile_name(self):
        """
        Tests that CrearReclamoActionHandler correctly uses context['profile_name']
        when the user's name is not provided in the action_data or viewer_user object,
        preventing the KeyError that motivated the fix.
        """
        # 1. Setup the context for the action handler to simulate an anonymous user
        handler_context = {
            "user_obj": self.owner_user,
            "viewer_user_obj": None, # No existing user object
            "anon_id": "whatsapp:+5491122334455", # Anonymous ID for the user
            "profile_name": "Marcelo From WhatsApp", # This is the crucial fallback data
            "channel": "whatsapp",
            "contexto_municipio_v2": {
                "datos_parciales_llm_reclamo": {}
            }
        }

        # 2. Define the data coming from the LLM after gathering all info
        # This simulates the final step where the LLM has everything except the name
        action_data = {
            "categoria": "Arbolado",
            "descripcion": "tengo arboles y hojas en mi garage tapando todo",
            "ubicacion": "Don Bosco 55, Junín, Mendoza",
            "distrito": "Junín",
            "telefono_detectado": "+5491122334455",
            "email_detectado": "marcelo.test@example.com"
            # Note: 'nombre_vecino' or 'usuario' is intentionally missing
        }

        # 3. Patch the dependencies of the handler (e.g., ticket creation)
        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket, \
             patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla'), \
             patch('services.actions.municipio_actions.enviar_notificacion_sms'):

            mock_ticket = MagicMock()
            mock_ticket.id = 999
            mock_ticket.nro_ticket = "M-STABILITY-TEST"
            mock_crear_ticket.return_value = mock_ticket

            # 4. Instantiate and execute the handler
            handler = CrearReclamoActionHandler(handler_context)
            result = handler.execute(action_data)

            # 5. Assertions
            self.assertTrue(result.get("success"), "The action should succeed.")
            self.assertIn("M-STABILITY-TEST", result.get("message_to_user", ""), "The response should contain the ticket number.")

            # The most important assertion: check what name was used to create the ticket
            mock_crear_ticket.assert_called_once()
            _, kwargs = mock_crear_ticket.call_args
            ticket_data = kwargs.get("ticket_data", {})

            # This verifies that the handler successfully fell back to the profile_name
            self.assertEqual(ticket_data.get("nombre_vecino"), "Marcelo From WhatsApp")
            self.assertIsNotNone(ticket_data.get("nombre_vecino"), "The neighbor's name should not be None in the final ticket data.")

if __name__ == "__main__":
    unittest.main()
