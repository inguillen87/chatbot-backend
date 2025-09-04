import unittest
from unittest.mock import patch

from services.points_of_interest_handler import PointsOfInterestHandler
from services.conversation_state import ConversationState


class TestPOIFlowMenuReset(unittest.TestCase):
    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value={"libres": 1, "camera": "Demo", "timestamp": "00:00"})
    def test_poi_response_shows_menu_and_clears_state(self, mock_occ):
        context = {"chat_db_context_data": {}}
        handler = PointsOfInterestHandler(context=context)
        loc = {"lat": -33.0, "lon": -68.0, "address": "Test"}
        resp = handler.handle({"pregunta": "estacionamiento", "location": loc})
        self.assertIn("¿Cómo te puedo ayudar hoy?", resp.get("message_body", ""))
        municipio_ctx = context["chat_db_context_data"].get("contexto_municipio_v2", {})
        self.assertEqual(
            municipio_ctx.get("estado_conversacion"),
            ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name,
        )


if __name__ == "__main__":
    unittest.main()
