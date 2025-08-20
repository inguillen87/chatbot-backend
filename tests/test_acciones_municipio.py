import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Añadir el directorio raíz del proyecto al sys.path
project_root_acciones = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_acciones not in sys.path:
    sys.path.insert(0, project_root_acciones)

from app import create_app, db
from config import Config
from models import User, Rubro, ChatSessionContext
from services.actions.municipio_actions import (
    CrearReclamoActionHandler,
    ConsultarEstadoTicketActionHandler,
    ConsultarInfoTramiteActionHandler,
    HacerSugerenciaActionHandler,
    CONTEXTO_MUNICIPIO
)
from services.ticket_service import ServicioTickets

class TestConfigAll(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False

class TestAccionesMunicipio(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfigAll)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Crear un usuario y rubro para las pruebas
        self.rubro = Rubro(clave="municipio", nombre="municipio", es_publico=True)
        self.user = User(name="testmunicipio", email="test@municipio.com", municipio_id=1, rubro=self.rubro)
        self.user.set_password("password")
        db.session.add(self.rubro)
        db.session.add(self.user)
        db.session.commit()

        self.ticket_service = ServicioTickets()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value="+1234567890")
    def test_accion_crear_reclamo_con_datos_completos_anonimo(
        self, mock_formatear_telefono, mock_validar_telefono, mock_crear_ticket
    ):
        mock_ticket = MagicMock()
        mock_ticket.nro_ticket = "T123"
        mock_ticket.id = 1
        mock_crear_ticket.return_value = mock_ticket

        datos_llm = {
            "categoria": "Basura",
            "descripcion": "Mucha basura en la esquina",
            "ubicacion": "Calle Siempre Viva 742",
            "usuario": "Test User",
            "telefono": "1234567890",
            "email": "test@example.com"
        }
        context = {
            "viewer_user_obj": None,
            "user_obj": self.user,
            "anon_id": "testanon",
            "channel": "web"
        }
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])
        self.assertIn("Tu reclamo #T123 ha sido creado", respuesta["message_to_user"])
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args
        call_data = kwargs['ticket_data']
        self.assertEqual(call_data["categoria"], "Basura")
        self.assertEqual(call_data["nombre_vecino"], "Test User")
        self.assertEqual(call_data["telefono_vecino"], "+1234567890")
        self.assertEqual(call_data["email_vecino"], "test@example.com")
        self.assertEqual(call_data["anon_id"], "testanon")

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_accion_crear_reclamo_con_usuario_registrado(self, mock_crear_ticket):
        mock_ticket = MagicMock()
        mock_ticket.nro_ticket = "T124"
        mock_ticket.id = 2
        mock_crear_ticket.return_value = mock_ticket

        viewer_user = User(
            id=99,
            name="Registered User",
            telefono="987654321",
            email="registered@example.com"
        )
        viewer_user.set_password("password")
        db.session.add(viewer_user)
        db.session.commit()

        datos_llm = {
            "categoria": "Alumbrado",
            "descripcion": "Poste de luz no funciona",
            "ubicacion": "Avenida Falsa 123"
        }
        context = {
            "viewer_user_obj": viewer_user,
            "user_obj": self.user,
            "channel": "mobile_app"
        }
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args
        call_data = kwargs['ticket_data']
        self.assertEqual(call_data["nombre_vecino"], "Registered User")
        self.assertEqual(call_data["user_id"], 99)
        self.assertIsNone(call_data.get("anon_id"))

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
        self.assertIn("Para poder registrar tu reclamo, necesito los siguientes datos: **descripcion, email, telefono**", respuesta["message_to_user"])

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_accion_crear_reclamo_sin_ubicacion_llm(self, mock_crear_ticket):
        datos_llm = {"categoria": "Alumbrado", "descripcion": "Luz parpadea mucho", "usuario": "Test User"}
        context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)
        self.assertFalse(respuesta["success"])
        self.assertIn("Para poder registrar tu reclamo, necesito los siguientes datos: **email, telefono, ubicacion**", respuesta["message_to_user"])

    def test_consultar_info_tramite_exitoso(self):
        with patch('services.municipios.obtener_info_tramite_web') as mock_obtener_info:
            mock_obtener_info.return_value = {"contenido": "Info sobre Licencia de Conducir"}
            handler = ConsultarInfoTramiteActionHandler({})
            respuesta = handler.execute({"nombre_tramite": "Licencia de Conducir"})
            self.assertTrue(respuesta["success"])
            self.assertEqual(respuesta["message_to_user"], "Info sobre Licencia de Conducir")

    def test_consultar_info_tramite_sin_nombre(self):
        handler = ConsultarInfoTramiteActionHandler({})
        respuesta = handler.execute({})
        self.assertFalse(respuesta["success"])
        self.assertEqual(respuesta["pedir_info"], "nombre_tramite")

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_hacer_sugerencia_con_descripcion(self, mock_crear_ticket):
        mock_ticket = MagicMock()
        mock_ticket.nro_ticket = "S456"
        mock_ticket.id = 3
        mock_crear_ticket.return_value = mock_ticket

        handler = HacerSugerenciaActionHandler({"viewer_user_obj": None, "user_obj": self.user, "anon_id": "anon-sug"})
        respuesta = handler.execute({"descripcion": "Deberían poner más bancos en la plaza."})

        self.assertTrue(respuesta["success"])
        self.assertIn("Tu sugerencia #S456 ha sido registrada", respuesta["message_to_user"])
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args
        self.assertEqual(kwargs['ticket_data']['categoria'], 'Sugerencia')

    def test_hacer_sugerencia_sin_descripcion(self):
        handler = HacerSugerenciaActionHandler({})
        respuesta = handler.execute({})
        self.assertFalse(respuesta["success"])
        self.assertEqual(respuesta["pedir_info"], "descripcion_sugerencia")

if __name__ == '__main__':
    unittest.main()
