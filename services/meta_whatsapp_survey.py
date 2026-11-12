"""Native municipal survey turns within the existing conversation transaction."""
from copy import deepcopy
import hashlib
import re
from urllib.parse import quote

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from models import TenantProfile, db
from services.constants import CONTEXTO_MUNICIPIO
from services.encuestas_service import (
    EncuestaError, _acquire_encuesta_response_guard, _acquire_encuesta_write_guard,
    get_public_encuesta, list_public_encuestas_for_tenant,
)
from services.whatsapp_conversation_uow import ConversationUnitOfWork, ConversationUnitOfWorkError
from services.whatsapp_survey_conversation import (
    WHATSAPP_SURVEY_FLOW_STATE_KEY, _resolve_authoritative_scope, _tenant_scoped_public_url,
    clear_whatsapp_survey_flow, handle_whatsapp_survey_flow_turn,
    has_active_whatsapp_survey_flow, start_whatsapp_survey_flow,
)


STATE_KEY = "meta_survey_navigation_v1"
COMMANDS = {"encuestas", "encuesta", "votaciones", "votación", "votacion", "sondeos"}
EXIT_COMMANDS = {"cancelar", "menu", "menú", "menú principal", "menu principal"}
PAGE_SIZE = 5


def _scope(unit):
    claim = unit.claim
    return {"tenant_id": claim.tenant_id, "chat_session_id": claim.chat_session_id,
            "binding_id": claim.session_identity_binding_id,
            "binding_version": claim.session_identity_version}


def _emit_text(sink, text):
    # Each chunk is an ordered native text item. Slicing retains every Unicode
    # code point and the full disclosure before its acceptance prompt.
    if not isinstance(text, str) or not text.strip():
        raise ConversationUnitOfWorkError("meta_survey_reply_invalid")
    for start in range(0, len(text), 4000):
        sink.emit_text(text[start:start + 4000])


def _emit_payload(sink, payload):
    disclosure = payload.get("_whatsapp_required_disclosure")
    if disclosure is not None:
        body = disclosure.get("body") if isinstance(disclosure, dict) else None
        if (not isinstance(body, str) or not body.strip()
                or disclosure.get("contract_version") != "whatsapp.required_disclosure.v1"
                or disclosure.get("sha256") != hashlib.sha256(body.encode()).hexdigest()):
            raise ConversationUnitOfWorkError("meta_survey_disclosure_invalid")
        _emit_text(sink, body)
        _emit_text(sink, "Respondé 1 para aceptar o 2 para rechazar. También podés escribir Cancelar.")
        return
    body = payload.get("message_body")
    _emit_text(sink, body)
    if not payload.get("survey_progress") and not payload.get("response_persisted"):
        _emit_text(sink, "Escribí Encuestas para volver al listado o Cancelar para salir.")


