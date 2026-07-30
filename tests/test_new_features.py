import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from types import SimpleNamespace

# Asegúrate de que el directorio raíz del proyecto esté en el sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.ticket_utils import (
    formatear_ticket_respuesta,
    construir_descripcion_breve,
    _remove_redundant_urls_from_message,
    remove_buttons_with_urls_in_message,
)
from services.whatsapp_receipts import render_ticket_whatsapp
from services.municipio_responder import GreetingHandler
from services.municipio_responder import responder_municipio
from services.pymes import url_descargar_catalogo_pyme
from services.actions.pyme_order_actions import AgregarItemCarritoAction, ConsultarEstadoPedidoAction
from services.common_utils import parse_cantidad_flexible
from services.constants import CONTEXTO_MUNICIPIO, ConversationState
from config import TestConfig
from app import create_app
from models import db, User, ArchivoAdjunto, PymePedido, PedidoConversacional, TenantProfile


def _unexpected_deterministic_provider_call(*_args, **_kwargs):
    raise AssertionError("A deterministic municipal shortcut attempted an LLM/provider call")


def _forbid_deterministic_provider_calls(test_func):
    """Fail before any configured provider can be reached by shortcut tests."""

    return patch.multiple(
        "services.municipio_responder",
        llamar_gemini=_unexpected_deterministic_provider_call,
        extract_multiple_contact_details_llm=_unexpected_deterministic_provider_call,
        extract_complaint_details_llm=_unexpected_deterministic_provider_call,
    )(test_func)


