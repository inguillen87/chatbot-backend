import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.actions.municipio_claim_actions import CrearReclamoActionHandler
from models import User
from config import Config # Removed TestConfig
from app import create_app
from extensions import db


class TestConfigAll(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get('TEST_DATABASE_URL', 'sqlite:///:memory:')
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    CELERY_TASK_ALWAYS_EAGER = True
    DEBUG = False # Ensure debug is False for some tests if needed, or True if that's the default

class TestAccionesMunicipio(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Set up once for all tests in this class."""
        cls.app = create_app(config_class=TestConfigAll)
        with cls.app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        """Tear down once after all tests in this class."""
        with cls.app.app_context():
            db.drop_all()

    def setUp(self):
        """Set up for each test."""
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.session = db.session
        self.session.begin_nested()

    def tearDown(self):
        """Tear down after each test."""
        self.session.rollback()
        self.session.remove()
        self.app_context.pop()

    @patch('services.municipios.servicio_tickets.crear_nuevo_ticket')
    @patch('services.municipios.formatear_telefono_e164')
    @patch('services.municipios.validar_telefono')
    @patch('services.municipios.validar_email')
    @patch('services.municipios.parse_direccion_completa')
    @patch('services.municipios.enviar_notificacion_whatsapp_con_plantilla')
    def test_accion_crear_reclamo_exito_completo_llm(
        self, mock_enviar_whatsapp, mock_parse_direccion, mock_validar_email,
        mock_validar_telefono, mock_formatear_telefono, mock_crear_ticket
    ):
        mock_ticket_simulado = MagicMock()
        mock_ticket_simulado.nro_ticket = "12345"
        mock_ticket_simulado.id = 1
        mock_crear_ticket.return_value = mock_ticket_simulado

        mock_validar_telefono.return_value = True
        mock_formatear_telefono.return_value = "+5491122334455"
        mock_validar_email.return_value = True

        mock_parse_direccion.return_value = {
            "calle": "Calle Falsa", "numero": "123", "localidad": "Springfield"
        }

        datos_llm = {
            "categoria": "Alumbrado", "descripcion": "Poste de luz caído y chispas.",
            "ubicacion": "Calle Falsa 123, Springfield",
            "coordenadas": {"lat": -32.8908, "lon": -68.8272},
            "usuario": "Homero Simpson", "telefono": "91122334455", "email": "homero@example.com"
        }

        mock_viewer_user = MagicMock(spec=User)
        mock_viewer_user.id = 100; mock_viewer_user.nombre = "Homero J. Simpson"
        mock_viewer_user.telefono = "2615550000"; mock_viewer_user.email = "hsimpson@springfield.com"

        mock_owner_user = MagicMock(spec=User)
        mock_owner_user.id = 1; mock_owner_user.municipio_id = "springfield_municipio"

        context = {
            "viewer_user_obj": mock_viewer_user, "owner_user": mock_owner_user, "anon_id": None,
            "municipio_config_actual": {"ejemplo_direccion": "Av. Siempreviva 742"},
            "chat_session_uuid": "test-session-uuid-123",
            "chat_db_context_data": {"processed_idempotency_keys": {}}
        }
        respuesta = accion_crear_reclamo_municipio(datos_llm, context)

        self.assertIn("message_body", respuesta)
        self.assertIn(mock_ticket_simulado.nro_ticket, respuesta["message_body"])
        self.assertEqual(respuesta["fuente"], "accion_crear_reclamo_llm_exito")
        self.assertEqual(respuesta["ticket_id"], mock_ticket_simulado.id)
        mock_crear_ticket.assert_called_once()
        datos_ticket_enviados = mock_crear_ticket.call_args[1]['ticket_data']
        self.assertEqual(datos_ticket_enviados["nombre_vecino"], "Homero Simpson")
        self.assertEqual(datos_ticket_enviados["telefono_vecino"], "+5491122334455")
        self.assertEqual(datos_ticket_enviados["email_vecino"], "homero@example.com")
        mock_enviar_whatsapp.assert_called_once_with("+5491122334455", "Homero Simpson", "12345", "Alumbrado")

    @patch('services.municipios.servicio_tickets.crear_nuevo_ticket')
    @patch('services.municipios.validar_telefono', return_value=True)
    @patch('services.municipios.formatear_telefono_e164', return_value="+1234567890")
    def test_accion_crear_reclamo_sin_descripcion_llm(
        self, mock_formatear_telefono, mock_validar_telefono, mock_crear_ticket
    ):
        datos_llm = {"categoria": "Basura", "ubicacion": "Calle Siempre Viva 742", "usuario": "Test User"}
        context = {"viewer_user_obj": None, "owner_user": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        respuesta = accion_crear_reclamo_municipio(datos_llm, context)
        self.assertEqual(respuesta["fuente"], "accion_crear_reclamo_error_sin_descripcion")
        self.assertIn("No pude entender la descripción", respuesta["message_body"])
        mock_crear_ticket.assert_not_called()

    @patch('services.municipios.servicio_tickets.crear_nuevo_ticket')
    def test_accion_crear_reclamo_sin_ubicacion_llm(self, mock_crear_ticket):
        datos_llm = {"categoria": "Alumbrado", "descripcion": "Luz parpadea mucho", "usuario": "Test User"}
        context = {"viewer_user_obj": None, "owner_user": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        respuesta = accion_crear_reclamo_municipio(datos_llm, context)
        self.assertEqual(respuesta["fuente"], "accion_crear_reclamo_error_sin_ubicacion")
        self.assertIn("No pude entender la ubicación", respuesta["message_body"])
        mock_crear_ticket.assert_not_called()

    @patch('services.municipios.servicio_tickets.crear_nuevo_ticket')
    @patch('services.municipios.validar_telefono')
    @patch('services.municipios.validar_email', return_value=True) # Email del LLM es inválido, pero el del perfil es válido
    @patch('services.municipios.parse_direccion_completa')
    @patch('services.municipios.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.municipios.formatear_telefono_e164')
    def test_accion_crear_reclamo_contacto_llm_invalido_usa_perfil(
        self, mock_formatear_tel, mock_enviar_whatsapp, mock_parse_direccion,
        mock_validar_email_func, mock_validar_telefono_func, mock_crear_ticket
    ):
        mock_ticket_simulado = MagicMock(); mock_ticket_simulado.nro_ticket = "67890"; mock_ticket_simulado.id = 2
        mock_crear_ticket.return_value = mock_ticket_simulado
        mock_parse_direccion.return_value = {"calle": "Avenida Falsa", "numero": "456", "localidad": "Testville"}

        # Teléfono del LLM inválido, teléfono del perfil válido
        mock_validar_telefono_func.side_effect = [False, True]
        mock_formatear_tel.return_value = "+549876543210" # Formato del teléfono del perfil

        datos_llm = {
            "categoria": "Varios", "descripcion": "Problema general", "ubicacion": "Avenida Falsa 456",
            "usuario": "Usuario LLM", "telefono": "tel_invalido_llm", "email": "email_invalido_llm@llm.bad"
        }

        mock_viewer_user = MagicMock(spec=User)
        mock_viewer_user.id = 200; mock_viewer_user.nombre = "Usuario Perfil Valido"
        mock_viewer_user.telefono = "9876543210"; mock_viewer_user.email = "perfil_valido@example.com"

        context = {
            "viewer_user_obj": mock_viewer_user, "owner_user": MagicMock(id=1, municipio_id="testmuni"),
            "anon_id": None, "municipio_config_actual": {}
        }
        respuesta = accion_crear_reclamo_municipio(datos_llm, context)

        self.assertEqual(respuesta["fuente"], "accion_crear_reclamo_llm_exito")
        mock_crear_ticket.assert_called_once()
        datos_ticket_enviados = mock_crear_ticket.call_args[1]['ticket_data']

        self.assertEqual(datos_ticket_enviados["nombre_vecino"], "Usuario LLM")
        self.assertEqual(datos_ticket_enviados["telefono_vecino"], "+549876543210") # Tomado y formateado del perfil
        self.assertEqual(datos_ticket_enviados["email_vecino"], "perfil_valido@example.com") # Tomado del perfil (mock_validar_email siempre True)

        mock_enviar_whatsapp.assert_called_once_with(
            "+549876543210", "Usuario LLM", "67890", "Varios"
        )

if __name__ == '__main__':
    unittest.main(verbosity=2)
