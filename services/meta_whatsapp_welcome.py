"""Native municipal text welcome inside the claimed conversation transaction.

This deliberately selects only text. It does not reinterpret legacy media or
templates, reset a flow, send to a provider, or grant any recipient authority.
"""
from copy import deepcopy

from services.common_utils import _get_main_menu_payload
from services.constants import CONTEXTO_MUNICIPIO
from services.meta_whatsapp_outbox import MetaReplyContent
from services.meta_whatsapp_survey import STATE_KEY as SURVEY_NAVIGATION_KEY
from services.whatsapp_conversation_uow import ConversationUnitOfWork, ConversationUnitOfWorkError
from services.whatsapp_survey_conversation import WHATSAPP_SURVEY_FLOW_STATE_KEY


def try_execute_meta_welcome(turn, sink):
    """Handle existing greeting/menu commands without consuming active answers."""
    from services.municipio_responder import (
        RETURN_TO_MAIN_MENU_NORMALIZED, SIMPLE_GREETINGS,
        _resolve_encuestas_toggle, normalizar_texto,
    )

    unit = turn.unit_of_work
    if not isinstance(unit, ConversationUnitOfWork):
        raise ConversationUnitOfWorkError("meta_welcome_owner_required")
    unit.require_turn(turn)
    if unit.claim.message_kind != "text":
        return False
    body = turn.message_body
    if not isinstance(body, str) or body != unit.claim.payload["input"]["text"]:
        unit.fail("meta_welcome_input_mismatch")
    normalized = normalizar_texto(body).strip()
    if normalized not in SIMPLE_GREETINGS | RETURN_TO_MAIN_MENU_NORMALIZED:
        return False

    tenant = turn.tenant_profile
    if (tenant.tipo != "municipio" or tenant.municipio_id != turn.owner_user.id
            or turn.owner_user.tipo_chat != "municipio"):
        unit.fail("meta_welcome_scope_invalid")
    data = turn.session_context.context_data
    if not isinstance(data, dict):
        unit.fail("meta_welcome_context_invalid")
    municipal = data.get(CONTEXTO_MUNICIPIO) or {}
    if not isinstance(municipal, dict):
        unit.fail("meta_welcome_context_invalid")
    flow = municipal.get("reclamo_flow_v2") or {}
    if not isinstance(flow, dict):
        unit.fail("meta_welcome_context_invalid")
    survey_active = (SURVEY_NAVIGATION_KEY in municipal
                     or WHATSAPP_SURVEY_FLOW_STATE_KEY in municipal)
    if normalized in RETURN_TO_MAIN_MENU_NORMALIZED and (survey_active or flow.get("state")):
        # Keep the existing survey/draft exit commands with their own handlers.
        return False
    # The common producer receives a detached copy: a greeting must never clear
    # an active draft, survey, follow-up or identity-bearing session state.
    context = {
        "tenant_profile": tenant,
        "tenant_id": turn.tenant_id,
        "user_obj": turn.owner_user,
        "viewer_user_obj": None,
        "channel": "whatsapp",
        "chat_db_context_data": deepcopy(data),
        "municipio_config_actual": deepcopy(tenant.configuracion)
        if isinstance(tenant.configuracion, dict) else {},
    }
    actions = ("iniciar_reclamo",)
    if _resolve_encuestas_toggle(context) is not False:
        actions += ("mostrar_menu_encuestas",)
    payload = _get_main_menu_payload(context, native_text_actions=actions)
    options = payload["options_list"]
    if survey_active:
        menu = ("Tu participación sigue en curso. Respondé la opción o pregunta anterior "
                "para continuar, o escribí Cancelar para salir.")
    elif flow.get("state") and flow.get("meta_text_draft_v1") is True:
        menu = ("Conservo el borrador de tu reclamo. Respondé la consulta anterior "
                "para continuar, o escribí Cancelar.")
    elif flow.get("state") or data.get("pending_sensitive_action"):
        # An unported state cannot be reset or advertised as a native flow.
        menu = "Conservo tu gestión pendiente. Este saludo no cambió los datos que ya cargaste."
    else:
        menu = "Podés usar estas opciones:\n\n" + "\n".join(
            f"• Escribí {option['texto']}." for option in options
        )
    # Do not read/clear WELCOME_MEDIA_URL: this is the explicitly text-only
    # canary. Native callers selecting media still require verified MediaRefs.
    sink.emit_welcome(greeting=payload["message_body"], menu=MetaReplyContent("text", menu))
    unit.require_turn(turn)
    return True
