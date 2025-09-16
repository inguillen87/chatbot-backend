import unittest
from unittest.mock import MagicMock, patch

from services.municipio_responder import (
    ReclamoFlowHandler,
    CONTEXTO_MUNICIPIO,
    ReclamoState,
    ConversationState,
)


class TestReclamoFlowUX(unittest.TestCase):
    def _build_handler(self, flow_context):
        context = {"chat_db_context_data": {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": flow_context}}}
        return ReclamoFlowHandler(context, MagicMock())

    def test_skip_photo_prompt_if_already_has_photo(self):
        flow_context = {
            "state": ReclamoState.ESPERANDO_DIRECCION.name,
            "datos_reclamo": {
                "categoria": "Bache",
                "descripcion": "pozo en la calle",
                "foto_url": "http://example.com/foto.jpg",
                "nombre": "Test User", "dni": "12345", "email": "test@test.com", "telefono": "+5492613168608"
            },
        }
        handler = self._build_handler(flow_context)
        resp = handler.handle_direccion("Calle 123", {})
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_CONFIRMACION.name)
        self.assertIn("foto adjunta", resp["message_body"].lower())

    def test_contact_details_prefilled_goes_to_confirmation(self):
        flow_context = {
            "datos_reclamo": {
                "categoria": "Bache",
                "direccion": "Calle 123",
                "descripcion": "pozo en la calle",
                "nombre": "Juan Perez",
                "dni": "12345678",
                "email": "juan@example.com",
                "telefono": "+5400000000",
            }
        }
        handler = self._build_handler(flow_context)
        resp = handler.ask_for_contact_details()
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_CONFIRMACION.name)
        self.assertIn("Juan Perez", resp["message_body"])

    def test_handle_descripcion_skips_photo_if_already_has(self):
        flow_context = {
            "state": ReclamoState.ESPERANDO_DESCRIPCION.name,
            "datos_reclamo": {
                "categoria": "Bache",
                "direccion": "Calle 123",
                "foto_url": "http://example.com/foto.jpg",
                "nombre": "Test User", "dni": "12345", "email": "test@test.com", "telefono": "+5492613168608"
            },
        }
        handler = self._build_handler(flow_context)
        resp = handler.handle_descripcion("pozo grande en la calle")
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_CONFIRMACION.name)
        self.assertIn("foto adjunta", resp["message_body"].lower())

    def test_start_flow_uses_context_photo(self):
        context = {
            "chat_db_context_data": {},
            "foto_url": "http://example.com/foto.jpg",
        }
        handler = ReclamoFlowHandler(context, MagicMock())
        handler.start_flow(
            datos_iniciales={
                "categoria": "Bache",
                "direccion": "Calle 123",
                "descripcion": "pozo grande",
                "nombre": "Test User", "dni": "12345", "email": "test@test.com", "telefono": "+5492613168608"
            }
        )
        self.assertEqual(
            handler.flow_context["datos_reclamo"].get("foto_url"),
            "http://example.com/foto.jpg",
        )
        self.assertEqual(
            handler.flow_context["state"],
            ReclamoState.ESPERANDO_CONFIRMACION.name,
        )

    def test_handle_foto_accepts_direct_image(self):
        flow_context = {
            "state": ReclamoState.ESPERANDO_FOTO.name,
            "datos_reclamo": {
                "categoria": "Bache",
                "direccion": "Calle 123",
                "descripcion": "pozo grande",
                "nombre": "Test User", "dni": "12345", "email": "test@test.com", "telefono": "+5492613168608"
            },
        }
        handler = self._build_handler(flow_context)
        resp = handler.handle_foto("", {"es_foto": True, "foto_url": "http://example.com/foto.jpg"})
        self.assertEqual(
            handler.flow_context["datos_reclamo"].get("foto_url"),
            "http://example.com/foto.jpg",
        )
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_CONFIRMACION.name)
        self.assertIn("foto adjunta", resp["message_body"].lower())

    def test_missing_dni_goes_to_contact_request(self):
        flow_context = {
            "datos_reclamo": {
                "categoria": "Bache",
                "direccion": "Calle 123",
                "descripcion": "pozo en la calle",
                "nombre": "Juan Perez",
                "email": "juan@example.com",
                "telefono": "+5400000000",
            }
        }
        handler = self._build_handler(flow_context)
        resp = handler.ask_for_contact_details()
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_DATOS_CONTACTO.name)
        self.assertIn("dni", resp["message_body"].lower())

    def test_start_flow_prefills_dni_from_viewer_alias(self):
        class Viewer:
            name = "Juan"
            email = "juan@example.com"
            telefono = "+5400000000"
            dni_vecino = "12345678"

        context = {"chat_db_context_data": {}, "viewer_user_obj": Viewer()}
        handler = ReclamoFlowHandler(context, MagicMock())
        resp = handler.start_flow(
            datos_iniciales={
                "categoria": "Bache",
                "direccion": "Calle 123",
                "descripcion": "pozo grande",
            }
        )
        self.assertEqual(handler.flow_context["datos_reclamo"].get("dni"), "12345678")
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_CONFIRMACION.name)
        self.assertIn("Juan", resp["message_body"])

    def test_confirmacion_resets_context_and_shows_menu(self):
        flow_context = {
            "state": ReclamoState.ESPERANDO_CONFIRMACION.name,
            "datos_reclamo": {
                "categoria": "Bache",
                "descripcion": "pozo",
                "direccion": "Calle 123",
                "nombre": "Juan",
                "dni": "123",
                "email": "juan@test.com",
                "telefono": "+5400000000",
            },
        }
        context = {"chat_db_context_data": {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": flow_context}}}
        handler = ReclamoFlowHandler(context, MagicMock())
        with patch('services.actions.municipio_actions.CrearReclamoActionHandler.execute') as mock_exec:
            mock_exec.return_value = {"success": True, "data": {"nro_ticket": "R-1"}}
            resp = handler.handle_confirmacion("si", {})
        self.assertIn("R-1", resp["message_body"])
        self.assertIn("¿Cómo te puedo ayudar hoy?", resp["message_body"])
        municipal_ctx = context["chat_db_context_data"][CONTEXTO_MUNICIPIO]
        self.assertNotIn("reclamo_flow_v2", municipal_ctx)
        self.assertEqual(
            municipal_ctx["estado_conversacion"],
            ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name,
        )

    def test_confirmacion_negative_returns_to_contact_details(self):
        flow_context = {
            "state": ReclamoState.ESPERANDO_CONFIRMACION.name,
            "datos_reclamo": {
                "categoria": "Bache",
                "descripcion": "pozo",
                "direccion": "Calle 123",
            },
        }
        handler = self._build_handler(flow_context)
        resp = handler.handle_confirmacion("no", {})
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_DATOS_CONTACTO.name)
        self.assertIn("Por favor", resp["message_body"])


if __name__ == "__main__":
    unittest.main()
