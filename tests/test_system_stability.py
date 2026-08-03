import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app, db
from config import Config
from models import TenantProfile, User, Rubro
from services.actions.municipio_actions import CrearReclamoActionHandler

class TestSystemStability(unittest.TestCase):

    def setUp(self):
        class TestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
            WTF_CSRF_ENABLED = False
            SESSION_COOKIE_SECURE = False
            CELERY_TASK_ALWAYS_EAGER = True
            DEBUG = False
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False
            SKIP_INIT_TENANTS = True
            SESSION_TYPE = "null"

        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Create necessary users and rubros for tests
        self.rubro = Rubro(nombre="municipio", clave="municipio")
        self.owner_user = User(
            id=1,
            tipo_chat='municipio',
            rol='admin',
            email='admin@test.com',
            name='Admin',
            rubro=self.rubro,
            municipio_id=1,
            tenant_slug='system-stability',
            password_hash='hash',
        )
        self.tenant = TenantProfile(
            slug='system-stability',
            nombre='Municipio System Stability',
            tipo='municipio',
            municipio_id=1,
            is_active=True,
        )
        db.session.add_all([self.rubro, self.owner_user, self.tenant])
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
            "tenant_profile": self.tenant,
            "tenant_id": self.tenant.id,
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
            "email_detectado": "marcelo.test@example.com",
            "pin": "112233",
            "dni": "12345678"
            # Note: 'nombre_vecino' or 'usuario' is intentionally missing
        }

        # 3. Patch the dependencies of the handler (e.g., ticket creation)
        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket, \
             patch('services.actions.municipio_actions.direccion_es_valida', return_value=True), \
             patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla'), \
             patch('services.actions.municipio_actions.enviar_notificacion_sms'):

            # Fix: The service now returns a dict representing the ticket
            mock_crear_ticket.return_value = {"id": 999, "nro_ticket": "M-STABILITY-TEST"}

            # 4. Instantiate and execute the handler
            handler = CrearReclamoActionHandler(handler_context)
            result = handler.execute(action_data)

            # 5. Assertions
            self.assertTrue(result.get("success"), "The action should succeed.")
            self.assertEqual(result.get("data", {}).get("nro_ticket"), "M-STABILITY-TEST")

            # Verify that the fallback name was used in the ticket creation
            mock_crear_ticket.assert_called_once()
            _, kwargs = mock_crear_ticket.call_args
            self.assertEqual(kwargs['ticket_data']['nombre_vecino'], "Marcelo From WhatsApp")

if __name__ == '__main__':
    unittest.main()
