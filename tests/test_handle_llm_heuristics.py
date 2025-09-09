from types import SimpleNamespace
from unittest.mock import patch

from services.conversation_state import ConversationState
from services.llm_interaction_handler import handle_llm_interaction


def test_handle_llm_creates_claim_without_llm():
    context = {"chat_session_uuid": "test"}
    chat_db_context = SimpleNamespace(context_data={})
    contexto = {"estado_conversacion": ConversationState.CONVERSACION_GENERAL_LLM.name}

    with patch("services.llm_interaction_handler.llamar_llm_con_fallback") as mock_llm, \
         patch("services.llm_interaction_handler.CrearReclamoActionHandler") as mock_handler:
        mock_handler.return_value.execute.return_value = {"message_body": "OK"}
        result, updated = handle_llm_interaction(
            app=None,
            pregunta_str="hay un arbol caido en sarmiento 100",
            context=context,
            viewer_user=None,
            owner_user=None,
            chat_db_context=chat_db_context,
            contexto_municipio_actual=contexto,
        )

    mock_llm.assert_not_called()
    mock_handler.return_value.execute.assert_called_once()
    assert result["message_body"] == "OK"
    assert updated["datos_parciales_llm_reclamo"]["categoria"]
    assert updated["datos_parciales_llm_reclamo"]["ubicacion"]


def test_handle_llm_requests_location_without_llm():
    context = {"chat_session_uuid": "test"}
    chat_db_context = SimpleNamespace(context_data={})
    contexto = {"estado_conversacion": ConversationState.CONVERSACION_GENERAL_LLM.name}

    with patch("services.llm_interaction_handler.llamar_llm_con_fallback") as mock_llm:
        result, updated = handle_llm_interaction(
            app=None,
            pregunta_str="hay un arbol caido",
            context=context,
            viewer_user=None,
            owner_user=None,
            chat_db_context=chat_db_context,
            contexto_municipio_actual=contexto,
        )

    mock_llm.assert_not_called()
    assert updated["estado_conversacion"] == ConversationState.ESPERANDO_DIRECCION_RECLAMO.name
    assert "ubicación" in result["message_body"].lower()
