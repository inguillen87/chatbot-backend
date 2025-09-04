import unittest
from unittest.mock import MagicMock

from services.municipio_responder import (
    ReclamoFlowHandler,
    CONTEXTO_MUNICIPIO,
    ReclamoState,
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
            },
        }
        handler = self._build_handler(flow_context)
        resp = handler.handle_direccion("Calle 123", {})
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_DATOS_CONTACTO.name)
        self.assertNotIn("foto", resp["message_body"].lower())

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
            },
        }
        handler = self._build_handler(flow_context)
        resp = handler.handle_descripcion("pozo grande en la calle")
        self.assertEqual(handler.flow_context["state"], ReclamoState.ESPERANDO_DATOS_CONTACTO.name)
        self.assertNotIn("foto", resp["message_body"].lower())

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


if __name__ == "__main__":
    unittest.main()
