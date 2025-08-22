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
from services.herramientas_municipio import direccion_es_valida
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
        # The call is positional, so the assertion should be positional
        mock_enviar_whatsapp.assert_called_once_with(
            "+5491122334455", "Homero Simpson", "12345", "Alumbrado"
        )

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
        mock_ticket_simulado = MagicMock()
        mock_ticket_simulado.nro_ticket = "55555"
        mock_ticket_simulado.id = 5
        mock_crear_ticket.return_value = mock_ticket_simulado

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
            "nombre_usuario_detectado": "Vecino Detectado"
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
        self.assertIn("necesito algunos datos más: **descripcion, email, telefono**", respuesta["message_to_user"])
        mock_crear_ticket.assert_not_called()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    def test_accion_crear_reclamo_sin_ubicacion_llm(self, mock_crear_ticket):
        datos_llm = {"categoria": "Alumbrado", "descripcion": "Luz parpadea mucho", "usuario": "Test User"}
        context = {"viewer_user_obj": None, "user_obj": MagicMock(id=1, municipio_id="testmuni"), "anon_id": "testanon"}
        handler = CrearReclamoActionHandler(context)
        respuesta = handler.execute(datos_llm)
        self.assertFalse(respuesta["success"])
        # The new logic correctly identifies the user's name from the "usuario" field
        self.assertIn("necesito algunos datos más: **email, telefono, ubicacion**", respuesta["message_to_user"])
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
        mock_ticket_simulado = MagicMock(); mock_ticket_simulado.nro_ticket = "67890"; mock_ticket_simulado.id = 2
        mock_crear_ticket.return_value = mock_ticket_simulado
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
            "usuario": "Usuario LLM", "telefono": "tel_invalido_llm", "email": "email_invalido_llm@llm.bad"
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

        # The call is positional, so the assertion should be positional
        mock_enviar_whatsapp.assert_called_once_with(
            "+549876543210", "Usuario LLM", "67890", "Varios"
        )

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
            "email": None
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

    @patch('services.herramientas_municipio.geocode_address')
    def test_direccion_es_valida(self, mock_geocode):
        # Setea el valor de retorno simulado
        mock_geocode.return_value = {'lat': -32.8895, 'lng': -68.8458}  # Coordenadas de Mendoza

        # El resto de tu test usa la función mockeada
        resultado = direccion_es_valida("don bosco 55 esquina sarmiento junin mendoza")
        self.assertTrue(resultado)

    @patch('services.municipio_responder.google_search')
    def test_news_handler(self, mock_google_search):
        mock_google_search.return_value = [
            {"title": "Test News 1", "link": "http://example.com/news1"},
            {"title": "Test News 2", "link": "http://example.com/news2"},
        ]

        from services.municipio_responder import responder_municipio
        with self.app.test_request_context():
            owner_user = MagicMock(spec=User, id=1, municipio_id='test_muni')
            owner_user.rubro = MagicMock(clave='municipio')
            chat_context = MagicMock()
            chat_context.context_data = {}

            response = responder_municipio(
                pregunta_original={"action": "ultimas_novedades"},
                owner_user=owner_user,
                viewer_user=None,
                anon_id="test_anon_123",
                chat_db_context=chat_context,
                rubro_obj=owner_user.rubro
            )

        self.assertIn("Aquí están las últimas noticias", response["message_body"])
        self.assertIn("Test News 1", response["options_list"][0]['texto'])
        self.assertEqual(response["fuente"], "news_handler_with_results")

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

if __name__ == '__main__':
    unittest.main(verbosity=2)
