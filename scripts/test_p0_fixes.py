# scripts/test_p0_fixes.py
import sys
import os
import logging
from unittest.mock import MagicMock, patch

# Add root directory to path so we can import app modules
sys.path.append(os.getcwd())

# Mock environment variables needed for config loading
os.environ["ENABLE_EMAIL_NOTIFICATIONS"] = "false"
os.environ["OPENAI_API_KEY"] = "mock_key"

# Import app to get context
from app import app
from services.actions.municipio_actions import HacerSugerenciaActionHandler
from services.email_service import enviar_email

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MockUser:
    def __init__(self, id, email, municipio_id=None, tenant_id=None, tenant_slug=None):
        self.id = id
        self.email = email
        self.municipio_id = municipio_id
        self.tenant_id = tenant_id
        self.tenant_slug = tenant_slug
        self.rol = 'admin'
        self.tipo_chat = 'municipio'
        self.name = "Mock User"
        self.telefono = "123456789"
        self.direccion = "Mock Address"
        self.dni = "12345678"

class MockTenant:
    def __init__(self, id, municipio_id, tipo):
        self.id = id
        self.municipio_id = municipio_id
        self.tipo = tipo

def test_hacer_sugerencia_handler():
    print("\n--- Testing HacerSugerenciaActionHandler (P0 Fixes) ---")

    # Mock context
    mock_user = MockUser(id=123, email="test@example.com", municipio_id=999, tenant_id=888, tenant_slug="demo-municipio")

    mock_context = {
        "user_obj": mock_user,
        "viewer_user_obj": mock_user,
        "cliente_id": 123,
        "anon_id": "anon-123",
        "channel": "whatsapp",
        "municipio_config_actual": {}
    }

    # Mock action data
    action_data = {
        "descripcion": "Esta es una sugerencia de prueba",
        "ubicacion": "Calle Falsa 123",
        "nombre": "Juan Perez",
        "dni": "12345678",
        "email": "juan@example.com",
        "telefono": "1234567890"
    }

    with app.app_context():
        # Mock service dependencies
        # We patch TenantProfile.query on the imported module in services/actions/municipio_actions.py
        # BUT since we are inside app context, maybe we don't need to patch query if we just mock the DB result?
        # Actually, since we are running without a real DB connected (likely), patching is safer.

        with patch("services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket") as mock_create_ticket, \
             patch("services.actions.municipio_actions.TenantProfile") as MockTenantProfile:

            # Setup mock tenant query
            mock_tenant_instance = MockTenant(id=777, municipio_id=999, tipo="municipio")
            MockTenantProfile.query.filter_by.return_value.first.return_value = mock_tenant_instance

            # Setup mock create ticket
            mock_create_ticket.return_value = {"id": 100, "nro_ticket": "123456"}

            handler = HacerSugerenciaActionHandler(mock_context)
            response = handler.execute(action_data)

            # Verification 1: Check Response Schema
            print(f"Response keys: {response.keys()}")
            assert "success" in response
            assert "message_body" in response
            assert "message_type" in response
            assert "options_list" in response
            print("✅ Response schema check passed.")

            # Verification 2: Check Ticket Creation Params (Tenant ID resolution)
            call_args = mock_create_ticket.call_args[1]["ticket_data"]
            print(f"Ticket Data sent to create: {call_args}")

            if call_args.get("tenant_id") == 888:
                 print("✅ Tenant ID resolved correctly from User object (888).")
            elif call_args.get("tenant_id") == 777:
                 print("✅ Tenant ID resolved correctly from Slug lookup (777).")
            else:
                 print(f"❌ Tenant ID resolution failed. Got: {call_args.get('tenant_id')}")

            if call_args.get("municipio_id") == 999:
                 print("✅ Municipio ID passed correctly (999).")
            else:
                 print(f"❌ Municipio ID resolution failed. Got: {call_args.get('municipio_id')}")

def test_email_smtp_safety():
    print("\n--- Testing SMTP Safety Flag (P0 Fixes) ---")

    with app.app_context():
        with patch("services.email_service._get_config_val") as mock_config, \
             patch("services.email_service.smtplib.SMTP") as mock_smtp, \
             patch("services.email_service.logger") as mock_logger:

            mock_config.return_value = "dummy_value"

            # Call send email
            result = enviar_email("dest@example.com", "Test", "Body")

            # Check result
            if result is False:
                print("✅ Email sending correctly blocked when ENABLE_EMAIL_NOTIFICATIONS is false.")
            else:
                print("❌ Email sending WAS NOT blocked.")

            # Verify log message
            found_log = False
            for call in mock_logger.info.call_args_list:
                if "Notifications disabled by feature flag" in call[0][0]:
                    found_log = True
                    break

            if found_log:
                print("✅ Correct log message generated.")
            else:
                print("❌ Log message for disabled notifications not found.")

if __name__ == "__main__":
    test_hacer_sugerencia_handler()
    test_email_smtp_safety()