def _list(context, navigation, sink, page, unit):
    _, tenant_id = _resolve_authoritative_scope(context)

    navigation.clear()
    navigation.update(scope=_scope(unit), page=page)
    rows = list_public_encuestas_for_tenant(tenant_id, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
    shown = rows[:PAGE_SIZE]
    navigation["choices"] = [{"id": survey.id, "slug": slug} for survey, slug in shown]
    navigation["more"] = len(rows) > PAGE_SIZE
    if not shown:
        _emit_text(sink, "Por el momento no hay encuestas activas en este municipio.")
        return
    lines = ["Encuestas de participación ciudadana", ""]
    for index, (survey, _) in enumerate(shown, start=1):
        title = " ".join(survey.titulo.split())
        lines.append(f"{index}. {title}")
    lines.extend(("", "Respondé con el número de la encuesta. Escribí Cancelar para salir."))
    if navigation["more"]:
        lines.append("Escribí Siguiente para ver más encuestas.")
    if page > 1:
        lines.append("Escribí Anterior para volver a la página previa.")
    _emit_text(sink, "\n".join(lines))


def _public_survey(slug, tenant_id):
    try:
        return get_public_encuesta(slug, preferred_tenant_id=tenant_id, require_tenant_match=True)
    except EncuestaError as exc:
        if exc.status_code in {403, 404, 410}:
            return None
        raise


def try_execute_meta_survey(turn, sink):
    unit = turn.unit_of_work
    if not isinstance(unit, ConversationUnitOfWork):
        raise ConversationUnitOfWorkError("meta_survey_owner_required")
    unit.require_turn(turn)
    if unit.claim.message_kind != "text" or turn.tenant_profile.tipo != "municipio":
        return False
    session = turn.session_context
    original = session.context_data
    if not isinstance(original, dict):
        unit.fail("meta_survey_context_invalid")
    municipal = original.get(CONTEXTO_MUNICIPIO) or {}
    text = turn.message_body.strip()
    command = text.casefold().strip(".!? ")
    if (command not in COMMANDS and STATE_KEY not in municipal
            and WHATSAPP_SURVEY_FLOW_STATE_KEY not in municipal):
        return False

    db.session.refresh(session, with_for_update=True)
    unit.require_turn(turn)
    if not isinstance(session.context_data, dict):
        unit.fail("meta_survey_context_invalid")
    data = deepcopy(session.context_data)
    municipal = data.setdefault(CONTEXTO_MUNICIPIO, {})
    navigation = municipal.setdefault(STATE_KEY, {})
    if not isinstance(navigation, dict):
        unit.fail("meta_survey_navigation_invalid")

    state = None
    choice = None
    if command not in COMMANDS and command not in EXIT_COMMANDS and navigation.get("scope") == _scope(unit):
        persisted_state = municipal.get(WHATSAPP_SURVEY_FLOW_STATE_KEY)
        if isinstance(persisted_state, dict):
            state = persisted_state
        elif re.fullmatch(r"[1-5]", text) and int(text) <= len(navigation.get("choices") or []):
            choice = navigation["choices"][int(text) - 1]

    # Territorial review locks the instrument first and the tenant second.
    # Resolve only the pinned target before locking, then reread eligibility
    # with both locks held. Terminal turns take the write lock from the start.
    locked_survey_id = None
    if state is not None or choice is not None:
        slug = state["public_slug"] if state is not None else choice["slug"]
        expected_survey_id = state["survey_id"] if state is not None else choice["id"]
        target = _public_survey(slug, turn.tenant_id)
        if target is not None and target.id == expected_survey_id:
            for_submission = state is not None and (state.get("step") == "retry" or (
                state.get("step") == "question"
                and state.get("question_index") == len(state.get("question_ids") or ()) - 1))
            acquire_guard = _acquire_encuesta_write_guard if for_submission else _acquire_encuesta_response_guard
            try:
                locked_survey_id = acquire_guard(target.id).id
            except EncuestaError as exc:
                if exc.status_code != 404:
                    raise

    db.session.execute(select(TenantProfile).where(TenantProfile.id == turn.tenant_id)
                       .with_for_update(read=True, of=TenantProfile)
                       .execution_options(populate_existing=True)).scalar_one()
    unit.require_turn(turn)
    if turn.tenant_profile.tipo != "municipio":
        unit.fail("meta_survey_tenant_type_changed")
    participant = unit.contact_identity
    context = {"tenant_profile": turn.tenant_profile, "tenant_id": turn.tenant_id,
               "viewer_user_obj": None, "anon_id": participant, "phone": participant, "channel": "whatsapp",
               "chat_db_context_data": data, "chat_db_context_obj": session,
               "chat_session_uuid": turn.chat_session_id,
               "municipio_config_actual": turn.tenant_profile.configuracion or {}}
    survey_available = False
    if locked_survey_id is not None:
        current = _public_survey(slug, turn.tenant_id)
        survey_available = current is not None and current.id == locked_survey_id

    from services.municipio_responder import _resolve_encuestas_toggle

    if _resolve_encuestas_toggle(context) is False:
        clear_whatsapp_survey_flow(context)
        municipal.pop(STATE_KEY, None)
        _emit_text(sink, "Las encuestas todavía no están habilitadas en este municipio.")
    elif command in EXIT_COMMANDS:
        clear_whatsapp_survey_flow(context)
        municipal.pop(STATE_KEY, None)
        _emit_text(sink, "Saliste de la participación. Escribí Encuestas para volver al listado.")
    elif command in COMMANDS:
        claim_flow = municipal.get("reclamo_flow_v2") or {}
        if claim_flow.get("meta_text_draft_v1") is True and claim_flow.get("state"):
            municipal.pop(STATE_KEY, None)
            _emit_text(sink, "Tenés un reclamo en curso. Terminalo o cancelalo antes de elegir una encuesta.")
        else:
            clear_whatsapp_survey_flow(context)
            _list(context, navigation, sink, 1, unit)
            if not navigation.get("choices"):
                municipal.pop(STATE_KEY, None)
    elif navigation.get("scope") != _scope(unit):
        clear_whatsapp_survey_flow(context)
        municipal.pop(STATE_KEY, None)
        _emit_text(sink, "Esta participación ya no está activa. Escribí Encuestas para comenzar de nuevo.")
    elif (state is not None or choice is not None) and not survey_available:
        clear_whatsapp_survey_flow(context)
        municipal.pop(STATE_KEY, None)
        _emit_text(sink, "Esta encuesta ya no está disponible. Escribí Encuestas para ver las vigentes.")
    elif state is not None:
        payload = handle_whatsapp_survey_flow_turn(
            context, text=text, unit_of_work=unit, conversation_turn_id=unit.claim.turn_id)
        if not isinstance(payload, dict):
            unit.fail("meta_survey_turn_unhandled")
        _emit_payload(sink, payload)
        if not has_active_whatsapp_survey_flow(context):
            municipal.pop(STATE_KEY, None)
    elif command == "siguiente" and navigation.get("more") is True:
        _list(context, navigation, sink, int(navigation["page"]) + 1, unit)
    elif command == "anterior" and int(navigation.get("page") or 1) > 1:
        _list(context, navigation, sink, int(navigation["page"]) - 1, unit)
    elif choice is not None:
        base = str(current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
                   or "https://www.chatboc.ar").rstrip("/")
        public_url = _tenant_scoped_public_url(f"{base}/e/{quote(choice['slug'], safe='')}", turn.tenant_profile)
        _emit_payload(sink, start_whatsapp_survey_flow(context, choice["slug"], public_url=public_url))
        if not has_active_whatsapp_survey_flow(context):
            municipal.pop(STATE_KEY, None)
    else:
        _emit_text(sink, "Elegí el número de una encuesta del listado o escribí Encuestas para actualizarlo.")

    unit.require_turn(turn)
    session.context_data = data
    flag_modified(session, "context_data")
    return True
