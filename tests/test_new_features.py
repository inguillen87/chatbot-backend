import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Asegúrate de que el directorio raíz del proyecto esté en el sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.ticket_utils import formatear_ticket_respuesta
from services.ticket_utils import _remove_redundant_urls_from_message
from services.municipio_responder import GreetingHandler
from services.municipio_responder import responder_municipio
from config import TestConfig
from app import create_app
from models import db

class TestNewFeatures(unittest.TestCase):

    def setUp(self):
        """Configura un entorno de prueba básico."""
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_formatear_ticket_respuesta_con_contacto_especializado(self):
        """
        Verifica que la respuesta del ticket incluya la información del contacto especializado.
        """
        contacto = {
            "nombre": "Juan Obras",
            "titulo": "Jefe de Bacheo",
            "telefono": "+5491122334455"
        }
        respuesta = formatear_ticket_respuesta(
            "reclamo", "Marcelo", "Bache en la calle", "Bacheo", "M-12345", contacto
        )
        message_body, buttons = respuesta
        self.assertIn("Juan Obras", message_body)
        self.assertIn("Jefe de Bacheo", message_body)
        self.assertIn("+5491122334455", message_body)
        # Se agrega botón de información municipal por defecto
        self.assertTrue(any(b.get("texto") == "🌐 Más información" for b in buttons))

    def test_formatear_ticket_respuesta_incluye_pin_y_url(self):
        message, buttons = formatear_ticket_respuesta(
            "reclamo",
            "Ana",
            "Descripción",
            "Categoria",
            "M-99999",
            contacto_especializado=None,
            base_chat_url="https://example.com/tickets",
            consulta_pin="654321",
        )
        self.assertIn("654321", message)
        self.assertTrue(any("pin=654321" in b.get("url", "") for b in buttons))
        self.assertIn("https://example.com/tickets/99999?pin=654321", message)

    def test_formatear_ticket_respuesta_incluye_contacto(self):
        message, _ = formatear_ticket_respuesta(
            "reclamo",
            "Ana",
            "Descripción",
            "Categoria",
            "M-88888",
            base_chat_url="https://example.com/tickets",
            dni="12345678",
            telefono="+549261000000",
            email="ana@example.com",
        )
        self.assertIn("12345678", message)
        self.assertIn("+549261000000", message)
        self.assertIn("ana@example.com", message)
        self.assertIn("Actualizar datos", message)
        self.assertIn("https://example.com/tickets/88888", message)

    def test_formatear_ticket_respuesta_incluye_links_promocionales(self):
        message, buttons = formatear_ticket_respuesta(
            "reclamo",
            "Ana",
            "Descripción",
            "Categoria",
            "M-77777",
            base_chat_url="https://example.com/tickets",
            consulta_pin="111222",
        )
        self.assertIn("Junín Punto Limpio: https://www.juninmendoza.gov.ar/punto-limpio", message)
        self.assertIn("Obras y novedades: https://www.juninmendoza.gov.ar/obras", message)
        self.assertIn("Más información municipal: https://www.juninmendoza.gov.ar/", message)
        self.assertIn("💬 Ver mi Ticket: https://example.com/tickets/77777?pin=111222", message)
        self.assertTrue(any(b.get("texto") == "💬 Ver mi Ticket" for b in buttons))
        self.assertTrue(any(b.get("texto") == "🌐 Más información" for b in buttons))

    def test_remove_redundant_urls_fallback_to_original(self):
        body = "https://example.com/tickets/1"
        cleaned = _remove_redundant_urls_from_message(body, [
            {"texto": "link", "url": "https://example.com/tickets/1", "type": "url"}
        ])
        self.assertEqual(cleaned, body)

    def test_greeting_handler_final_menu(self):
        """
        Verifica que el GreetingHandler devuelve el menú principal final (v5).
        """
        handler = GreetingHandler(context={
            'profile_name': 'Tester',
            'channel': 'whatsapp',
            'municipio_config_actual': {'welcome_image_url': 'http://example.com/welcome.jpg'}
        })
        respuesta = handler.handle(payload={})
        self.assertIn("¡Hola, Tester!", respuesta["message_body"])
        self.assertIn("Soy JUNI", respuesta["message_body"])
        self.assertIn("options_list", respuesta)
        self.assertEqual(len(respuesta["options_list"]), 4)
        self.assertEqual(respuesta["options_list"][0]["texto"], "🗣️ Reclamos y Consultas")
        self.assertEqual(respuesta.get("fuente"), "greeting_handler_structured_menu_v2")
        self.assertEqual(respuesta.get("image_url"), 'http://example.com/welcome.jpg')

    def test_greeting_handler_web_menu_includes_submenus(self):
        """El saludo en canal web debe incluir submenús de Obras y Punto Limpio."""
        handler = GreetingHandler(context={
            'profile_name': 'Tester',
            'channel': 'web',
            'municipio_config_actual': {}
        })
        respuesta = handler.handle(payload={})
        categorias = respuesta.get("categorias", [])
        info = next((c for c in categorias if c.get("titulo") == "📰 Información del Municipio"), {})
        botones = [b.get("texto") for b in info.get("botones", [])]
        self.assertIn("🏗️ Obras", botones)
        self.assertIn("♻️ Punto Limpio", botones)
        self.assertEqual(len(respuesta.get("options_list", [])), 12)

    def test_menu_flow_includes_new_submenus(self):
        from services.flows import menu as menu_flow

        respuesta = menu_flow.handle(msg={}, ctx={'municipio_config_actual': {}})
        categorias = respuesta.get("categorias", [])
        info = next((c for c in categorias if c.get("titulo") == "📰 Información del Municipio"), {})
        botones = [b.get("texto") for b in info.get("botones", [])]
        self.assertIn("🏗️ Obras", botones)
        self.assertIn("♻️ Punto Limpio", botones)
        self.assertEqual(len(respuesta.get("options_list", [])), 12)

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_llm_mostrar_menu_returns_full_menu(self, mock_llamar_gemini):
        """Verifica que la acción "mostrar_menu" del LLM devuelve el menú completo."""
        mock_llamar_gemini.return_value = (
            {
                "message_body": "Partial menu",  # Should be replaced by local menu
                "accion_backend": "mostrar_menu",
                "datos_estructura": {"target": "municipio", "nombre_menu": "principal"},
                "botones": [{"texto": "🛠️ Iniciar un Reclamo", "action_id": "mostrar_menu_reclamos"}],
            },
            {}
        )

        response = responder_municipio(
            pregunta_original="otra consulta",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
            channel="whatsapp",
        )

        self.assertIn("Elegí una opción", response.get("message_body", ""))
        self.assertGreaterEqual(len(response.get("options_list", [])), 4)
        self.assertTrue(response.get("options_list"))

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_keyword_pago_tasas(self, mock_llamar_gemini):
        """Ingresar un mensaje sobre impuestos debe devolver info de tasas sin usar el LLM."""
        mock_llamar_gemini.return_value = ({}, {})
        response = responder_municipio(
            pregunta_original="Quiero pagar un impuesto",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("tasas municipales", response.get("message_body", ""))
        mock_llamar_gemini.assert_not_called()

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_keyword_estacionamiento(self, mock_llamar_gemini):
        """Solicitar estacionamiento por texto debe activar la acción correspondiente."""
        mock_llamar_gemini.return_value = ({}, {})
        response = responder_municipio(
            pregunta_original="Necesito estacionar mi auto",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("estacionamiento libre", response.get("message_body", "").lower())
        mock_llamar_gemini.assert_not_called()

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_keyword_defensa_consumidor(self, mock_llamar_gemini):
        """Preguntar por defensa del consumidor devuelve contacto y evita LLM."""
        mock_llamar_gemini.return_value = ({}, {})
        response = responder_municipio(
            pregunta_original="Necesito defensa del consumidor",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("Defensa del Consumidor", response.get("message_body", ""))
        mock_llamar_gemini.assert_not_called()

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_keyword_recoleccion_residuos(self, mock_llamar_gemini):
        """Consultas sobre recolección deben responder con horarios sin usar LLM."""
        mock_llamar_gemini.return_value = ({}, {})
        response = responder_municipio(
            pregunta_original="¿Cuando pasa el camión de basura?",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("camión recolector", response.get("message_body", ""))
        mock_llamar_gemini.assert_not_called()

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_keyword_obras(self, mock_llamar_gemini):
        """Consultas sobre obras deben responder sin usar el LLM."""
        mock_llamar_gemini.return_value = ({}, {})
        response = responder_municipio(
            pregunta_original="¿Qué obras están haciendo?",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        body = response.get("message_body", "").lower()
        self.assertIn("obras", body)
        self.assertNotIn("facebook.com", body)
        self.assertNotIn("instagram.com", body)
        options = response.get("options_list", [])
        social = {opt.get("texto"): opt for opt in options if opt.get("texto") in {"Facebook", "Instagram"}}
        self.assertIn("Facebook", social)
        self.assertIn("Instagram", social)
        self.assertTrue(social["Facebook"].get("image_url"))
        self.assertTrue(social["Instagram"].get("image_url"))
        mock_llamar_gemini.assert_not_called()

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_keyword_punto_limpio(self, mock_llamar_gemini):
        """Preguntar por punto limpio debe devolver info y evitar el LLM."""
        mock_llamar_gemini.return_value = ({}, {})
        response = responder_municipio(
            pregunta_original="¿Dónde está el punto limpio?",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        body = response.get("message_body", "").lower()
        self.assertIn("planta de recolección", body)
        self.assertNotIn("facebook.com", body)
        self.assertNotIn("instagram.com", body)
        options = response.get("options_list", [])
        social = {opt.get("texto"): opt for opt in options if opt.get("texto") in {"Facebook", "Instagram"}}
        self.assertIn("Facebook", social)
        self.assertIn("Instagram", social)
        self.assertTrue(social["Facebook"].get("image_url"))
        self.assertTrue(social["Instagram"].get("image_url"))
        self.assertEqual(
            response.get("image_url"),
            "https://www.juninmendoza.gov.ar/wp-content/uploads/logo-junin-punto-limpio-1024x472.png",
        )
        mock_llamar_gemini.assert_not_called()

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_keyword_tributo(self, mock_llamar_gemini):
        """El uso de la palabra 'tributo' debe resolverse sin el LLM."""
        mock_llamar_gemini.return_value = ({}, {})
        response = responder_municipio(
            pregunta_original="¿Dónde pago un tributo municipal?",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("tasas municipales", response.get("message_body", ""))
        mock_llamar_gemini.assert_not_called()

    @patch('services.municipio_responder.cargar_agenda_cultural')
    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_agenda_incluye_imagen_evento(self, mock_llamar_llm, mock_cargar):
        mock_llamar_llm.return_value = ({}, {})
        mock_cargar.return_value = {
            "eventos": [
                {
                    "tipo_post": "evento",
                    "titulo": "Maratón",
                    "imagen_url": "http://example.com/event.jpg",
                    "fecha_evento_inicio": "2025-08-15T10:00:00"
                }
            ]
        }
        response = responder_municipio(
            pregunta_original="agenda",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertEqual(response.get("image_url"), "http://example.com/event.jpg")
        self.assertIn("maratón", response.get("message_body", "").lower())
        mock_llamar_llm.assert_not_called()

    @patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket')
    @patch('services.actions.municipio_actions.validar_telefono', return_value=True)
    @patch('services.actions.municipio_actions.validar_email', return_value=True)
    @patch(
        'services.location_service.geocode_address',
        return_value={
            'lat': -32.89,
            'lng': -68.83,
            'display_name': 'X 1, Y',
            'maps_search_url': 'https://www.openstreetmap.org/search?query=X%201,%20Y',
        },
    )
    @patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla')
    @patch('services.actions.municipio_actions.enviar_notificacion_sms')
    @patch('services.actions.municipio_actions.formatear_telefono_e164', return_value='+5491111111111')
    @patch('services.herramientas_municipio.parse_direccion_completa', return_value={'calle': 'X', 'numero': '1', 'localidad': 'Y'})
    def test_reclamo_incluye_imagen_promocional(
        self,
        mock_parse,
        mock_formatear_tel,
        mock_sms,
        mock_whatsapp,
        mock_geocode,
        mock_validar_email,
        mock_validar_tel,
        mock_crear_ticket,
    ):
        """El reclamo final debe incluir la imagen promocional configurada."""
        mock_crear_ticket.return_value = {"id": 1, "nro_ticket": "12345", "consulta_pin": "555444"}
        from services.actions.municipio_actions import CrearReclamoActionHandler
        context = {
            'municipio_config_actual': {
                'promo_image_url': 'http://example.com/promo.jpg',
                'base_chat_url': 'https://chat.example'
            },
            'chat_db_context_data': {'processed_idempotency_keys': {}},
            'anon_id': 'test-123'
        }
        handler = CrearReclamoActionHandler(context)
        datos_llm = {
            'categoria': 'Luminaria',
            'descripcion': 'poste caido',
            'ubicacion': 'Calle 123',
            'coordenadas': {'lat': -32.89, 'lon': -68.83},
            'usuario': 'Juan',
            'telefono': '2615550000',
            'email': 'juan@example.com',
            'pin': '555444',
            'dni': '12345678'
        }
        respuesta = handler.execute(datos_llm)
        self.assertEqual(respuesta.get('image_url'), 'http://example.com/promo.jpg')

if __name__ == '__main__':
    unittest.main()
