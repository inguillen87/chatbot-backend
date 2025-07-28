import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from services.actions.municipio_actions import CrearReclamoActionHandler
from models import User
from config import Config

class TestConfigAll(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get('TEST_DATABASE_URL', 'sqlite:///:memory:')
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    CELERY_TASK_ALWAYS_EAGER = True
    DEBUG = False

class TestAccionesMunicipio(unittest.TestCase):

    def setUp(self):
        """Set up for each test."""
        self.app = create_app(config_class=TestConfigAll)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.session = db.session

    def tearDown(self):
        """Tear down after each test."""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono')
    @patch('services.actions.municipio_actions.validar_email')
    @patch('services.actions.municipio_actions.parse_direccion_completa')
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.actions.municipio_actions.formatear_telefono_e164')
    def test_accion_crear_reclamo_exito_completo_llm(
        self, mock_formatear_tel, mock_enviar_whatsapp, mock_parse_direccion,
        mock_validar_email, mock_validar_telefono, mock_crear_ticket
    ):
        mock_ticket_simulado = MagicMock()
        mock_ticket_simulado.nro_ticket = "12345"
        mock_ticket_simulado.id = 1
        mock_crear_ticket.return_value = mock_ticket_simulado

        mock_validar_telefono.return_value = True
        mock_formatear_tel.return_value = "+5491122334455"
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
            "viewer_user_obj": mock_viewer_user, "user_obj": mock_owner_user, "anon_id": None,
            "municipio_config_actual": {"ejemplo_direccion": "Av. Siempreviva 742"},
            "chat_session_uuid": "test-session-uuid-123",
            "chat_db_context_data": {"processed_idempotency_keys": {}}
        }

        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])
        self.assertIn("message_to_user", respuesta)
        self.assertIn(mock_ticket_simulado.nro_ticket, respuesta["message_to_user"])
        self.assertEqual(respuesta["data"]["ticket_id"], mock_ticket_simulado.id)
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args
        self.assertEqual(kwargs['ticket_data']['nombre_vecino'], "Homero Simpson")
        self.assertEqual(kwargs['ticket_data']['telefono_vecino'], "+5491122334455")
        self.assertEqual(kwargs['ticket_data']['email_vecino'], "homero@example.com")
        mock_enviar_whatsapp.assert_called_once_with("+5491122334455", "Homero Simpson", "12345", "Alumbrado")

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value="+1234567890")
    def test_accion_crear_reclamo_sin_descripcion_llm(
        self, mock_formatear_telefono, mock_validar_telefono, mock_crear_ticket
    ):
        datos_llm = {"categoria": "Basura", "ubicacion": "Calle Siempre Viva 742", "usuario": "Test User"}
        context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)
        self.assertFalse(respuesta["success"])
        self.assertIn("Para registrar tu reclamo, necesito que me indiques: una descripción del problema, tu número de teléfono y tu correo electrónico.", respuesta["message_to_user"])
        mock_crear_ticket.assert_not_called()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_accion_crear_reclamo_sin_ubicacion_llm(self, mock_crear_ticket):
        datos_llm = {"categoria": "Alumbrado", "descripcion": "Luz parpadea mucho", "usuario": "Test User"}
        context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)
        self.assertFalse(respuesta["success"])
        self.assertIn("Para registrar tu reclamo, necesito que me indiques: la ubicación del problema, tu número de teléfono y tu correo electrónico.", respuesta["message_to_user"])
        mock_crear_ticket.assert_not_called()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono')
    @patch('services.actions.municipio_actions.validar_email')
    @patch('services.actions.municipio_actions.parse_direccion_completa')
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.actions.municipio_actions.formatear_telefono_e164')
    def test_accion_crear_reclamo_contacto_llm_invalido_usa_perfil(
        self, mock_formatear_tel, mock_enviar_whatsapp, mock_parse_direccion,
        mock_validar_email_func, mock_validar_telefono_func, mock_crear_ticket
    ):
        mock_ticket_simulado = MagicMock(); mock_ticket_simulado.nro_ticket = "67890"; mock_ticket_simulado.id = 2
        mock_crear_ticket.return_value = mock_ticket_simulado
        mock_parse_direccion.return_value = {"calle": "Avenida Falsa", "numero": "456", "localidad": "Testville"}

        # Teléfono del LLM inválido, teléfono del perfil válido
        mock_validar_telefono_func.side_effect = [False, True]
        mock_validar_email_func.side_effect = [False, True]
        mock_formatear_tel.return_value = "+549876543210" # Formato del teléfono del perfil

        datos_llm = {
            "categoria": "Varios", "descripcion": "Problema general", "ubicacion": "Avenida Falsa 456",
            "usuario": "Usuario LLM", "telefono": "tel_invalido_llm", "email": "email_invalido_llm@llm.bad"
        }

        mock_viewer_user = MagicMock(spec=User)
        mock_viewer_user.id = 200; mock_viewer_user.nombre = "Usuario Perfil Valido"
        mock_viewer_user.telefono = "9876543210"; mock_viewer_user.email = "perfil_valido@example.com"

        context = {
            "viewer_user_obj": mock_viewer_user, "user_obj": MagicMock(id=1, municipio_id="testmuni"),
            "anon_id": None, "municipio_config_actual": {},
            "current_user": mock_viewer_user
        }
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args

        self.assertEqual(kwargs['ticket_data']['nombre_vecino'], "Usuario LLM")
        self.assertEqual(kwargs['ticket_data']['telefono_vecino'], "+549876543210") # Tomado y formateado del perfil
        self.assertEqual(kwargs['ticket_data']['email_vecino'], "perfil_valido@example.com") # Tomado del perfil (mock_validar_email siempre True)

        mock_enviar_whatsapp.assert_called_once_with(
            "+549876543210", "Usuario LLM", "67890", "Varios"
        )

if __name__ == '__main__':
    unittest.main(verbosity=2)
