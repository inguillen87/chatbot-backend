from types import SimpleNamespace
from unittest.mock import patch

from services.actions.municipio_actions import (
    CONTEXTO_MUNICIPIO,
    SolicitarLlamadaActionHandler,
)
from services.constants import ConversationState
from services.municipio_responder import handle_main_menu_action


def _handler(
    *,
    anon_id="whatsapp:+5492613168608",
    tenant_config=None,
    viewer_phone=None,
    contact_phone=None,
):
    tenant = SimpleNamespace(plan="full", configuracion=tenant_config or {})
    owner = SimpleNamespace(tenant=tenant, plan="full")
    viewer = SimpleNamespace(telefono=viewer_phone) if viewer_phone else None
    chat_data = {}
    if contact_phone:
        chat_data[CONTEXTO_MUNICIPIO] = {
            "contacto_usuario": {"telefono": contact_phone}
        }
    return SolicitarLlamadaActionHandler(
        {
            "anon_id": anon_id,
            "user_obj": owner,
            "viewer_user_obj": viewer,
            "chat_session_uuid": "session-callback-1",
            "chat_db_context_data": chat_data,
        }
    )


@patch.dict(
    "os.environ",
    {"TWILIO_VOICE_PHONE_NUMBER": "+17432643718"},
    clear=False,
)
@patch("services.actions.municipio_actions.initiate_outbound_call", return_value=True)
def test_callback_uses_explicit_voice_caller_and_reports_provider_acceptance(mock_call):
    response = _handler().execute({"motivo_llamada": "seguimiento"})

    assert response["success"] is True
    assert response["data"]["delivery_state"] == "provider_accepted"
    assert "solicitud de llamada fue aceptada" in response["message_to_user"]
    mock_call.assert_called_once_with(
        to_number="+5492613168608",
        from_number="+17432643718",
        chat_session_id="session-callback-1",
    )


@patch.dict(
    "os.environ",
    {"TWILIO_VOICE_PHONE_NUMBER": "+17432643718"},
    clear=False,
)
@patch("services.actions.municipio_actions.initiate_outbound_call")
def test_callback_rejects_invalid_destination_before_provider_send(mock_call):
    response = _handler(anon_id="whatsapp:not-a-phone").execute({})

    assert response["success"] is False
    assert response["data"]["delivery_state"] == "rejected_invalid_destination"
    mock_call.assert_not_called()


@patch.dict(
    "os.environ",
    {"TWILIO_VOICE_PHONE_NUMBER": "+17432643718"},
    clear=False,
)
@patch("services.actions.municipio_actions.initiate_outbound_call", return_value=False)
def test_callback_does_not_claim_delivery_when_provider_outcome_is_unknown(mock_call):
    response = _handler().execute({})

    assert response["success"] is False
    assert response["data"]["delivery_state"] == "provider_outcome_unknown"
    assert "evitar duplicarla" in response["message_to_user"]
    mock_call.assert_called_once()


@patch.dict("os.environ", {}, clear=True)
@patch("services.actions.municipio_actions.initiate_outbound_call")
def test_callback_requires_a_dedicated_voice_caller_id(mock_call):
    response = _handler().execute({})

    assert response["success"] is False
    assert response["data"]["delivery_state"] == "rejected_invalid_caller_id"
    mock_call.assert_not_called()


@patch.dict(
    "os.environ",
    {"TWILIO_PHONE_NUMBER": "whatsapp:+14155550123"},
    clear=True,
)
@patch("services.actions.municipio_actions.initiate_outbound_call")
def test_callback_never_uses_generic_whatsapp_sender_as_voice_caller(mock_call):
    response = _handler().execute({})

    assert response["success"] is False
    assert response["data"]["delivery_state"] == "rejected_invalid_caller_id"
    mock_call.assert_not_called()


@patch.dict(
    "os.environ",
    {"TWILIO_VOICE_PHONE_NUMBER": "+17432643718"},
    clear=False,
)
@patch("services.actions.municipio_actions.initiate_outbound_call", return_value=True)
def test_callback_resolves_local_session_contact_with_tenant_country(mock_call):
    response = _handler(
        anon_id="opaque-session-id",
        contact_phone="261 316 8608",
        tenant_config={"voice_country_calling_code": "54"},
    ).execute({})

    assert response["success"] is True
    mock_call.assert_called_once_with(
        to_number="+5492613168608",
        from_number="+17432643718",
        chat_session_id="session-callback-1",
    )


@patch("services.municipio_responder.SolicitarLlamadaActionHandler")
def test_menu_callback_delegates_to_canonical_handler_and_preserves_session(mock_handler):
    mock_handler.return_value.execute.return_value = {
        "success": True,
        "message_to_user": "La solicitud de llamada fue aceptada.",
        "message_type": "text",
        "data": {"delivery_state": "provider_accepted"},
    }
    context = {
        "anon_id": "whatsapp:+5492613168608",
        "chat_db_context_data": {},
    }
    chat_session = SimpleNamespace(chat_session_id="db-session-42")

    response = handle_main_menu_action(
        "solicitar_llamada_ia",
        context,
        chat_session,
    )

    handler_context = mock_handler.call_args.args[0]
    assert handler_context["chat_session_uuid"] == "db-session-42"
    mock_handler.return_value.execute.assert_called_once_with({})
    assert response["data"]["delivery_state"] == "provider_accepted"
    assert response["fuente"] == "solicitar_llamada_provider_accepted"
    assert "te estamos llamando" not in response["message_body"].lower()


@patch("services.municipio_responder.SolicitarLlamadaActionHandler")
def test_menu_callback_requests_phone_and_updates_conversation_state(mock_handler):
    mock_handler.return_value.execute.return_value = {
        "success": False,
        "message_to_user": "Para llamarte necesito un teléfono válido.",
        "message_type": "text",
        "pedir_info": "telefono",
        "data": {"delivery_state": "rejected_missing_destination"},
    }
    context = {"chat_db_context_data": {}}

    response = handle_main_menu_action("solicitar_llamada_ia", context, None)

    municipal_context = context["chat_db_context_data"][CONTEXTO_MUNICIPIO]
    assert municipal_context["estado_conversacion"] == (
        ConversationState.ESPERANDO_TELEFONO_VECINO.name
    )
    assert response["data"]["delivery_state"] == "rejected_missing_destination"
