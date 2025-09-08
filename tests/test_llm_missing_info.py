import json
from types import SimpleNamespace
from unittest.mock import patch

from services.conversation_state import ConversationState
from services.llm_interaction_handler import handle_llm_interaction


def test_responder_directamente_keeps_waiting_state():
    context = {"chat_session_uuid": "test-session"}
    viewer_user = None
    owner_user = None
    chat_db_context = SimpleNamespace(context_data={})
    contexto = {
        "estado_conversacion": ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name,
        "datos_parciales_llm_reclamo": {"categoria": "arbol caido"},
        "historial_llm_reclamo": [],
        "esperando_info_llm_reclamo": "ubicacion",
    }

    llm_response = {
        "message_body": "De nada!",
        "accion_backend": "responder_directamente",
        "datos_estructura": {"target": "municipio"},
        "pedir_info": None,
        "botones": [],
    }

    with patch(
        "services.llm_interaction_handler.llamar_llm_con_fallback",
        return_value=(llm_response, {}),
    ):
        result, updated_context = handle_llm_interaction(
            app=None,
            pregunta_str="gracias",
            context=context,
            viewer_user=viewer_user,
            owner_user=owner_user,
            chat_db_context=chat_db_context,
            contexto_municipio_actual=contexto,
        )

    assert updated_context["estado_conversacion"] == ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name
    assert "historial_llm_reclamo" in updated_context
    assert updated_context["historial_llm_reclamo"][0]["respuesta_ia"] == "De nada!"
    assert "historial_conversacion_general_llm" not in updated_context
    assert result["message_body"] == "De nada!"
