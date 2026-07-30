from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.actions.municipio_actions import CrearReclamoActionHandler
from services.constants import CONTEXTO_MUNICIPIO
from services.llm_utils import extract_complaint_details_llm
from services.municipio_responder import (
    ReclamoFlowHandler,
    ReclamoState,
    _try_start_reclamo_from_text,
    extract_reclamo_details_from_text,
)


VOICE_CLAIM_WITH_CALLBACK = (
    "Tengo un poste caído acá en el barrio, acá en Palmira. "
    "¿Puedes ayudarme? ¿Puedes llamarme por teléfono o te llamo yo?"
)


def _complete_claim(**overrides):
    claim = {
        "categoria": "Luminaria",
        "descripcion": "Tengo un poste caído en el barrio.",
        "direccion": "Don Bosco 56, Palmira",
        "nombre": "Marcelo",
        "dni": "32877851",
        "email": "marcelo@example.com",
        "telefono": "+5492613168608",
    }
    claim.update(overrides)
    return claim


def _handler_for(state, claim):
    flow = {
        "state": state.name,
        "confirmation_id": "claim-regression-001",
        "datos_reclamo": claim,
    }
    context = {
        "channel": "whatsapp",
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "estado_conversacion": "EN_FLUJO_RECLAMO",
                "reclamo_flow_v2": flow,
            }
        },
    }
    return ReclamoFlowHandler(context, chat_db_context=None)


def test_audio_claim_preserves_llm_callback_request_through_guided_draft():
    llm_result = {
        "tipo_problema": "Luminaria",
        "descripcion_problema": "Poste caído en el barrio de Palmira.",
        "solicita_llamada": True,
        "motivo_llamada": "Quiere coordinar el reclamo por teléfono.",
    }

    with (
        patch(
            "services.municipio_responder.extract_multiple_contact_details_llm",
            return_value={},
        ),
        patch(
            "services.municipio_responder.extract_complaint_details_llm",
            return_value=llm_result,
        ) as complaint_llm,
        patch(
            "services.municipio_ai_classifier.infer_reclamo_category",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_priority",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_operational_signals",
            return_value=None,
        ),
        patch(
            "services.municipio_ai_classifier.infer_reclamo_sentiment",
            return_value=None,
        ),
    ):
        details = extract_reclamo_details_from_text(
            VOICE_CLAIM_WITH_CALLBACK,
            [{"texto": "Luminaria"}],
            default_localidad="Palmira",
            default_provincia="Mendoza",
        )

    complaint_llm.assert_called_once()
    assert details["solicita_llamada"] is True
    assert details["motivo_llamada"] == "Quiere coordinar el reclamo por teléfono."

    context = {
        "channel": "whatsapp",
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "contacto_usuario": {
                    "nombre": "Marcelo",
                    "dni": "32877851",
                    "email": "marcelo@example.com",
                    "telefono": "+5492613168608",
                }
            }
        },
    }
    with patch(
        "services.municipio_responder.extract_reclamo_details_from_text",
        return_value=details,
    ):
        response = _try_start_reclamo_from_text(
            VOICE_CLAIM_WITH_CALLBACK,
            context,
            chat_db_context=None,
            default_localidad="Palmira",
            default_provincia="Mendoza",
        )

    flow = context["chat_db_context_data"][CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]
    assert response is not None
    assert flow["datos_reclamo"]["solicita_llamada"] is True
    assert (
        flow["datos_reclamo"]["motivo_llamada"]
        == "Quiere coordinar el reclamo por teléfono."
    )


def test_callback_extraction_uses_llm_reason_but_requires_explicit_user_request():
    with patch(
        "services.llm_utils.robust_chat",
        return_value=json.dumps(
            {
                "tipo_problema": "Luminaria",
                "descripcion_problema": "Poste caído en Palmira.",
                "solicita_llamada": True,
                "motivo_llamada": "Coordinar atención por el poste caído.",
            }
        ),
    ):
        result = extract_complaint_details_llm(VOICE_CLAIM_WITH_CALLBACK)

    assert result["solicita_llamada"] is True
    assert result["motivo_llamada"] == "Coordinar atención por el poste caído."

    with patch(
        "services.llm_utils.robust_chat",
        return_value=json.dumps(
            {
                "descripcion_problema": "Consulta por teléfono municipal.",
                "solicita_llamada": True,
                "motivo_llamada": "Llamar al vecino.",
            }
        ),
    ):
        informational = extract_complaint_details_llm(
            "¿A qué número llamo yo para consultar el estado?"
        )

    assert "solicita_llamada" not in informational
    assert "motivo_llamada" not in informational


