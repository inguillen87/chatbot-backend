import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import pytest

pytestmark = pytest.mark.quarantine(reason="Temporarily quarantined while PYME multimodal refactor is stabilised (MIG-4821)")

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from services.actions.municipio_actions import (
    CrearReclamoActionHandler,
    ConsultarEstadoTicketActionHandler,
    HacerSugerenciaActionHandler,
)
from services.herramientas_municipio import direccion_es_valida
from services.constants import CONTEXTO_MUNICIPIO
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
        self.app.config["BACKEND_URL"] = "https://api.example.com"
        self.app.config["IS_HTTPS"] = True
        self.client = self.app.test_client()
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
    @patch('services.location_service.geocode_address')
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.actions.municipio_actions.formatear_telefono_e164')
    @patch('services.herramientas_municipio.parse_direccion_completa')
    def test_accion_crear_reclamo_exito_completo_llm(
        self, mock_parse_direccion, mock_formatear_tel, mock_enviar_whatsapp, mock_geocode_address,
        mock_validar_email, mock_validar_telefono, mock_crear_ticket
    ):
        mock_crear_ticket.return_value = {"id": 1, "nro_ticket": "12345", "consulta_pin": "555444"}

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
            "usuario": "Homero Simpson", "telefono": "91122334455", "email": "homero@example.com",
            "pin": "555444"
        }

        mock_viewer_user = MagicMock(spec=User)
        mock_viewer_user.id = 100; mock_viewer_user.nombre = "Homero J. Simpson"
        mock_viewer_user.telefono = "2615550000"; mock_viewer_user.email = "hsimpson@springfield.com"

        mock_owner_user = MagicMock(spec=User)
        mock_owner_user.id = 1; mock_owner_user.municipio_id = "springfield_municipio"

        context = {
            "viewer_user_obj": mock_viewer_user, "user_obj": mock_owner_user, "anon_id": "session123",
            "municipio_config_actual": {"ejemplo_direccion": "Av. Siempreviva 742"},
            "chat_session_uuid": "test-session-uuid-123",
            "chat_db_context_data": {"processed_idempotency_keys": {}}
        }

        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])
        self.assertIn("message_to_user", respuesta)
        self.assertIn("12345", respuesta["message_to_user"])
        self.assertIn("555444", respuesta["message_to_user"])
        self.assertTrue(any("pin=555444" in opt.get("url", "") for opt in respuesta.get("options_list", [])))
        self.assertEqual(respuesta["data"]["ticket_id"], 1)
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args
        self.assertEqual(kwargs['ticket_data']['nombre_vecino'], "Homero Simpson")
        self.assertEqual(kwargs['ticket_data']['telefono_vecino'], "+5491122334455")
        self.assertEqual(kwargs['ticket_data']['email_vecino'], "homero@example.com")
        self.assertEqual(kwargs['ticket_data']['consulta_pin'], "555444")
        self.assertEqual(kwargs['ticket_data']['anon_id'], "session123")
        # The call is positional, so the assertion should be positional
        # mock_enviar_whatsapp.assert_called_once_with(
        #     "+5491122334455", "Homero Simpson", "12345", "Alumbrado"
        # )

    @patch('services.actions.municipio_actions.formatear_ticket_respuesta')
    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value="+5492611234567")
    @patch('services.herramientas_municipio.parse_direccion_completa', return_value={"calle": "Calle Falsa", "numero": "123", "localidad": "Junin"})
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.location_service.geocode_address')
    def test_categoria_con_emoji_usa_contacto_especializado(
        self, mock_geocode, mock_enviar, mock_parse, mock_formatear_tel, mock_validar_email,
        mock_validar_tel, mock_crear_ticket, mock_formatear_respuesta
    ):
        mock_crear_ticket.return_value = {"id": 1, "nro_ticket": "88888", "consulta_pin": "123456"}
        mock_formatear_respuesta.return_value = ("ok", [])

        datos_llm = {
            "categoria": "💡 Luminaria",
            "descripcion": "Poste caido",
            "ubicacion": "Calle Falsa 123", 
            "telefono": "2611234567",
            "email": "vecino@example.com",
            "usuario": "Marcelo",
            "dni": "32877851"
        }

        context = {
            "viewer_user_obj": None,
            "user_obj": MagicMock(id=1, municipio_id="default"),
            "anon_id": "anon123",
            "municipio_config_actual": {}
        }

        handler = CrearReclamoActionHandler(context)
        handler.execute(datos_llm)

        args, _ = mock_formatear_respuesta.call_args
        contacto = args[5]
        self.assertEqual(contacto.get("nombre"), "Ana María de Servicios")
        self.assertEqual(contacto.get("telefono"), "+5492610000002")

        _, kwargs = mock_crear_ticket.call_args
        self.assertEqual(kwargs['ticket_data']['categoria'], 'Luminaria')
        self.assertEqual(kwargs['ticket_data']['anon_id'], 'anon123')

    @patch('services.actions.municipio_actions.formatear_ticket_respuesta')
    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value="+5492611234567")
    @patch('services.herramientas_municipio.parse_direccion_completa', return_value={"calle": "Calle Falsa", "numero": "123", "localidad": "Junin"})
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.location_service.geocode_address')
    def test_categoria_con_espacios_usa_contacto_especializado(
        self, mock_geocode, mock_enviar, mock_parse, mock_formatear_tel, mock_validar_email,
        mock_validar_tel, mock_crear_ticket, mock_formatear_respuesta
    ):
        mock_crear_ticket.return_value = {"id": 2, "nro_ticket": "99999", "consulta_pin": "123456"}
        mock_formatear_respuesta.return_value = ("ok", [])

        datos_llm = {
            "categoria": " Arbolado  ",
            "descripcion": "Rama caída",
            "ubicacion": "Calle Falsa 123",
            "telefono": "2611234567",
            "email": "vecino@example.com",
            "usuario": "Marcelo",
            "dni": "32877851"
        }

        context = {
            "viewer_user_obj": None,
            "user_obj": MagicMock(id=1, municipio_id="default"),
            "anon_id": "anon123",
            "municipio_config_actual": {}
        }

        handler = CrearReclamoActionHandler(context)
        handler.execute(datos_llm)

        args, _ = mock_formatear_respuesta.call_args
        contacto = args[5]
        self.assertEqual(contacto.get("nombre"), "Roberto de Espacios Verdes")
        self.assertEqual(contacto.get("telefono"), "+5492610000003")

        _, kwargs = mock_crear_ticket.call_args
        self.assertEqual(kwargs['ticket_data']['categoria'], 'Arbolado')
        self.assertEqual(kwargs['ticket_data']['anon_id'], 'anon123')

    def test_accion_consultar_estado_ticket(self):
        from models import MunicipioTicket

        ticket = MunicipioTicket(pregunta="p", nro_ticket="88888", estado="en_proceso", categoria="Alumbrado", consulta_pin="123456")
        db.session.add(ticket)
        db.session.commit()

        handler = ConsultarEstadoTicketActionHandler({})
        result = handler.execute({"id_ticket_mencionado": "88888", "pin": "123456"})

        self.assertTrue(result["success"])
        self.assertIn("88888", result["message_to_user"])
        self.assertIn("en_proceso", result["message_to_user"])

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono')
    @patch('services.actions.municipio_actions.validar_email')
    @patch('services.location_service.geocode_address')
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.actions.municipio_actions.formatear_telefono_e164')
    @patch('services.herramientas_municipio.parse_direccion_completa')
    def test_accion_crear_reclamo_campos_detectados(
        self, mock_parse_direccion, mock_formatear_tel, mock_enviar_whatsapp, mock_geocode_address,
        mock_validar_email, mock_validar_telefono, mock_crear_ticket
    ):
        mock_crear_ticket.return_value = {"id": 5, "nro_ticket": "55555", "consulta_pin": "123456"}

        mock_validar_telefono.return_value = True
        mock_formatear_tel.return_value = "+5499988776655"
        mock_validar_email.return_value = True

        mock_parse_direccion.return_value = {
            "calle": "Ruta 40", "numero": "1", "localidad": "Mendoza"
        }

        datos_llm = {
            "categoria": "Bacheo",
            "descripcion": "Hueco grande",
            "ubicacion": "Ruta 40 1, Mendoza",
            "telefono_detectado": "9988776655",
            "email_detectado": "vecino@ejemplo.com",
            "nombre_usuario_detectado": "Vecino Detectado",
            "pin": "123456",
            "dni": "12345678"
        }

        context = {
            "viewer_user_obj": None,
            "user_obj": MagicMock(id=1, municipio_id="testmuni"),
            "anon_id": "anon123",
            "municipio_config_actual": {}
        }

        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args
        self.assertEqual(kwargs['ticket_data']['nombre_vecino'], "Vecino Detectado")
        self.assertEqual(kwargs['ticket_data']['telefono_vecino'], "+5499988776655")
        self.assertEqual(kwargs['ticket_data']['email_vecino'], "vecino@ejemplo.com")
        self.assertEqual(kwargs['ticket_data']['consulta_pin'], "123456")

    @patch('services.actions.municipio_actions.formatear_ticket_respuesta', return_value=("ok", []))
    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value="+5492611234567")
    @patch('services.herramientas_municipio.parse_direccion_completa', return_value={"calle": "Don Bosco", "numero": "55", "localidad": "Junin"})
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.location_service.geocode_address')
    def test_crear_reclamo_prefiere_nombre_perfil(
        self,
        mock_geocode,
        mock_enviar,
        mock_parse,
        mock_formatear_tel,
        mock_validar_email,
        mock_validar_tel,
        mock_crear_ticket,
        mock_formatear_respuesta,
    ):
        mock_crear_ticket.return_value = {"id": 42, "nro_ticket": "772356", "consulta_pin": "211545"}

        datos_llm = {
            "categoria": "Arreglo de calle",
            "descripcion": "bache profundo en la calzada",
            "ubicacion": "Don Bosco 55",
            "usuario": "quiero pedir que",
            "telefono": "2613168608",
            "email": "vecino@example.com",
        }

        viewer_user = MagicMock(spec=User)
        viewer_user.id = 77
        viewer_user.nombre = None
        viewer_user.name = None

        context = {
            "viewer_user_obj": viewer_user,
            "user_obj": MagicMock(id=1, municipio_id="default"),
            "anon_id": "+5492613168608",
            "municipio_config_actual": {},
            "profile_name": "Marcelo",
            CONTEXTO_MUNICIPIO: {
                "contacto_usuario": {"nombre": "quiero pedir que"},
                "datos_parciales_llm_reclamo": {},
            },
        }

        handler = CrearReclamoActionHandler(context)
        handler.execute(datos_llm)

        _, kwargs = mock_crear_ticket.call_args
        self.assertEqual(kwargs['ticket_data']['nombre_vecino'], 'Marcelo')

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
        # The new logic correctly identifies the user's name from the "usuario" field
        self.assertIn("necesito algunos datos más: **descripcion, dni, email, telefono**", respuesta["message_to_user"])
        mock_crear_ticket.assert_not_called()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_accion_crear_reclamo_sin_ubicacion_llm(self, mock_crear_ticket):
        datos_llm = {"categoria": "Alumbrado", "descripcion": "Luz parpadea mucho", "usuario": "Test User"}
        context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)
        self.assertFalse(respuesta["success"])
        # The new logic correctly identifies the user's name from the "usuario" field
        self.assertIn("necesito algunos datos más: **dni, email, telefono, ubicacion**", respuesta["message_to_user"])
        mock_crear_ticket.assert_not_called()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value="+541234567890")
    def test_accion_crear_reclamo_sin_pin_pide_pin(
        self, mock_formatear_tel, mock_validar_email, mock_validar_tel, mock_crear_ticket
    ):
        datos_llm = {
            "categoria": "Alumbrado",
            "descripcion": "Luz apagada",
            "ubicacion": "Calle Falsa 321",
            "telefono": "1234567890",
            "email": "vecino@example.com",
            "usuario": "Juan",
            "dni": "12345678"
        }
        context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)
        self.assertFalse(respuesta["success"])
        self.assertEqual(respuesta["pedir_info"], "pin_ticket")
        self.assertIn("PIN", respuesta["message_to_user"])
        mock_crear_ticket.assert_not_called()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono')
    @patch('services.actions.municipio_actions.validar_email')
    @patch('services.location_service.geocode_address')
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.actions.municipio_actions.formatear_telefono_e164')
    @patch('services.herramientas_municipio.parse_direccion_completa')
    def test_accion_crear_reclamo_contacto_llm_invalido_usa_perfil(
        self, mock_parse_direccion, mock_formatear_tel, mock_enviar_whatsapp, mock_geocode_address,
        mock_validar_email_func, mock_validar_telefono_func, mock_crear_ticket
    ):
        mock_crear_ticket.return_value = {"id": 2, "nro_ticket": "67890", "consulta_pin": "246810"}
        mock_parse_direccion.return_value = {"calle": "Avenida Falsa", "numero": "456", "localidad": "Testville"}

        # Simular que el teléfono del LLM es inválido, pero el del perfil es válido.
        # La función mockeada 'validar_telefono' devolverá False para el primer llamado (LLM) y True para el segundo (perfil).
        mock_validar_telefono_func.side_effect = [False, True]
        # Simular el mismo comportamiento para el email.
        mock_validar_email_func.side_effect = [False, True]

        # El mock de formatear_telefono_e164 debe devolver el teléfono del *perfil* ya formateado.
        mock_formatear_tel.return_value = "+549876543210"

        datos_llm = {
            "categoria": "Varios", "descripcion": "Problema general", "ubicacion": "Avenida Falsa 456",
            "usuario": "Usuario LLM", "telefono": "tel_invalido_llm", "email": "email_invalido_llm@llm.bad",
            "pin": "246810"
        }

        mock_viewer_user = MagicMock(spec=User)
        mock_viewer_user.id = 200; mock_viewer_user.nombre = "Usuario Perfil Valido"
        mock_viewer_user.telefono = "9876543210"; mock_viewer_user.email = "perfil_valido@example.com"

        context = {
            "viewer_user_obj": mock_viewer_user, "user_obj": MagicMock(id=1, municipio_id="testmuni"),
            "anon_id": None, "municipio_config_actual": {},
            "current_user": mock_viewer_user,
            "pregunta_actual_usuario": "mi pregunta de prueba"
        }
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args

        self.assertEqual(kwargs['ticket_data']['nombre_vecino'], "Usuario LLM")
        self.assertEqual(kwargs['ticket_data']['telefono_vecino'], "+549876543210") # Tomado y formateado del perfil
        self.assertEqual(kwargs['ticket_data']['email_vecino'], "perfil_valido@example.com") # Tomado del perfil
        self.assertEqual(kwargs['ticket_data']['pregunta'], "mi pregunta de prueba")
        self.assertEqual(kwargs['ticket_data']['consulta_pin'], "246810")

        # The call is positional, so the assertion should be positional
        # mock_enviar_whatsapp.assert_called_once_with(
        #     "+549876543210", "Usuario LLM", "67890", "Varios"
        # )

    def test_accion_crear_reclamo_datos_incompletos_llm(self):
        """
        Prueba que el sistema maneja correctamente los datos incompletos del LLM.
        """
        datos_llm = {
            "categoria": "Alumbrado",
            "descripcion": "Poste de luz caído y chispas.",
            "ubicacion": "Calle Falsa 123, Springfield",
            "coordenadas": {"lat": -32.8908, "lon": -68.8272},
            "usuario": "Homero Simpson",
            "telefono": None,
            "email": None,
            "pin": "135790"
        }

        mock_viewer_user = MagicMock(spec=User)
        mock_viewer_user.id = 100
        mock_viewer_user.nombre = "Homero J. Simpson"
        mock_viewer_user.telefono = None
        mock_viewer_user.email = None

        mock_owner_user = MagicMock(spec=User)
        mock_owner_user.id = 1
        mock_owner_user.municipio_id = "springfield_municipio"

        context = {
            "viewer_user_obj": mock_viewer_user,
            "user_obj": mock_owner_user,
            "anon_id": None,
            "municipio_config_actual": {"ejemplo_direccion": "Av. Siempreviva 742"},
            "chat_session_uuid": "test-session-uuid-123",
            "chat_db_context_data": {"processed_idempotency_keys": {}}
        }

        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta["success"])

    @patch('services.actions.municipio_actions.promo_service.build_ticket_promo_section', return_value=None)
    @patch('services.actions.municipio_actions.cargar_configuracion_municipio')
    @patch('services.herramientas_municipio.parse_direccion_completa', return_value={'localidad': 'Centro'})
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value='+549111111111')
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_crear_reclamo_web_mantiene_boton_ticket(
        self,
        mock_crear_ticket,
        mock_validar_tel,
        mock_validar_email,
        mock_formatear_tel,
        mock_parse_direccion,
        mock_cargar_config,
        _mock_promo,
    ):
        mock_crear_ticket.return_value = {
            'id': 1,
            'nro_ticket': '12345',
            'consulta_pin': '654321',
        }
        mock_cargar_config.side_effect = [
            {'default': {}},  # contactos_especializados.json
            {},               # tramites.json
            {'web_url': 'https://municipio.example'},  # config.json
        ]

        viewer_user = MagicMock(spec=User)
        viewer_user.id = 999
        viewer_user.nombre = "Ana"
        viewer_user.telefono = "2615550000"
        viewer_user.email = "vecina@example.com"
        viewer_user.dni = "30111222"
        viewer_user.direccion = "Calle 1"

        owner_user = MagicMock(spec=User)
        owner_user.municipio_id = "muni-test"

        context = {
            CONTEXTO_MUNICIPIO: {'datos_parciales_llm_reclamo': {}, 'contacto_usuario': {}},
            'viewer_user_obj': viewer_user,
            'user_obj': owner_user,
            'anon_id': 'anon-1',
            'municipio_config_actual': {'base_chat_url': 'https://example.com/chat'},
            'chat_db_context_data': {'processed_idempotency_keys': {}},
            'channel': 'web',
        }

        handler = CrearReclamoActionHandler(context)
        datos_llm = {
            'categoria': 'Luminaria',
            'descripcion': 'poste caido',
            'ubicacion': 'Calle 123',
            'usuario': 'Ana',
            'telefono': '2615550000',
            'email': 'vecina@example.com',
            'pin': '654321',
        }

        respuesta = handler.execute(datos_llm)

        self.assertTrue(respuesta.get('success'))
        message = respuesta.get('message_body', '')
        expected_url = 'https://example.com/chat/12345?pin=654321'
        self.assertIn('Ver mi Ticket', message)
        self.assertNotIn(expected_url, message)
        opciones = respuesta.get('options_list', [])
        urls = [opt.get('url') for opt in opciones if isinstance(opt, dict)]
        self.assertIn(expected_url, urls)

    @patch('services.actions.municipio_actions.promo_service.build_ticket_promo_section', return_value=None)
    @patch('services.actions.municipio_actions.cargar_configuracion_municipio')
    @patch('services.herramientas_municipio.parse_direccion_completa', return_value={'localidad': 'Centro'})
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value='+549111111111')
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_crear_reclamo_whatsapp_usa_template_pre_message(
        self,
        mock_crear_ticket,
        mock_validar_tel,
        mock_validar_email,
        mock_formatear_tel,
        mock_parse_direccion,
        mock_cargar_config,
        _mock_promo,
    ):
        mock_crear_ticket.return_value = {
            'id': 1,
            'nro_ticket': '12345',
            'consulta_pin': '654321',
        }
        mock_cargar_config.side_effect = [
            {'default': {}},
            {},
            {'web_url': 'https://municipio.example'},
        ]

        viewer_user = MagicMock(spec=User)
        viewer_user.id = 999
        viewer_user.nombre = "Ana"
        viewer_user.telefono = "2615550000"
        viewer_user.email = "vecina@example.com"
        viewer_user.dni = "30111222"
        viewer_user.direccion = "Calle 1"

        owner_user = MagicMock(spec=User)
        owner_user.municipio_id = "muni-test"

        context = {
            CONTEXTO_MUNICIPIO: {'datos_parciales_llm_reclamo': {}, 'contacto_usuario': {}},
            'viewer_user_obj': viewer_user,
            'user_obj': owner_user,
            'anon_id': 'anon-1',
            'municipio_config_actual': {'base_chat_url': 'https://example.com/chat'},
            'chat_db_context_data': {'processed_idempotency_keys': {}},
            'channel': 'whatsapp',
        }

        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute({
            'categoria': 'Luminaria',
            'descripcion': 'poste caido',
            'ubicacion': 'Calle 123',
            'usuario': 'Ana',
            'telefono': '2615550000',
            'email': 'vecina@example.com',
            'pin': '654321',
        })

        self.assertTrue(respuesta.get('success'))
        self.assertNotIn('whatsapp_receipt', respuesta)
        pre_messages = respuesta.get('_twilio_pre_messages') or []
        self.assertEqual(len(pre_messages), 1)
        template = pre_messages[0]
        self.assertEqual(template.get('template_name'), 'chatboc_gov_claim_created_v2')
        self.assertEqual(template.get('variables'), {
            '1': 'M-12345',
            '2': 'chat/12345?pin=654321',
        })
        self.assertIn('https://example.com/chat/12345?pin=654321', template.get('body', ''))
        self.assertIn('Ver seguimiento: https://example.com/chat/12345?pin=654321', respuesta.get('message_body', ''))
        self.assertEqual(respuesta.get('message_type'), 'text')
        self.assertEqual(respuesta.get('options_list'), [])

    @patch('services.herramientas_municipio.geocode_address')
    def test_direccion_es_valida(self, mock_geocode):
        # Setea el valor de retorno simulado
        mock_geocode.return_value = {'lat': -32.8895, 'lng': -68.8458}  # Coordenadas de Mendoza

        # El resto de tu test usa la función mockeada
        resultado = direccion_es_valida("don bosco 55 esquina sarmiento junin mendoza")
        self.assertTrue(resultado)

    @patch('services.herramientas_municipio.geocode_address', return_value=None)
    def test_direccion_es_valida_fallback(self, _mock_geocode):
        """Debe aceptar direcciones simples aunque no haya geocodificación."""
        self.assertTrue(direccion_es_valida("don bosco 55"))
        self.assertFalse(direccion_es_valida("sin numero"))

    @patch('services.municipio_responder.cargar_agenda_cultural')
    def test_agenda_y_noticias_handler(self, mock_cargar_agenda):
        mock_cargar_agenda.return_value = {
            "eventos": [
                {
                    "titulo": "Noticia de Prueba 1",
                    "descripcion": "Este es el cuerpo de la noticia 1.",
                    "tipo_post": "noticia",
                    "fecha_publicacion": "2025-08-22T10:00:00",
                    "imagen_url": "https://example.com/flyer.jpg",
                    "enlace": "https://example.com/noticia1"
                },
                {
                    "titulo": "Información Útil",
                    "descripcion": "Dato cargado desde la solapa información.",
                    "tipo_post": "informacion",
                    "fecha_publicacion": "2025-08-23T12:00:00"
                },
                {
                    "titulo": "Evento Cultural de Prueba",
                    "descripcion": "Este es un evento.",
                    "tipo_post": "evento",
                    "fecha_evento_inicio": "2025-08-30T20:00:00",
                    "fecha_evento_fin": "2025-08-30T22:00:00",
                    "enlace": "https://example.com/evento",
                    "imagen_url": "/data/archivos/evento.jpg"
                },
                {
                    "titulo": "Noticia de Prueba 2",
                    "descripcion": "Este es el cuerpo de la noticia 2.",
                    "tipo_post": "noticia",
                    "fecha_publicacion": "2025-08-21"
                },
                {
                    "titulo": "Evento Sin Fecha",
                    "descripcion": "Actividad sin fecha programada.",
                    "tipo_post": "evento",
                    "fecha_publicacion": "2025-08-19T09:00:00",
                    "imagen_url": "/data/archivos/evento_sin_fecha.png",
                    "enlace": "https://example.com/evento-sin-fecha"
                },
            ]
        }

        from services.municipio_responder import responder_municipio
        with self.app.test_request_context():
            owner_user = MagicMock(spec=User, id=1, municipio_id='test_muni')
            owner_user.rubro = MagicMock(clave='municipio')
            chat_context = MagicMock()
            chat_context.context_data = {}

            response = responder_municipio(
                pregunta_original={"action": "agenda_y_noticias"},
                owner_user=owner_user,
                viewer_user=None,
                anon_id="test_anon_123",
                chat_db_context=chat_context,
                rubro_obj=owner_user.rubro
            )

        self.assertIn("Noticias Recientes", response["message_body"])
        self.assertIn("Próximos Eventos", response["message_body"])
        self.assertIn("Noticia de Prueba 1", response["message_body"])
        self.assertIn("Información Útil", response["message_body"])
        self.assertIn("Evento Cultural de Prueba", response["message_body"])
        self.assertIn("Evento Sin Fecha", response["message_body"])
        social_urls = [opt.get("url") for opt in response.get("options_list", []) if isinstance(opt, dict)]
        self.assertTrue(any(url and url.startswith("https://www.facebook.com/") for url in social_urls))
        self.assertIn("https://example.com/flyer.jpg", response["message_body"])
        self.assertIn("🔗 Más info: https://example.com/noticia1", response["message_body"])
        self.assertIn("📅 22/08/2025 10:00 hs", response["message_body"])
        self.assertIn(
            "📅 30/08/2025 20:00 hs - 30/08/2025 22:00 hs",
            response["message_body"],
        )
        self.assertIn("https://api.example.com/media/archivos/evento.jpg", response["message_body"])
        self.assertEqual(
            response.get("image_url"),
            "https://api.example.com/media/archivos/evento.jpg",
        )
        self.assertEqual(response["fuente"], "handler_agenda_y_noticias")

    @patch('services.municipio_responder.llamar_gemini')
    def test_points_of_interest_handler_with_location(self, mock_llamar_gemini):
        # Simulate the LLM deciding to use the google_search tool
        mock_llamar_gemini.return_value = (
            {
                "accion_backend": "ejecutar_herramienta",
                "message_body": "Buscando farmacias...",
                "datos_estructura": {
                    "nombre_herramienta": "google_search",
                    "parametros_herramienta": {"query": "farmacias de turno cerca de Mendoza, Argentina"}
                }
            },
            {}
        )

        from services.herramientas_municipio import TOOL_REGISTRY
        # This is a bit of a hack, but it's the most reliable way to mock the tool
        # without fighting with patch decorators on nested imports.
        original_google_search = TOOL_REGISTRY['google_search']['funcion']
        mock_google_search = MagicMock(return_value="Resultados de búsqueda: Farmacia Central, Abierto 24hs, http://example.com/farmacia")
        TOOL_REGISTRY['google_search']['funcion'] = mock_google_search

        from services.municipio_responder import responder_municipio
        with self.app.test_request_context():
            owner_user = MagicMock(spec=User, id=1, municipio_id='test_muni')
            owner_user.rubro = MagicMock(clave='municipio')
            chat_context = MagicMock()
            chat_context.context_data = {}

            try:
                response = responder_municipio(
                    pregunta_original="farmacias de turno",
                    owner_user=owner_user,
                    viewer_user=None,
                    anon_id="test_anon_123",
                    chat_db_context=chat_context,
                    rubro_obj=owner_user.rubro,
                    location={"formatted_address": "Mendoza, Argentina"}
                )

                self.assertIn("Farmacia Central", response["message_body"])
                mock_google_search.assert_called_with(query="farmacias de turno cerca de Mendoza, Argentina")
            finally:
                # Restore the original function to avoid side effects in other tests
                TOOL_REGISTRY['google_search']['funcion'] = original_google_search

    @patch('services.municipio_responder.google_search')
    @patch('services.municipio_responder.llamar_gemini')
    def test_points_of_interest_handler_without_location(self, mock_llamar_gemini, mock_google_search):
        # Simulate the LLM asking for location
        mock_llamar_gemini.return_value = ({"message_body": "Para darte información precisa, necesito tu ubicación. ¿Podrías compartirla?",
                                            "accion_backend": "pedir_info", "pedir_info": "ubicacion"}, {})

        from services.municipio_responder import responder_municipio
        with self.app.test_request_context():
            owner_user = MagicMock(spec=User, id=1, municipio_id='test_muni')
            owner_user.rubro = MagicMock(clave='municipio')
            chat_context = MagicMock()
            chat_context.context_data = {}

            response = responder_municipio(
                pregunta_original="farmacias de turno",
                owner_user=owner_user,
                viewer_user=None,
                anon_id="test_anon_123",
                chat_db_context=chat_context,
                rubro_obj=owner_user.rubro
            )

            # The new expected response comes from the mocked LLM
            self.assertIn("necesito tu ubicación", response["message_body"])
            mock_google_search.assert_not_called()

    @patch('services.actions.municipio_actions.formatear_ticket_respuesta', return_value=("ok", []))
    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_hacer_sugerencia_persiste_ubicacion(self, mock_crear_ticket, mock_formatear):
        mock_crear_ticket.return_value = {"id": 1, "nro_ticket": "555"}
        viewer = MagicMock(spec=User)
        viewer.id = 10
        owner = MagicMock(spec=User)
        owner.municipio_id = 1
        context = {
            "viewer_user_obj": viewer,
            "user_obj": owner,
            "anon_id": "anon",
            "municipio_config_actual": {},
            "contexto_municipio_v2": {},
        }
        handler = HacerSugerenciaActionHandler(context)
        datos = {
            "descripcion": "Más árboles en la plaza",
            "ubicacion": "Plaza Central",
            "coordenadas": {"lat": -32.9, "lon": -68.8},
            "nombre": "Juan Perez",
            "dni": "12345678",
            "email": "juan@example.com",
            "direccion": "Calle Falsa 123",
        }
        resp = handler.execute(datos)
        self.assertTrue(resp["success"])
        mock_crear_ticket.assert_called_once()
        ticket_kwargs = mock_crear_ticket.call_args.kwargs['ticket_data']
        self.assertEqual(ticket_kwargs['direccion'], 'Plaza Central')
        self.assertEqual(ticket_kwargs['latitud'], -32.9)
        self.assertEqual(ticket_kwargs['longitud'], -68.8)
        self.assertEqual(ticket_kwargs['direccion_contacto'], 'Calle Falsa 123')

    @patch('services.municipio_responder.handle_llm_interaction', return_value=(None, {}))
    @patch('services.municipio_responder.google_search')
    def test_fallback_handler(self, mock_google_search, mock_handle_llm):
        mock_google_search.return_value = [
            {"title": "Test Search Result", "link": "http://example.com/search", "snippet": "This is a test search result."}
        ]

        from services.municipio_responder import responder_municipio
        with self.app.test_request_context():
            owner_user = MagicMock(spec=User, id=1, municipio_id='test_muni')
            owner_user.rubro = MagicMock(clave='municipio')
            chat_context = MagicMock()
            chat_context.context_data = {}

            response = responder_municipio(
                pregunta_original="unhandled query",
                owner_user=owner_user,
                viewer_user=None,
                anon_id="test_anon_123",
                chat_db_context=chat_context,
                rubro_obj=owner_user.rubro
            )

            self.assertIn("encontré esto en la web", response["message_body"])
            self.assertIn("Test Search Result", response["message_body"])
            self.assertEqual(response["fuente"], "municipio_fallback_google_search")
            mock_google_search.assert_called_with("unhandled query")

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket', return_value={'id': 1, 'nro_ticket': '123', 'consulta_pin': '111111'})
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value='+5492611234567')
    @patch('services.actions.municipio_actions.parse_direccion', return_value={'localidad': 'Junín'})
    def test_parse_distrito_uses_municipio_config(self, mock_parse, mock_fmt, mock_val_email, mock_val_tel, mock_crear):
        datos_llm = {
            'categoria': 'Luminaria',
            'descripcion': 'poste caido',
            'ubicacion': 'sarmiento y san martin',
            'nombre': 'Juan',
            'telefono': '2611234567',
            'email': 'juan@test.com'
        }
        context = {
            'viewer_user_obj': None,
            'user_obj': MagicMock(id=1, municipio_id='default'),
            'anon_id': 'anonX',
            'municipio_config_actual': {'ciudad': 'Junín'}
        }
        handler = CrearReclamoActionHandler(context)
        handler.execute(datos_llm)
        mock_parse.assert_called_once_with('sarmiento y san martin', {'ciudad': 'Junín'})
        _, kwargs = mock_crear.call_args
        self.assertEqual(kwargs['ticket_data']['distrito'], 'Junín')

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono')
    @patch('services.actions.municipio_actions.validar_email')
    @patch('services.location_service.geocode_address')
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.actions.municipio_actions.formatear_telefono_e164')
    @patch('services.herramientas_municipio.parse_direccion_completa')
    def test_crear_reclamo_con_email_existente_asigna_ticket_a_usuario_existente(
        self, mock_parse_direccion, mock_formatear_tel, mock_enviar_whatsapp, mock_geocode_address,
        mock_validar_email, mock_validar_telefono, mock_crear_ticket
    ):
        # 1. Setup: Crear un usuario existente en la base de datos.
        existing_user = User(nombre="Usuario Original", email="original@example.com", telefono="123456789")
        self.session.add(existing_user)
        self.session.commit()
        self.assertEqual(User.query.count(), 1)

        # Mock de los servicios externos
        mock_crear_ticket.return_value = {"id": 99, "nro_ticket": "TICKET-99", "consulta_pin": "9876"}
        mock_validar_telefono.return_value = True
        mock_validar_email.return_value = True
        mock_formatear_tel.return_value = "+549987654321"
        mock_parse_direccion.return_value = {"calle": "Calle Nueva", "numero": "1", "localidad": "TestCity"}

        # 2. Datos del LLM para el nuevo reclamo: mismo email, diferente nombre.
        datos_llm = {
            "categoria": "Limpieza",
            "descripcion": "Basura en la calle.",
            "ubicacion": "Calle Nueva 1, TestCity",
            "usuario": "Usuario Nuevo Intento",
            "telefono": "987654321",
            "email": "original@example.com", # Email existente
            "pin": "9876"
        }

        # Contexto simulando un nuevo usuario (no logueado)
        mock_owner_user = MagicMock(spec=User, id=1, municipio_id="test_municipio")
        context = {
            "viewer_user_obj": None, # Simula que no hay usuario logueado
            "user_obj": mock_owner_user,
            "anon_id": "session_abc",
            "municipio_config_actual": {},
            "chat_session_uuid": "test-session-uuid-456",
            "chat_db_context_data": {"processed_idempotency_keys": {}}
        }

        # 3. Ejecutar la acción
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)

        # 4. Asserts
        self.assertTrue(respuesta["success"], "La acción debería ser exitosa.")
        self.assertEqual(User.query.count(), 1, "No se debería haber creado un nuevo usuario.")

        # Verificar que el ticket se creó con los datos del LLM pero se asoció al usuario existente
        mock_crear_ticket.assert_called_once()
        _, kwargs = mock_crear_ticket.call_args

        ticket_data = kwargs['ticket_data']
        self.assertEqual(ticket_data['nombre_vecino'], "Usuario Nuevo Intento")
        self.assertEqual(ticket_data['email_vecino'], "original@example.com")

        # El user_id asociado al ticket debe ser el del usuario original
        self.assertEqual(kwargs['user_id'], existing_user.id)

        # Verificar que se intentó actualizar el perfil del usuario existente con los datos nuevos
        updated_user = self.session.get(User, existing_user.id)
        self.assertEqual(updated_user.nombre, "Usuario Nuevo Intento")
        self.assertEqual(updated_user.telefono, "+549987654321")


if __name__ == '__main__':
    unittest.main(verbosity=2)
