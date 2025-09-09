from types import SimpleNamespace
from unittest.mock import patch

from services.conversation_state import ConversationState
from services.municipio_responder import handle_location_for_reclamo


def test_location_completes_claim_without_llm():
    context = {}
    chat_db_context = None
    contexto = {
        "estado_conversacion": ConversationState.ESPERANDO_DIRECCION_RECLAMO.name,
        "datos_parciales_llm_reclamo": {
            "categoria": "Arbolado",
            "descripcion": "rama caída"
        },
    }
    location = {"latitude": "-33", "longitude": "-68", "address": "Sarmiento 100"}
    with patch("services.municipio_responder.CrearReclamoActionHandler") as mock_handler:
        mock_handler.return_value.execute.return_value = {"message_body": "ok"}
        result, updated = handle_location_for_reclamo(
            context, None, None, chat_db_context, contexto, location
        )
    mock_handler.return_value.execute.assert_called_once()
    assert result["message_body"] == "ok"
    assert updated["estado_conversacion"] == ConversationState.CONVERSACION_GENERAL_LLM.name
    coords = updated["datos_parciales_llm_reclamo"]["coordenadas"]
    assert coords["lat"] == "-33" and coords["lon"] == "-68"