def test_nada_mas_in_photo_state_skips_photo_and_reaches_confirmation():
    handler = _handler_for(ReclamoState.ESPERANDO_FOTO, _complete_claim())

    response = handler.handle("Nada más.", {"pregunta": "Nada más."})

    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert handler.flow_context["datos_reclamo"]["foto_url"] is None
    assert "confirm" in response["message_body"].lower()
    assert "no entend" not in response["message_body"].lower()


def test_pin_question_during_confirmation_keeps_same_draft_without_ticket():
    claim = _complete_claim()
    expected_claim = deepcopy(claim)
    handler = _handler_for(ReclamoState.ESPERANDO_CONFIRMACION, claim)

    with patch("services.municipio_responder.CrearReclamoActionHandler") as action_handler:
        response = handler.handle(
            "¿Y mi pin para poder preguntar en el futuro?",
            {"pregunta": "¿Y mi pin para poder preguntar en el futuro?"},
        )

    action_handler.assert_not_called()
    assert handler.flow_context["state"] == ReclamoState.ESPERANDO_CONFIRMACION.name
    assert handler.flow_context["datos_reclamo"] == expected_claim
    assert "pin" in response["message_body"].lower()
    assert "todavía no" in response["message_body"].lower()
    assert response["options_list"]


def test_confirmed_audio_claim_hands_callback_request_to_ticket_action():
    handler = _handler_for(
        ReclamoState.ESPERANDO_CONFIRMACION,
        _complete_claim(
            solicita_llamada=True,
            motivo_llamada="Quiere coordinar el reclamo por teléfono.",
        ),
    )

    with patch("services.municipio_responder.CrearReclamoActionHandler") as action_handler:
        action_handler.return_value.execute.return_value = {
            "success": True,
            "message_body": "Reclamo recibido.",
            "message_type": "text",
            "data": {
                "ticket_id": 401,
                "nro_ticket": "M-401",
                "consulta_pin": "167779",
            },
        }
        handler.handle("confirmar", {"pregunta": "confirmar"})

    action_data = action_handler.return_value.execute.call_args.args[0]
    assert action_data["solicita_llamada"] is True
    assert action_data["motivo_llamada"] == "Quiere coordinar el reclamo por teléfono."


def test_ticket_action_persists_callback_as_pending_crm_metadata(client):
    owner = SimpleNamespace(
        id=91,
        municipio_id=91,
        tenant_slug=None,
        link_web=None,
        telefono=None,
        horario=None,
    )
    context = {
        "channel": "web",
        "user_obj": owner,
        "anon_id": "callback-regression-user",
        "municipio_config_actual": {
            "ciudad": "Palmira",
            "base_chat_url": "https://www.chatboc.ar/chat",
        },
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: {}},
    }
    action_data = {
        "categoria": "Luminaria",
        "descripcion": "Poste caído en el barrio.",
        "ubicacion": "Don Bosco 56, Palmira",
        "usuario": "Marcelo",
        "dni": "32877851",
        "email": "marcelo@example.com",
        "telefono": "+5492613168608",
        "solicita_llamada": True,
        "motivo_llamada": "Quiere coordinar el reclamo por teléfono.",
    }

    with (
        patch("services.actions.municipio_actions.validar_email", return_value=True),
        patch("services.actions.municipio_actions.validar_telefono", return_value=True),
        patch(
            "services.actions.municipio_actions.formatear_telefono_e164",
            return_value="+5492613168608",
        ),
        patch(
            "services.actions.municipio_actions.parse_direccion",
            return_value={"localidad": "Palmira"},
        ),
        patch(
            "services.actions.municipio_actions.cargar_configuracion_municipio",
            return_value={},
        ),
        patch(
            "services.actions.municipio_actions.formatear_ticket_respuesta",
            return_value=("Reclamo recibido.", []),
        ),
        patch(
            "services.actions.municipio_actions.promo_service.build_ticket_promo_section",
            return_value=None,
        ),
        patch(
            "services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket",
            return_value={"id": 401, "nro_ticket": "401"},
        ) as create_ticket,
    ):
        response = CrearReclamoActionHandler(context).execute(action_data)

    ticket_data = create_ticket.call_args.kwargs["ticket_data"]
    callback = ticket_data["datos_extra"]["callback_request"]
    assert callback == {
        "requested": True,
        "channel": "phone",
        "status": "pending",
        "reason": "Quiere coordinar el reclamo por teléfono.",
    }
    assert response["data"]["callback_request"] == callback
    assert response["contexto_actualizado"]["pending_callback_request"]["ticket_nro"] == "M-401"