class TestNewFeatures(unittest.TestCase):

    WHATSAPP_MENU_ACTIONS = {
        "mostrar_menu_reclamos",
        "mostrar_menu_tramites",
        "mostrar_menu_informacion",
        "mostrar_menu_catalogo",
        "mostrar_menu_encuestas",
        "mostrar_menu_estacionamiento",
        "solicitar_llamada",
        "mostrar_menu_ayuda",
    }

    WEB_MENU_ACTIONS = {
        "iniciar_reclamo",
        "enviar_sugerencia",
        "consultar_estado_reclamo",
        "contactos_utiles",
        "licencia_de_conducir",
        "solicitar_turnos",
        "pago_de_tasas_vigentes",
        "agenda_y_noticias",
        "veterinaria_bromatologia",
        "obras",
        "punto_limpio",
        "catalogo_ver",
        "catalogo_canje_puntos",
        "catalogo_compras",
        "catalogo_donaciones",
        "mostrar_menu_encuestas",
        "buscar_estacionamiento",
        "solicitar_llamada",
        "mostrar_menu_ayuda",
    }

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

    def assert_menu_actions(self, response, expected_actions):
        action_ids = [item.get("action_id") for item in response.get("options_list", [])]
        self.assertEqual(set(action_ids), expected_actions)
        self.assertEqual(len(action_ids), len(expected_actions), "El menu no debe repetir acciones")

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
        self.assertEqual(len(buttons), 0)

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

    def test_formatear_ticket_respuesta_sin_enlace_para_web(self):
        message, buttons = formatear_ticket_respuesta(
            "reclamo",
            "Ana",
            "Descripción",
            "Categoria",
            "M-99999",
            contacto_especializado=None,
            base_chat_url="https://example.com",
            consulta_pin="654321",
            include_links_in_message=False,
        )
        expected_url = "https://example.com/tracking/claim/99999#pin=654321"
        self.assertIn("Ver mi Ticket", message)
        self.assertIn("botón \"Ver mi Ticket\"", message)
        self.assertNotIn(expected_url, message)
        tracking_button = next((btn for btn in buttons if btn.get("url") == expected_url), None)
        self.assertIsNotNone(tracking_button)
        self.assertEqual(tracking_button.get("texto"), "💬 Ver mi Ticket")
        self.assertNotIn("?pin=", tracking_button.get("url", ""))

    def test_formatear_ticket_respuesta_recorta_descripcion(self):
        message, _ = formatear_ticket_respuesta(
            "reclamo",
            "Marcelo",
            "tengo un poste caido a mitad de cuadra",
            "Luminaria",
            "M-123",
        )
        self.assertIn("caido a mitad de cuadra", message)
        self.assertNotIn("tengo un poste", message)

    def test_formatear_ticket_respuesta_para_pedido(self):
        message, buttons = formatear_ticket_respuesta(
            "pedido",
            "Ana",
            "Caja de espumantes brut nature",
            "Bodega",
            "PED-20241001",
            base_chat_url="https://ventas.example/pedidos",
            consulta_pin="123456",
        )
        self.assertIn("Pedido recibido", message)
        self.assertIn("PED-20241001", message)
        self.assertEqual(
            buttons,
            [{
                "texto": "💬 Ver mi Ticket",
                "url": "https://ventas.example/pedidos/tracking/order/PED-20241001",
                "type": "url",
            }],
        )

    def test_formatear_ticket_respuesta_formatea_horario_json(self):
        contacto = {
            "nombre": "Mesa de Entrada",
            "telefono": "+5492634519821",
            "horario": [
                {"dia": "Lunes", "abre": "09:00", "cierra": "20:00", "cerrado": False},
                {"dia": "Sábado", "abre": "", "cierra": "", "cerrado": True},
            ],
        }
        message, _ = formatear_ticket_respuesta(
            "reclamo",
            "Marcelo",
            "Descripción",
            "Categoria",
            "M-12345",
            contacto,
        )
        self.assertIn("Lunes: 09:00-20:00", message)
        self.assertIn("Sábado: cerrado", message)
        self.assertNotIn("[{\"dia\"", message)

    def test_render_ticket_whatsapp_formatea_horario_json(self):
        payload = render_ticket_whatsapp(
            kind="reclamo",
            nombre="Marcelo",
            ticket_nro="M-111",
            categoria="General",
            descripcion="Desc",
            contacto_especializado={
                "nombre": "Punto Limpio",
                "horario": [
                    {"dia": "Martes", "abre": "10:00", "cierra": "18:00", "cerrado": False},
                    {"dia": "Domingo", "abre": "", "cierra": "", "cerrado": True},
                ],
            },
            include_menu=False,
        )
        body = payload["body_text"]
        self.assertIn("Martes: 10:00-18:00", body)
        self.assertIn("Domingo: cerrado", body)
        self.assertIn("✅ *¡Reclamo recibido, Marcelo!*", body)
        self.assertIn("📄 *Resumen:*", body)
        self.assertIn("🔗 *Seguimiento:*", body)
        self.assertIn("*Contacto para seguimiento:*", body)
        self.assertNotIn("[{\"dia\"", body)
        self.assertNotIn("Ã", body)
        self.assertNotIn("â", body)
        self.assertNotIn("ð", body)

    def test_construir_descripcion_breve_incluye_detalle(self):
        texto = (
            "quiero pedir que corten las ramas de los arboles del barrio jardin en el centro de junin "
            "esta tapando y cruzando la medianera me ensucia toda la pileta"
        )
        resumen = construir_descripcion_breve(texto)
        self.assertIn("Ramas", resumen)
        self.assertTrue(
            "pileta" in resumen.lower() or "medianera" in resumen.lower(),
            msg=f"Resumen poco descriptivo: {resumen}",
        )

    def test_construir_descripcion_breve_descarta_saludo_inicial(self):
        texto = (
            "Hola, buenas tardes. Sí, mirá, quería hacer un reclamo. "
            "Tengo un poste caído acá a mitad de cuadra en mi barrio. "
            "Mi dirección es en Don Bosco 55 esquina Sarmiento de Junín."
        )
        resumen = construir_descripcion_breve(texto)

        self.assertIn("Poste", resumen)
        self.assertTrue(
            "caido" in resumen.lower() or "caído" in resumen.lower(),
            msg=f"Resumen debería incluir el problema detectado: {resumen}",
        )

    def test_remove_redundant_footer_links(self):
        message = (
            "🔗 *Seguimiento:*\n"
            "• *Ver mi Ticket:* https://www.chatboc.ar/chat/799928?pin=768114\n"
            "Visitar Punto Limpio: https://www.juninmendoza.gov.ar/\n"
            "💬 Ver mi Ticket: https://www.chatboc.ar/chat/799928?pin=768114"
        )
        buttons = [
            {"texto": "Visitar Punto Limpio", "url": "https://www.juninmendoza.gov.ar/"},
            {"texto": "💬 Ver mi Ticket", "url": "https://www.chatboc.ar/chat/799928?pin=768114"},
        ]
        cleaned = _remove_redundant_urls_from_message(message, buttons)
        self.assertNotIn("Visitar Punto Limpio:", cleaned)
        self.assertNotIn("💬 Ver mi Ticket:", cleaned)
        self.assertIn("• *Ver mi Ticket:* https://www.chatboc.ar/chat/799928?pin=768114", cleaned)

    def test_remove_buttons_with_urls_in_message(self):
        message = (
            "✅ ¡Reclamo recibido!\n"
            "🔗 Seguimiento:\n"
            "• *Ver mi Ticket:* https://www.chatboc.ar/chat/123?pin=456\n"
            "🔗 Más info: https://www.juninmendoza.gov.ar/punto-limpio"
        )
        buttons = [
            {"texto": "💬 Ver mi Ticket", "url": "https://www.chatboc.ar/chat/123?pin=456"},
            {"texto": "Visitar Punto Limpio", "url": "https://www.juninmendoza.gov.ar/punto-limpio"},
            {"texto": "Editar", "action_id": "editar"},
        ]

        filtered = remove_buttons_with_urls_in_message(message, buttons)

        self.assertEqual(len(filtered), 1)
        self.assertTrue(any(btn.get("action_id") == "editar" for btn in filtered))

    def test_greeting_handler_final_menu(self):
        """
        Verifica que el GreetingHandler devuelve el menu municipal completo y tenant-neutral.
        """
        handler = GreetingHandler(context={
            'profile_name': 'Tester',
            'channel': 'whatsapp',
            'municipio_config_actual': {}
        })
        respuesta = handler.handle(payload={})
        self.assertIn("¡Hola, Tester!", respuesta["message_body"])
        self.assertIn("Bienvenido a *tu municipio*", respuesta["message_body"])
        self.assertNotIn("JUNI", respuesta["message_body"])
        self.assert_menu_actions(respuesta, self.WHATSAPP_MENU_ACTIONS)
        self.assertEqual(respuesta["options_list"][0]["texto"], "🗣️ Reclamos y Consultas")
        self.assertEqual(respuesta.get("fuente"), "greeting_handler_structured_menu_v2")
        self.assertEqual(respuesta.get("message_type"), "interactive_list")
        self.assertTrue(respuesta.get("generar_audio"))
        self.assertTrue(respuesta.get("audio_text"))

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
        self.assert_menu_actions(respuesta, self.WEB_MENU_ACTIONS)

    def test_menu_flow_includes_new_submenus(self):
        from services.flows import menu as menu_flow

        respuesta = menu_flow.handle(
            msg={},
            ctx={
                'profile_name': 'Tester',
                'municipio_config_actual': {},
                'chat_db_context_data': {
                    CONTEXTO_MUNICIPIO: {'contacto_usuario': {'nombre': 'Tester'}},
                },
            },
        )
        categorias = respuesta.get("categorias", [])
        info = next((c for c in categorias if c.get("titulo") == "📰 Información del Municipio"), {})
        botones = [b.get("texto") for b in info.get("botones", [])]
        self.assertIn("🏗️ Obras", botones)
        self.assertIn("♻️ Punto Limpio", botones)
        self.assert_menu_actions(respuesta, self.WEB_MENU_ACTIONS)

    def test_url_descargar_catalogo_pyme_prefiere_url_externa_del_catalogo(self):
        owner = User(email="catalogo-owner@test.com", name="Catalogo Owner", rol="admin", tipo_chat="pyme")
        owner.set_password("test")
        db.session.add(owner)
        db.session.flush()
        db.session.add(
            ArchivoAdjunto(
                user_id=owner.id,
                filename="catalogo.pdf",
                nombre_original="catalogo.pdf",
                tipo="catalogo",
                url="https://cdn.cloudflare.example/catalogos/catalogo-bodega.pdf",
                mime="application/pdf",
            )
        )
        db.session.commit()

        url = url_descargar_catalogo_pyme(owner.id)
        self.assertEqual(url, "https://cdn.cloudflare.example/catalogos/catalogo-bodega.pdf")

    @patch("services.actions.pyme_order_actions.buscar_catalogo_qdrant")
    def test_agregar_item_carrito_pide_desambiguar_en_consulta_exploratoria(self, mock_qdrant):
        class _Hit:
            def __init__(self, payload):
                self.payload = payload

        mock_qdrant.return_value = [
            _Hit({"nombre": "Malbec Reserva 2022", "precio_str": "43200", "sku": "MALB-RES-22"}),
            _Hit({"nombre": "Malbec Clásico", "precio_str": "21000", "sku": "MALB-CLA"}),
        ]

        handler = AgregarItemCarritoAction(
            {
                "user_id": 99,
                "pregunta_actual_usuario": "quiero comprar malbec que tenes?",
                "chat_db_context_data": {},
            }
        )

        response = handler.execute({"nombre_producto_mencionado": "malbec"})
        self.assertTrue(response.get("success"))
        self.assertIn("varias opciones", response.get("message_to_user", "").lower())
        self.assertIn("43.200", response.get("message_to_user", ""))
        self.assertEqual(response.get("fuente"), "pyme_disambiguacion_producto_v1")
        opciones = response.get("options_list") or []
        self.assertGreaterEqual(len(opciones), 2)
        self.assertTrue(any(str(opt.get("id_accion", "")).startswith("agregar_item_carrito__") for opt in opciones))

    def test_consultar_estado_pedido_usa_estado_real_scoped(self):
        owner = User(
            email="pedido-real-owner@test.com",
            name="Pedido Real Owner",
            password_hash="hash",
            rol="admin",
            tipo_chat="pyme",
        )
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(
            slug="pedido-real",
            nombre="Pedido Real",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()

        pedido = PymePedido(
            pyme_id=owner.id,
            tenant_id=tenant.id,
            asunto="Pedido real",
            detalles="[]",
            monto_total=15800,
            nombre_cliente="Cliente",
        )
        pedido.nro_pedido = "PED-REAL-1"
        pedido.estado = "confirmado"
        db.session.add(pedido)
        db.session.commit()

        handler = ConsultarEstadoPedidoAction({"user_id": owner.id, "tenant_id": tenant.id})
        response = handler.execute({"id_pedido_mencionado": "PED-REAL-1"})

        self.assertTrue(response.get("success"))
        self.assertEqual(response["data"]["estado_actual"], "confirmado")
        self.assertEqual(response["data"]["source_model"], "PymePedido")
        self.assertNotIn("En preparaci", response.get("message_to_user", ""))
        self.assertIn("confirmado", response.get("message_to_user", ""))

    def test_consultar_estado_pedido_no_filtra_otro_tenant(self):
        owner_a = User(
            email="pedido-owner-a@test.com",
            name="Pedido Owner A",
            password_hash="hash",
            rol="admin",
            tipo_chat="pyme",
        )
        owner_b = User(
            email="pedido-owner-b@test.com",
            name="Pedido Owner B",
            password_hash="hash",
            rol="admin",
            tipo_chat="pyme",
        )
        db.session.add_all([owner_a, owner_b])
        db.session.flush()

        tenant_a = TenantProfile(slug="pedido-a", nombre="Pedido A", tipo="pyme", pyme_id=owner_a.id)
        tenant_b = TenantProfile(slug="pedido-b", nombre="Pedido B", tipo="pyme", pyme_id=owner_b.id)
        db.session.add_all([tenant_a, tenant_b])
        db.session.flush()

        pedido_b = PymePedido(
            pyme_id=owner_b.id,
            tenant_id=tenant_b.id,
            asunto="Pedido de B",
            detalles="[]",
            monto_total=4200,
            nombre_cliente="Cliente B",
        )
        pedido_b.nro_pedido = "PED-OTRO-TENANT"
        pedido_b.estado = "despachado"
        db.session.add(pedido_b)
        db.session.commit()

        handler = ConsultarEstadoPedidoAction({"user_id": owner_a.id, "tenant_id": tenant_a.id})
        response = handler.execute({"id_pedido_mencionado": "PED-OTRO-TENANT"})

        self.assertTrue(response.get("success"))
        self.assertIsNone(response["data"]["estado_actual"])
        self.assertIsNone(response["data"]["source_model"])
        self.assertNotIn("despachado", response.get("message_to_user", ""))

    def test_consultar_estado_pedido_conversacional_por_id(self):
        owner = User(
            email="pedido-conv-owner@test.com",
            name="Pedido Conv Owner",
            password_hash="hash",
            rol="admin",
            tipo_chat="pyme",
        )
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(
            slug="pedido-conv",
            nombre="Pedido Conv",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()

        pedido = PedidoConversacional(
            tenant_id=tenant.id,
            user_id=owner.id,
            estado="pagado",
            monto_monetario=9900,
            items=[],
        )
        db.session.add(pedido)
        db.session.commit()

        handler = ConsultarEstadoPedidoAction({"tenant_id": tenant.id, "tenant_slug": tenant.slug})
        response = handler.execute({"id_pedido_mencionado": str(pedido.id)})

        self.assertTrue(response.get("success"))
        self.assertEqual(response["data"]["estado_actual"], "pagado")
        self.assertEqual(response["data"]["source_model"], "PedidoConversacional")
        self.assertEqual(response["data"]["tenant_id"], tenant.id)

    def test_parse_cantidad_flexible_prioriza_empaque_sobre_medida(self):
        self.assertEqual(parse_cantidad_flexible("Caja x6 - 750 ml"), 6)
        self.assertEqual(parse_cantidad_flexible("Pack de 12 latas"), 12)
        self.assertIsNone(parse_cantidad_flexible("750 ml"))

    @patch('services.municipio_responder.llamar_gemini')
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

        chat_context = MagicMock(
            context_data={
                "profile_name": "Tester",
                CONTEXTO_MUNICIPIO: {
                    "contacto_usuario": {"nombre": "Tester"},
                    "estado_conversacion": ConversationState.CONVERSACION_GENERAL_LLM.name,
                },
            },
            chat_session_id="llm-menu-test",
        )
        owner_user = SimpleNamespace(
            id=1,
            municipio_id=1,
            tipo_chat="municipio",
            name="Municipio Test",
            nombre_empresa=None,
            email="municipio@test.com",
            ciudad=None,
            provincia=None,
            pais=None,
            direccion=None,
        )
        rubro = SimpleNamespace(id=1, clave="municipio", nombre="municipio")
        with patch(
            'services.municipio_responder.intent_classifier.classify',
            return_value=(None, None),
        ) as mock_classify:
            response = responder_municipio(
                pregunta_original="Necesito orientacion porque todavia no se como expresar lo que busco",
                owner_user=owner_user,
                rubro_obj=rubro,
                chat_db_context=chat_context,
                channel="whatsapp",
                profile_name="Tester",
            )

        mock_classify.assert_called_once()
        mock_llamar_gemini.assert_called_once()
        self.assertIn("Podés compartir tu ubicación", response.get("message_body", ""))
        self.assert_menu_actions(response, self.WHATSAPP_MENU_ACTIONS)
        self.assertTrue(
            any(opt.get("texto") == "🗣️ Reclamos y Consultas" for opt in response.get("options_list", []))
        )

    @_forbid_deterministic_provider_calls
    def test_keyword_pago_tasas(self):
        """Ingresar un mensaje sobre impuestos debe devolver info de tasas sin usar el LLM."""
        response = responder_municipio(
            pregunta_original="Quiero pagar un impuesto",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("tasas municipales", response.get("message_body", ""))

    @_forbid_deterministic_provider_calls
    def test_keyword_estacionamiento(self):
        """Solicitar estacionamiento por texto debe activar la acción correspondiente."""
        response = responder_municipio(
            pregunta_original="Necesito estacionar mi auto",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("estacionamiento libre", response.get("message_body", "").lower())

    @_forbid_deterministic_provider_calls
    def test_keyword_defensa_consumidor(self):
        """Preguntar por defensa del consumidor devuelve contacto y evita LLM."""
        response = responder_municipio(
            pregunta_original="Necesito defensa del consumidor",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("Defensa del Consumidor", response.get("message_body", ""))

    @_forbid_deterministic_provider_calls
    def test_keyword_recoleccion_residuos(self):
        """Consultas sobre recolección deben responder con horarios sin usar LLM."""
        response = responder_municipio(
            pregunta_original="¿Cuando pasa el camión de basura?",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("camión recolector", response.get("message_body", ""))

    @_forbid_deterministic_provider_calls
    def test_keyword_obras(self):
        """Consultas sobre obras deben responder sin usar el LLM."""
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

    @_forbid_deterministic_provider_calls
    def test_keyword_punto_limpio(self):
        """Preguntar por punto limpio debe devolver info y evitar el LLM."""
        response = responder_municipio(
            pregunta_original="¿Dónde está el punto limpio?",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        body = response.get("message_body", "").lower()
        self.assertIn("punto limpio", body)
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

    @_forbid_deterministic_provider_calls
    def test_keyword_tributo(self):
        """El uso de la palabra 'tributo' debe resolverse sin el LLM."""
        response = responder_municipio(
            pregunta_original="¿Dónde pago un tributo municipal?",
            owner_user=MagicMock(id=1),
            rubro_obj=MagicMock(nombre='municipio'),
            chat_db_context=MagicMock(context_data={}),
        )
        self.assertIn("tasas municipales", response.get("message_body", ""))

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
    @patch('services.location_service.geocode_address', return_value=(-32.89, -68.83))
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
        """El recibo web incluye la imagen promocional del tenant validado."""
        mock_crear_ticket.return_value = {"id": 1, "nro_ticket": "12345", "consulta_pin": "555444"}
        from services.actions.municipio_actions import CrearReclamoActionHandler
        owner = User(
            name="Municipio Promo",
            email="municipio-promo@test.com",
            password_hash="test-hash",
            rol="admin",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="municipio-promo",
            nombre="Municipio Promo",
            tipo="municipio",
            municipio_id=owner.id,
        )
        db.session.add(tenant)
        db.session.commit()
        context = {
            'user_obj': owner,
            'tenant_profile': tenant,
            'tenant_id': tenant.id,
            'channel': 'web',
            'municipio_config_actual': {
                'tenant_slug': tenant.slug,
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
            'pin': '555444'
        }
        respuesta = handler.execute(datos_llm)
        self.assertEqual(respuesta.get('image_url'), 'http://example.com/promo.jpg')

if __name__ == '__main__':
    unittest.main()
