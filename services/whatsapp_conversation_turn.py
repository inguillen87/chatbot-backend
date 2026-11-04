"""Common post-auth conversation stages, not a provider ingress.

The legacy adapter supplies its current response/error hooks. This module never
imports routes, validates a provider signature, creates a provider Client or
chooses a tenant from user text. Existing business handler commits are retained;
this movement does not assert global transaction atomicity.
"""
from __future__ import annotations

from services.contact_service import read_contact_projection, store_contact_projection, current_contact_phone
from copy import deepcopy
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from flask import current_app
from sqlalchemy import or_
from models import ArchivoAdjunto, ChatSessionContext, MunicipioTicket, PymeTicket, TenantProfile, User
from extensions import db
from utils.db_utils import safe_flag_modified
from utils.response_utils import normalize_response_payload
from services.categorias_municipio import normalizar_texto
from services.constants import CONTEXTO_MUNICIPIO
from services.reclamo_turn_semantics import ReclamoTurnIntent, classify_reclamo_confirmation_turn
from services.ticket_service import build_whatsapp_ticket_effect_key, servicio_tickets
from services.tenant_ticket_scope import (TicketTenantScopeError,
    resolve_unique_tenant_for_owner, scoped_municipio_ticket_query, tenant_owner_ids)
from services.whatsapp_receipts import CLAIM_FOLLOWUP_WINDOW_SECONDS
from services.contact_intake import missing_contact_fields, resolve_contact_snapshot
from services.crm_intelligence import record_contact_interaction, resolve_or_create_contact
from services.whatsapp_inbound_content import is_audio_media_type
from services.whatsapp_conversation_contract import (ConversationTurn, ConversationSink,
    ConversationSelection, ConversationPreparation, ConversationFinish)

LIVE_CHAT_STATES = {"esperando_agente_en_vivo", "en_proceso", "en_vivo"}


MUNICIPAL_TICKET_CLOSED_STATES = {
    "anulado",
    "archivado",
    "archived",
    "canceled",
    "cancelada",
    "cancelado",
    "cancelled",
    "cerrada",
    "cerrado",
    "closed",
    "completada",
    "completado",
    "completed",
    "finalizada",
    "finalizado",
    "resolved",
    "resuelta",
    "resuelto",
}


SENSITIVE_ACTION_CONFIRM_ACCEPT = {"1", "si", "sí", "confirmar", "ok", "dale"}


SENSITIVE_ACTION_CONFIRM_REJECT = {"2", "no", "cancelar", "menu", "menú"}


SENSITIVE_MENU_ACTIONS = {
    "enviar_sugerencia",
    "iniciar_sugerencia",
    "crear_sugerencia",
}


def _resolve_whatsapp_menu_selection(
    message_body: str, context_data: dict, *, expecting_free_info: bool = False,
) -> tuple[Optional[dict], Optional[str]]:
    """Resolve legacy menus; institutional replies keep their original text.

    Only the formatter's persisted scope selects this path. The knowledge
    service validates its own displayed node/revision and exact reply code.
    """
    if context_data.get("last_options_scope") == "institutional_knowledge":
        return None, None
    last_options = context_data.get("last_options_sent")
    if not last_options or expecting_free_info:
        return None, None
    selected_option = None
    if message_body.isdigit():
        idx = int(message_body) - 1
        if 0 <= idx < len(last_options):
            selected_option = last_options[idx]
    else:
        normalized_body = (message_body or "").strip().lower()
        for option in last_options:
            option_text = (option.get("texto") or "").strip().lower()
            option_action = (option.get("action_id") or option.get("id") or "").strip().lower()
            if normalized_body and normalized_body in {option_text, option_action}:
                selected_option = option
                break
    if selected_option is None:
        return None, None
    action_id = (selected_option.get("action_id") or selected_option.get("id")
                 or selected_option.get("id_accion") or selected_option.get("category_name")
                 or selected_option.get("texto"))
    return selected_option, action_id


def _find_live_chat_ticket(
    owner_user: Optional[User],
    end_user: Optional[User],
    anon_id: Optional[str],
    tenant_profile: Optional[TenantProfile] = None,
    conversation_context: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[MunicipioTicket | PymeTicket]]:
    if not owner_user:
        return None, None

    tipo_chat = getattr(owner_user, "tipo_chat", None)
    context_data = (
        conversation_context if isinstance(conversation_context, dict) else {}
    )
    context_ticket_type = str(context_data.get("tipo_ticket") or "").strip().lower()
    try:
        context_ticket_id = int(str(context_data.get("ticket_id") or "").strip())
    except (TypeError, ValueError):
        context_ticket_id = 0

    # A live-chat turn is authorized by the exact binding persisted when the
    # handoff ticket was created. A room name or "latest open ticket" lookup is
    # not an authorization boundary and becomes ambiguous as soon as the same
    # contact has two active cases.
    if (
        tipo_chat not in {"municipio", "pyme"}
        or context_ticket_type != tipo_chat
        or context_ticket_id <= 0
    ):
        return tipo_chat if tipo_chat in {"municipio", "pyme"} else None, None

    if tipo_chat == "municipio":
        resolved_tenant = tenant_profile
        if resolved_tenant is None:
            owner_id = getattr(owner_user, "municipio_id", None) or getattr(owner_user, "id", None)
            try:
                resolution = resolve_unique_tenant_for_owner(owner_id)
            except TicketTenantScopeError:
                resolution = None
            if resolution is not None and resolution.status == "unique":
                resolved_tenant = resolution.tenant
        if resolved_tenant is None:
            current_app.logger.warning(
                "[WHATSAPP_WEBHOOK] Live chat municipal lookup rejected without exact tenant owner=%s",
                getattr(owner_user, "id", None),
            )
            return "municipio", None
        query = scoped_municipio_ticket_query(resolved_tenant).filter(
            MunicipioTicket.id == context_ticket_id,
            MunicipioTicket.estado.in_(LIVE_CHAT_STATES)
        )
        if end_user:
            identity_filters = [MunicipioTicket.user_id == end_user.id]
            if anon_id:
                identity_filters.append(MunicipioTicket.anon_id == anon_id)
            query = query.filter(or_(*identity_filters))
        elif anon_id:
            query = query.filter(MunicipioTicket.anon_id == anon_id)
        else:
            return "municipio", None
        return "municipio", query.first()

    if tipo_chat == "pyme":
        resolved_tenant = tenant_profile or _tenant_profile_for_user(owner_user)
        tenant_id = getattr(resolved_tenant, "id", None)
        if not tenant_id:
            current_app.logger.warning(
                "[WHATSAPP_WEBHOOK] Live chat PyME ticket lookup rejected without exact tenant owner=%s",
                getattr(owner_user, "id", None),
            )
            return "pyme", None
        query = PymeTicket.query.filter(
            PymeTicket.id == context_ticket_id,
            PymeTicket.estado.in_(LIVE_CHAT_STATES),
        )
        if end_user:
            identity_filters = [PymeTicket.user_id == end_user.id]
            if anon_id:
                identity_filters.append(PymeTicket.anon_id == anon_id)
            query = query.filter(or_(*identity_filters))
        elif anon_id:
            query = query.filter(PymeTicket.anon_id == anon_id)
        else:
            return "pyme", None
        query = query.filter(PymeTicket.tenant_id == tenant_id)
        return "pyme", query.first()

    return None, None


def _normalize_ticket_reference(ticket_ref: Optional[str]) -> List[str]:
    if not ticket_ref:
        return []

    cleaned = str(ticket_ref).strip().upper().replace("#", "")
    if not cleaned:
        return []

    candidates: List[str] = []
    digit_match = re.search(r"\d+", cleaned)
    if cleaned.isdigit():
        candidates.append(cleaned)
        candidates.append(cleaned.lstrip("0") or cleaned)
        padded = cleaned.zfill(6)
        candidates.append(padded)
        for prefix in ("M-", "S-", "P-"):
            candidates.append(f"{prefix}{padded}")
    else:
        candidates.append(cleaned)
        if digit_match:
            number = digit_match.group(0)
            padded = number.zfill(6)
            candidates.extend(
                [
                    padded,
                    number,
                    f"M-{padded}",
                    f"S-{padded}",
                    f"P-{padded}",
                ]
            )
        match = re.match(r"([A-Z]+)-?(\d+)", cleaned)
        if match:
            prefix, number = match.groups()
            padded = number.zfill(6)
            candidates.extend([f"{prefix}-{padded}", padded, number])

    seen: Set[str] = set()
    unique_candidates = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique_candidates.append(candidate)
    return unique_candidates


def _find_municipio_ticket_for_reference(
    ticket_ref: Optional[str],
    tenant_id: Optional[int] = None,
    municipio_id: Optional[int] = None,
    anon_id: Optional[str] = None,
) -> Optional[MunicipioTicket]:
    candidates = _normalize_ticket_reference(ticket_ref)
    if not candidates:
        return None

    tenant = None
    if tenant_id:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_id))
        except (TypeError, ValueError):
            return None
    elif municipio_id:
        try:
            resolution = resolve_unique_tenant_for_owner(municipio_id)
        except TicketTenantScopeError:
            return None
        tenant = resolution.tenant if resolution.status == "unique" else None
    if tenant is None:
        return None
    if municipio_id:
        try:
            normalized_owner_id = int(municipio_id)
        except (TypeError, ValueError):
            return None
        if normalized_owner_id not in tenant_owner_ids(tenant):
            return None

    base_query = scoped_municipio_ticket_query(tenant)

    ticket = base_query.filter(MunicipioTicket.nro_ticket.in_(candidates)).first()
    if ticket:
        return ticket

    if anon_id:
        anon_query = base_query.filter(MunicipioTicket.anon_id == anon_id)
        ticket = anon_query.filter(MunicipioTicket.nro_ticket.in_(candidates)).first()
        if ticket:
            return ticket

    return None


def _active_municipal_ticket_followup(
    context_data: Any,
    *,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return a bounded, ticket-specific follow-up receipt from session state."""

    if not isinstance(context_data, dict):
        return None
    now_value = time.time() if now_ts is None else float(now_ts)
    raw_followup = context_data.get("active_ticket_followup")
    followup = dict(raw_followup) if isinstance(raw_followup, dict) else {}

    # Backwards compatibility for receipts created before the structured
    # follow-up context existed.  The old photo window is already bounded and
    # therefore safe to reuse; ``last_ticket_code`` alone is never enough.
    if not followup:
        legacy_until = context_data.get("awaiting_ticket_photo_until")
        legacy_active = bool(context_data.get("awaiting_ticket_photo"))
        if legacy_active and isinstance(legacy_until, (int, float)):
            followup = {
                "ticket_nro": (
                    context_data.get("last_ticket_code")
                    or context_data.get("awaiting_photo_for_ticket")
                ),
                "consulta_pin": context_data.get("latest_ticket_pin"),
                "tracking_url": context_data.get("latest_tracking_url"),
                "until": legacy_until,
            }

    ticket_nro = str(followup.get("ticket_nro") or "").strip()
    until = followup.get("until")
    if not ticket_nro or not isinstance(until, (int, float)) or now_value > float(until):
        return None
    followup["ticket_nro"] = ticket_nro
    return followup


def _clear_municipal_ticket_followup(context_data: Any) -> None:
    if not isinstance(context_data, dict):
        return
    for key in (
        "active_ticket_followup",
        "awaiting_photo_for_ticket",
        "awaiting_ticket_photo",
        "awaiting_ticket_photo_until",
        "pending_attachment_id",
        "pending_ticket_code",
        "pending_attachment_until",
    ):
        context_data.pop(key, None)


def _municipal_ticket_is_open(ticket: Optional[MunicipioTicket]) -> bool:
    if ticket is None:
        return False
    state = str(getattr(ticket, "estado", "") or "").strip().lower()
    return state not in MUNICIPAL_TICKET_CLOSED_STATES


def _build_municipal_followup_tracking_text(
    followup: Dict[str, Any],
    ticket: Optional[MunicipioTicket],
) -> str:
    ticket_nro = str(
        followup.get("ticket_nro") or getattr(ticket, "nro_ticket", "") or ""
    ).strip()
    pin = str(
        followup.get("consulta_pin") or getattr(ticket, "consulta_pin", "") or ""
    ).strip()
    tracking_url = str(followup.get("tracking_url") or "").strip()
    lines = [f"🔎 *Reclamo {ticket_nro}*"]
    if pin:
        lines.append(f"*PIN:* {pin}")
    if tracking_url:
        lines.append(f"*Seguimiento:* {tracking_url}")
    lines.append("Podés seguir respondiendo acá para sumar información al mismo reclamo.")
    return "\n".join(lines)


def _handle_recent_municipal_ticket_followup(
    *,
    session_context: ChatSessionContext,
    client_user: Optional[User],
    tenant_profile: Optional[TenantProfile],
    end_user: Optional[User],
    from_number_cleaned: str,
    message_body: str,
    action: Optional[str],
    sink: ConversationSink,
    durable_claim: Any = None,
    now_ts: Optional[float] = None,
) -> Optional[Tuple[str, int]]:
    """Handle one safe text turn against the newly-created open ticket.

    The structured, expiring receipt is the only implicit binding.  A clear
    new-claim/menu intent exits this mode and continues through the normal LLM
    orchestrator.  Everything executed here is tenant-scoped and validated
    against the persisted ticket before a comment is created.
    """

    if not session_context or not isinstance(session_context.context_data, dict):
        return None
    if not client_user or getattr(client_user, "tipo_chat", None) != "municipio":
        return None

    context_data = session_context.context_data
    municipio_ctx = context_data.get(CONTEXTO_MUNICIPIO)
    if isinstance(municipio_ctx, dict) and (
        (municipio_ctx.get("reclamo_flow_v2") or {}).get("state")
    ):
        return None

    followup = _active_municipal_ticket_followup(context_data, now_ts=now_ts)
    if not followup:
        # Expired structured windows must not linger and later bind evidence to
        # an unrelated ticket.
        if isinstance(context_data.get("active_ticket_followup"), dict):
            _clear_municipal_ticket_followup(context_data)
            safe_flag_modified(session_context, "context_data")
            db.session.add(session_context)
            db.session.commit()
        return None

    decision = classify_reclamo_confirmation_turn(message_body, action=action)
    raw_msg = normalizar_texto(message_body or "")
    navigation_keywords = {
        "menu",
        "menú",
        "1",
        "2",
        "3",
        "4",
        "4. menú",
        "4. menu",
        "2. menú",
        "2. menu",
        "volver",
        "inicio",
        "principal",
        "encuesta",
        "encuestas",
        "hola",
        "buenas",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
    }
    is_nav_action = action in {
        "menu_principal",
        "mostrar_menu_reclamos",
        "mostrar_menu_encuestas",
        "consultar_estado_reclamo",
    }
    if (
        decision.intent in {ReclamoTurnIntent.NEW_CLAIM, ReclamoTurnIntent.CANCEL}
        or raw_msg in navigation_keywords
        or is_nav_action
    ):
        _clear_municipal_ticket_followup(context_data)
        context_data.pop("last_options_sent", None)
        context_data.pop("pending_sensitive_action", None)
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        db.session.commit()
        return None

    ticket_tenant_id = getattr(tenant_profile, "id", None) or session_context.tenant_id
    municipio_owner_id = (
        getattr(client_user, "municipio_id", None) or getattr(client_user, "id", None)
    )
    ticket = _find_municipio_ticket_for_reference(
        followup["ticket_nro"],
        tenant_id=ticket_tenant_id,
        municipio_id=municipio_owner_id,
        anon_id=from_number_cleaned,
    )
    if not _municipal_ticket_is_open(ticket):
        _clear_municipal_ticket_followup(context_data)
        context_data.pop("last_options_sent", None)
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        db.session.commit()
        return None

    context_data.pop("last_options_sent", None)
    context_data.pop("pending_sensitive_action", None)
    safe_flag_modified(session_context, "context_data")

    if decision.intent is ReclamoTurnIntent.FOLLOW_UP_QUESTION:
        db.session.add(session_context)
        db.session.commit()
        if sink.enabled:
            sink.emit_text(_build_municipal_followup_tracking_text(followup, ticket))
        return "OK", 200

    if decision.intent is ReclamoTurnIntent.ADD_PHOTO:
        db.session.add(session_context)
        db.session.commit()
        if sink.enabled:
            sink.emit_text(f"Enviá la foto o el audio y lo voy a sumar al reclamo "
                    f"*{followup['ticket_nro']}*.")
        return "OK", 200

    if decision.intent is ReclamoTurnIntent.EDIT and not decision.corrections:
        db.session.add(session_context)
        db.session.commit()
        if sink.enabled:
            sink.emit_text("Decime qué dato querés corregir. Lo voy a registrar como "
                    f"actualización del reclamo *{followup['ticket_nro']}*.")
        return "OK", 200

    clean_message = re.sub(r"\s+", " ", str(message_body or "")).strip()
    if not clean_message:
        return None

    if decision.reason == "isolated_emoji" or decision.intent is ReclamoTurnIntent.CONFIRM:
        db.session.add(session_context)
        db.session.commit()
        if sink.enabled:
            sink.emit_text(f"Recibí tu mensaje. El reclamo *{followup['ticket_nro']}* "
                    "sigue abierto.")
        return "OK", 200

    comment = servicio_tickets.crear_comentario(
        ticket_id=ticket.id,
        tipo_ticket="municipio",
        comentario_data={
            "comentario": clean_message[:4000],
            "user_id": getattr(end_user, "id", None),
            "anon_id": None if end_user else from_number_cleaned,
            "es_admin": False,
            "origen": "whatsapp",
            "estado_ticket": ticket.estado,
            "emit_notifications": False,
            "emit_socket": False,
        },
        **_durable_whatsapp_ticket_effect_kwargs(
            durable_claim,
            ticket_tenant_id,
            f"ticket_followup_comment:{ticket.id}",
        ),
    )
    if comment is None:
        raise RuntimeError("municipal_ticket_followup_comment_not_persisted")
    db.session.add(session_context)
    db.session.commit()
    if sink.enabled:
        sink.emit_text(f"✅ Sumé tu mensaje al reclamo *{followup['ticket_nro']}*.")
    return "OK", 200


def _durable_whatsapp_ticket_effect_kwargs(
    durable_claim: Any,
    tenant_id: Any,
    effect: str,
) -> dict[str, Any]:
    if durable_claim is None:
        return {}
    durable_turn_id = getattr(durable_claim, "turn_id", None)
    key = build_whatsapp_ticket_effect_key(tenant_id, durable_turn_id, effect)
    return {
        "idempotency_key": key,
        "idempotency_tenant_id": int(tenant_id),
    }


def _tenant_profile_for_user(user: Optional[User]) -> Optional[TenantProfile]:
    if not user:
        return None
    tenant = (
        getattr(user, "tenant", None)
        or getattr(user, "tenant_profile", None)
        or getattr(user, "tenant_profile_municipio", None)
        or getattr(user, "tenant_profile_pyme", None)
    )
    if tenant:
        return tenant
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id:
        return db.session.get(TenantProfile, tenant_id)
    return None


def _reset_municipio_context_for_menu(session_context: ChatSessionContext) -> None:
    if not session_context or not isinstance(session_context.context_data, dict):
        return
    municipio_ctx = session_context.context_data.get(CONTEXTO_MUNICIPIO)
    if not isinstance(municipio_ctx, dict):
        municipio_ctx = {}
        session_context.context_data[CONTEXTO_MUNICIPIO] = municipio_ctx

    municipio_ctx["estado_conversacion"] = "ESPERANDO_SELECCION_MENU_PRINCIPAL"
    # Clear potentially stale drafts to avoid accidental auto-confirm/create when
    # the user selects a fresh numeric menu option (e.g., "1. Iniciar reclamo").
    municipio_ctx.pop("reclamo_flow_v2", None)
    municipio_ctx.pop("datos_reclamo", None)
    municipio_ctx.pop("datos_parciales_llm_reclamo", None)
    municipio_ctx.pop("reclamo_confirmacion_pendiente", None)
    municipio_ctx.pop("confirmation_required", None)
    municipio_ctx.pop("ubicacion_contextual", None)
    municipio_ctx.pop("ultima_consulta_poi", None)
    municipio_ctx.pop("consulta_pendiente_ubicacion", None)
    municipio_ctx.pop("menu_opciones", None)
    session_context.context_data.pop("last_options_sent", None)
    session_context.context_data.pop("last_options_scope", None)
    session_context.context_data.pop("pending_sensitive_action", None)
    safe_flag_modified(session_context, "context_data")


def _apply_completed_reclamo_context(context_data: dict, completion: dict) -> dict:
    """Make a completed claim authoritative over a stale flow snapshot.

    ``crear_nuevo_ticket`` commits internally and expires ORM JSON state.  A
    shallow response-format merge must therefore never be allowed to restore
    the pre-commit ``ESPERANDO_CONFIRMACION`` value.
    """

    if not isinstance(context_data, dict) or not isinstance(completion, dict):
        return context_data
    confirmation_id = str(completion.get("confirmation_id") or "").strip()
    ticket_nro = str(completion.get("ticket_nro") or "").strip()
    if not confirmation_id or not ticket_nro:
        return context_data

    context_update = completion.get("contexto_actualizado")
    if isinstance(context_update, dict):
        for key, value in context_update.items():
            context_data[key] = value

    municipio_ctx = context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    if not isinstance(municipio_ctx, dict):
        municipio_ctx = {}
        context_data[CONTEXTO_MUNICIPIO] = municipio_ctx
    for stale_key in (
        "reclamo_flow_v2",
        "historial_llm_reclamo",
        "datos_parciales_llm_reclamo",
        "expected_fields_llm_reclamo",
        "menu_opciones",
    ):
        municipio_ctx.pop(stale_key, None)
    municipio_ctx["estado_conversacion"] = "CONVERSACION_GENERAL_LLM"
    municipio_ctx["last_created_reclamo"] = {
        key: value
        for key, value in {
            "ts": time.time(),
            "confirmation_id": confirmation_id,
            "ticket_id": completion.get("ticket_id"),
            "ticket_nro": ticket_nro,
            "consulta_pin": completion.get("consulta_pin"),
            "tracking_url": completion.get("tracking_url"),
            "fingerprint": completion.get("fingerprint"),
        }.items()
        if value is not None
    }
    followup_started_at = time.time()
    context_data["active_ticket_followup"] = {
        "ticket_id": completion.get("ticket_id"),
        "ticket_nro": ticket_nro,
        "consulta_pin": completion.get("consulta_pin"),
        "tracking_url": completion.get("tracking_url"),
        "started_at": followup_started_at,
        "until": followup_started_at + CLAIM_FOLLOWUP_WINDOW_SECONDS,
    }
    context_data.pop("last_options_sent", None)
    context_data.pop("pending_sensitive_action", None)
    context_data.pop("foto_url", None)
    context_data.pop("es_foto", None)
    return context_data


def _esperando_info_libre(municipio_ctx: dict) -> bool:
    """True if any LLM prompt awaits free-form user input.

    Both generic conversation fields and claim-specific flows use different
    context keys when asking the user for additional information. This helper
    centralizes the check so numeric shortcuts and other automated handlers
    can pause while the bot waits for a free-form response.
    """

    return (
        municipio_ctx.get("esperando_info_llm")
        or municipio_ctx.get("esperando_info_llm_reclamo")
    )


def _build_sensitive_action_confirmation_text(selected_option: dict) -> str:
    label = (selected_option or {}).get("texto") or "esta acción"
    return (
        f"Antes de continuar con *{label}*, confirmame por favor:\n"
        "1) Sí, iniciar desde cero\n"
        "2) No, volver al menú"
    )


def _selected_option_text_for_bot(selected_option: Optional[dict[str, Any]]) -> Optional[str]:
    """Return the human-facing label that should be sent to the bot layer."""

    if not selected_option:
        return None
    value = selected_option.get("category_name") or selected_option.get("texto")
    if not value:
        return None
    cleaned = re.sub(r"[*_`~]", "", str(value)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def prepare_conversation_turn(turn: ConversationTurn, sink: ConversationSink, *, on_failure, log, safe_reference):
    session_context_db_entry = turn.session_context
    client_user, tenant_profile, end_user = turn.owner_user, turn.tenant_profile, turn.end_user
    from_number_cleaned, chat_session_id_internal = turn.contact_identity, turn.chat_session_id
    tenant_id = turn.tenant_id
    durable_claim = turn.durable_claim
    durable_replay = durable_claim is not None
    flow_submission_present = turn.mode.flow_submission_present
    force_chatboc_demo_hub = turn.mode.force_chatboc_demo_hub
    uploaded_file_info, location_info = turn.attachment, turn.location
    interpretacion_media_data, skip_media_analysis = turn.interpreted_media, turn.skip_media_analysis
    safe_flow_submission, message_sid = turn.validated_flow_submission, turn.source_event_id
    profile_name = turn.profile_name
    message_body = turn.message_body
    try:
        followup_result = _handle_recent_municipal_ticket_followup(
            session_context=session_context_db_entry,
            client_user=client_user,
            tenant_profile=tenant_profile,
            end_user=end_user,
            from_number_cleaned=from_number_cleaned,
            message_body=message_body,
            action=turn.action_id,
            sink=sink,
            durable_claim=durable_claim,
        )
    except Exception as followup_exc:
        db.session.rollback()
        log(
            "error",
            "[WHATSAPP_FOLLOWUP] Failed to persist municipal ticket follow-up error_type=%s",
            type(followup_exc).__name__,
        )
        return ConversationPreparation(None, on_failure("municipal_ticket_followup_failed", followup_exc))
    if followup_result is not None:
        return ConversationPreparation(None, followup_result)

    # --- Numeric Menu Handling ---
    municipio_ctx = (
        session_context_db_entry.context_data.get(CONTEXTO_MUNICIPIO)
        or session_context_db_entry.context_data.get("contexto_municipio", {})
    )
    if isinstance(municipio_ctx, dict):
        flow_state = (municipio_ctx.get("reclamo_flow_v2") or {}).get("state")
        if flow_state:
            skip_media_analysis = True
    esperando_info = _esperando_info_libre(municipio_ctx)

    # Solo traducir números a acciones cuando no estamos esperando información libre.
    selected_option, selected_action_id = _resolve_whatsapp_menu_selection(
        message_body, session_context_db_entry.context_data,
        expecting_free_info=esperando_info,
    )

    pending_sensitive_action = session_context_db_entry.context_data.get("pending_sensitive_action")
    normalized_message = (message_body or "").strip().lower()

    if isinstance(pending_sensitive_action, dict) and not selected_option:
        if normalized_message in SENSITIVE_ACTION_CONFIRM_ACCEPT:
            selected_option = pending_sensitive_action.get("selected_option") or selected_option
            selected_action_id = pending_sensitive_action.get("action_id") or selected_action_id
            session_context_db_entry.context_data.pop("pending_sensitive_action", None)
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()
        elif normalized_message in SENSITIVE_ACTION_CONFIRM_REJECT:
            session_context_db_entry.context_data.pop("pending_sensitive_action", None)
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()
            selected_action_id = "menu_principal"

    if selected_option and (selected_action_id or "").strip().lower() in SENSITIVE_MENU_ACTIONS:
        if not (
            isinstance(pending_sensitive_action, dict)
            and normalized_message in SENSITIVE_ACTION_CONFIRM_ACCEPT
        ):
            session_context_db_entry.context_data["pending_sensitive_action"] = {
                "action_id": selected_action_id,
                "selected_option": selected_option,
            }
            safe_flag_modified(session_context_db_entry, "context_data")
            db.session.commit()

            confirmation_text = _build_sensitive_action_confirmation_text(selected_option)
            if sink.enabled:
                sink.emit_text(confirmation_text)
            return ConversationPreparation(None, ("OK", 200))

        # User explicitly confirmed. Start from a clean draft.
        _reset_municipio_context_for_menu(session_context_db_entry)

    selected_option_label = None
    if selected_option and str(turn.original_text or "").strip().isdigit():
        selected_option_label = _selected_option_text_for_bot(selected_option)
        if selected_option_label:
            message_body = selected_option_label

    # --- Live Chat Routing (WhatsApp -> Admin panel) ---
    human_chat_active = bool(
        session_context_db_entry.context_data.get("human_chat_in_progress")
        or session_context_db_entry.context_data.get("room")
    )
    if (
        not flow_submission_present
        and human_chat_active
        and (message_body or uploaded_file_info or location_info)
    ):
        should_route_live_chat = not bool(selected_option)
        if should_route_live_chat:
            tipo_ticket, live_ticket = _find_live_chat_ticket(
                client_user,
                end_user,
                from_number_cleaned,
                tenant_profile,
                session_context_db_entry.context_data,
            )
            if live_ticket:
                comentario_text = (message_body or "").strip()
                if location_info and not comentario_text:
                    label = location_info.get("label") or location_info.get("address")
                    if label:
                        comentario_text = f"[Ubicación compartida: {label}]"
                    else:
                        comentario_text = "[Ubicación compartida]"

                if uploaded_file_info:
                    attachment_name = uploaded_file_info.get("name") or "archivo"
                    if comentario_text:
                        comentario_text = f"{comentario_text} [Archivo: {attachment_name}]"
                    else:
                        comentario_text = f"[Archivo adjunto: {attachment_name}]"

                    candidate_attachment = db.session.get(
                        ArchivoAdjunto,
                        uploaded_file_info.get("id"),
                    )
                    expected_user_id = getattr(end_user, "id", None)
                    identity_matches = bool(
                        candidate_attachment
                        and (
                            expected_user_id is None
                            or candidate_attachment.user_id == expected_user_id
                        )
                        and (
                            candidate_attachment.session_id is None
                            or candidate_attachment.session_id == chat_session_id_internal
                        )
                    )
                    if tipo_ticket == "municipio":
                        target_matches = bool(
                            candidate_attachment
                            and candidate_attachment.pyme_ticket_id is None
                            and candidate_attachment.municipio_ticket_id in {None, live_ticket.id}
                        )
                    else:
                        target_matches = bool(
                            candidate_attachment
                            and candidate_attachment.municipio_ticket_id is None
                            and candidate_attachment.pyme_ticket_id in {None, live_ticket.id}
                        )
                    if not identity_matches or not target_matches:
                        log(
                            "warning",
                            "Rejected unsafe live-chat attachment binding ticket_type=%s ticket_id=%s",
                            tipo_ticket,
                            live_ticket.id,
                        )
                        uploaded_file_info = None
                    elif tipo_ticket == "municipio":
                        candidate_attachment.municipio_ticket_id = live_ticket.id
                        db.session.add(candidate_attachment)
                    else:
                        candidate_attachment.pyme_ticket_id = live_ticket.id
                        db.session.add(candidate_attachment)

                comentario_data = {
                    "comentario": comentario_text or "[Mensaje sin texto]",
                    "user_id": getattr(end_user, "id", None),
                    "anon_id": None if end_user else from_number_cleaned,
                    "es_admin": False,
                    "origen": "whatsapp",
                    "archivo_adjunto_id": uploaded_file_info.get("id") if uploaded_file_info else None,
                    "emit_socket": False,
                }

                nuevo_comentario = servicio_tickets.crear_comentario(
                    ticket_id=live_ticket.id,
                    tipo_ticket=tipo_ticket,
                    comentario_data=comentario_data,
                    **_durable_whatsapp_ticket_effect_kwargs(
                        durable_claim,
                        getattr(live_ticket, "tenant_id", None)
                        or getattr(tenant_profile, "id", None)
                        or session_context_db_entry.tenant_id,
                        f"live_chat_comment:{tipo_ticket}:{live_ticket.id}",
                    ),
                )

                try:
                    db.session.commit()
                except Exception as exc:
                    log(
                        "error",
                        "[WHATSAPP_WEBHOOK] Error guardando mensaje de chat en vivo error_type=%s",
                        type(exc).__name__,
                    )
                    db.session.rollback()
                else:
                    if nuevo_comentario:
                        try:
                            from socket_service import emit_new_chat_message

                            room_name = f"ticket_{tipo_ticket}_{live_ticket.id}"
                            emit_new_chat_message(
                                {
                                    "socket_room": room_name,
                                    "tenant_type": tipo_ticket,
                                    "ticket_id": live_ticket.id,
                                    "channel": "whatsapp",
                                    "message": nuevo_comentario.to_dict(),
                                }
                            )
                        except Exception as socket_exc:
                            log(
                                "error",
                                "[WHATSAPP_WEBHOOK] Error emitiendo mensaje en vivo error_type=%s",
                                type(socket_exc).__name__,
                            )

                return ConversationPreparation(None, ("OK", 200))

    # Legacy room relays are not a valid authorization boundary. If no exact
    # open ticket was resolved above, clear stale state and resume normal bot
    # handling instead of emitting to a client-controlled/stored room.
    if human_chat_active:
        log(
            "warning",
            "[WHATSAPP_WEBHOOK] Clearing stale live-chat state without exact ticket session_ref=%s",
            safe_reference(chat_session_id_internal),
        )
        session_context_db_entry.context_data.pop("human_chat_in_progress", None)
        session_context_db_entry.context_data.pop("ticket_id", None)
        session_context_db_entry.context_data.pop("tipo_ticket", None)
        session_context_db_entry.context_data.pop("room", None)
        session_context_db_entry.context_data.pop("live_chat_socket_room", None)
        session_context_db_entry.context_data.pop("live_chat_status", None)
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        db.session.commit()

    return ConversationPreparation(ConversationSelection(message_body, selected_option, selected_action_id, selected_option_label, skip_media_analysis))


def finish_conversation_turn(turn: ConversationTurn, selection: ConversationSelection, sink: ConversationSink, *, prepared_response, public_base_url, engine, classify_attachment, extract_contact_details, resolve_input_contact, receipt_header, normalize_flow_contract, on_failure, replay_error, log, safe_reference, safe_context_metadata):
    session_context_db_entry = turn.session_context
    client_user, tenant_profile, end_user = turn.owner_user, turn.tenant_profile, turn.end_user
    from_number_cleaned, chat_session_id_internal = turn.contact_identity, turn.chat_session_id
    tenant_id = turn.tenant_id
    durable_claim = turn.durable_claim
    durable_replay = durable_claim is not None
    flow_submission_present = turn.mode.flow_submission_present
    force_chatboc_demo_hub = turn.mode.force_chatboc_demo_hub
    uploaded_file_info, location_info = turn.attachment, turn.location
    interpretacion_media_data, skip_media_analysis = turn.interpreted_media, selection.skip_media_analysis
    safe_flow_submission, message_sid = turn.validated_flow_submission, turn.source_event_id
    profile_name = turn.profile_name
    message_body = selection.message_body
    selected_option, selected_action_id = selection.option, selection.action_id
    selected_option_label = selection.option_label
    bypass_bot_logic = prepared_response is not None
    bot_response_dict = prepared_response if bypass_bot_logic else {
        'message_body': "Lo siento, no pude procesar tu solicitud en este momento.",
        'options_list': [], 'message_type': 'text', 'fuente': 'error_handler_whatsapp'}
    try:
        if not bypass_bot_logic:
            log(
                "info",
                "Calling responder_chatboc session_ref=%s owner_user_id=%s",
                safe_reference(chat_session_id_internal),
                getattr(client_user, "id", None),
            )

            interpretacion_media_data = None
            if uploaded_file_info:
                mime_type = uploaded_file_info.get("mime_type", "")
                if not skip_media_analysis and not is_audio_media_type(mime_type):
                    interpretacion_media_data = classify_attachment(uploaded_file_info, client_user)
            # Location info should not be treated as interpreted media.
            # It should be passed directly as location data.

            kwargs_for_bot = {
                "source_channel": "whatsapp",
                "tenant_profile": tenant_profile,
                "tenant_id": tenant_id,
                # PII-free provider modality. The exact text continues in
                # ``pregunta`` so emoji/reactions reach language understanding
                # unchanged; this metadata must not be treated as an intent.
                "whatsapp_inbound_content": dict(turn.inbound_content_metadata or {}),
            }
            if durable_replay:
                kwargs_for_bot.update(
                    {
                        "source_event_id": message_sid,
                        "durable_turn_id": getattr(durable_claim, "turn_id", None),
                        "idempotency_key": (
                            f"whatsapp:{tenant_id}:"
                            f"{getattr(durable_claim, 'turn_id', '')}"
                        ),
                    }
                )
            if safe_flow_submission:
                kwargs_for_bot["whatsapp_flow_submission"] = deepcopy(safe_flow_submission)
            education_context = (
                session_context_db_entry.context_data.get("education_context")
                if isinstance(session_context_db_entry.context_data, dict)
                else None
            )
            if education_context:
                kwargs_for_bot["education_context"] = education_context
                kwargs_for_bot["vertical"] = "educacion"
            if uploaded_file_info:
                kwargs_for_bot["uploaded_file_info"] = uploaded_file_info
                kwargs_for_bot["archivo_id_para_asociar"] = uploaded_file_info.get("id")
                mime_type = uploaded_file_info.get("mime_type", "")
                if mime_type.startswith("image/"):
                    # Also add the specific keys the old flow handler expects
                    kwargs_for_bot["es_foto"] = True
                    kwargs_for_bot["foto_url"] = uploaded_file_info.get("url")
                if skip_media_analysis:
                    kwargs_for_bot["skip_media_analysis"] = True
            if location_info:
                # Pass location_info and mark it explicitly as a location payload
                kwargs_for_bot["location"] = location_info
                kwargs_for_bot["es_ubicacion"] = True
                kwargs_for_bot["ubicacion_usuario"] = location_info
            if interpretacion_media_data and not interpretacion_media_data.get("error"):
                # This will now only contain data from actual images/files, not locations.
                kwargs_for_bot["datos_interpretados_archivo"] = interpretacion_media_data
            if selected_action_id:
                kwargs_for_bot["action"] = selected_action_id

            if selected_option:
                kwargs_for_bot["selected_option_data"] = selected_option

            profile_name = turn.original_profile_name
            resolved_contact = resolve_input_contact(from_number_cleaned, profile_name)
            if resolved_contact:
                kwargs_for_bot["resolved_contact"] = resolved_contact
                resolved_contact = store_contact_projection(
                    session_context_db_entry.context_data, resolved_contact,
                    tenant_id=tenant_id, phone=from_number_cleaned,
                )
                kwargs_for_bot["resolved_contact"] = resolved_contact
            if profile_name:
                kwargs_for_bot["profile_name"] = profile_name

            # The actual call that might raise an exception
            bot_response_dict = engine(
                pregunta=message_body,
                owner_user=client_user,
                current_user=end_user,
                rubro_obj=client_user.rubro,
                chat_db_context=session_context_db_entry,
                rubro_nombre_frontend=None,
                tipo_chat=client_user.tipo_chat,
                anon_id=from_number_cleaned,
                chat_session_uuid=chat_session_id_internal,
                channel="whatsapp",
                **kwargs_for_bot
            )

            normalize_response_payload(bot_response_dict)

            # Si el usuario es anónimo y la acción requiere datos personales, pedir solo los faltantes.
            if not end_user and bot_response_dict.get("accion_backend") in ["crear_reclamo", "iniciar_reclamo"]:
                contexto_actual = session_context_db_entry.context_data.get("contexto_municipio", {})
                datos_reclamo = contexto_actual.get("datos_parciales_llm_reclamo", {})

                potential_fields = ["nombre_cliente", "telefono_cliente", "email_cliente"]
                log(
                    "debug",
                    "[CONTACT_EXTRACTION] Extracting contact fields requested_count=%s body_length=%s",
                    len(potential_fields),
                    len(message_body or ""),
                )
                extracted_data = extract_contact_details(message_body, potential_fields)
                log(
                    "debug",
                    "[CONTACT_EXTRACTION] Extraction completed extracted_count=%s",
                    sum(bool(extracted_data.get(field)) for field in potential_fields),
                )

                if extracted_data.get("nombre_cliente"):
                    datos_reclamo["nombre_usuario_detectado"] = extracted_data["nombre_cliente"]
                if extracted_data.get("telefono_cliente"):
                    datos_reclamo["telefono_detectado"] = extracted_data["telefono_cliente"]
                if extracted_data.get("email_cliente"):
                    datos_reclamo["email_detectado"] = extracted_data["email_cliente"]

                contexto_actual["datos_parciales_llm_reclamo"] = datos_reclamo
                session_context_db_entry.context_data["contexto_municipio"] = contexto_actual

                contacto = resolve_contact_snapshot(
                    datos=datos_reclamo,
                    profile_name=contexto_actual.get("profile_name") or session_context_db_entry.context_data.get("profile_name"),
                    anon_id=from_number_cleaned,
                )
                faltan_contactos = missing_contact_fields(contacto)
                if faltan_contactos:
                    bot_response_dict = {
                        "message_body": (
                            "Para poder registrar tu reclamo, necesito estos datos: "
                            + ", ".join(faltan_contactos)
                            + "."
                        ),
                        "pedir_info": faltan_contactos,
                    }

            log(
                "debug",
                "responder_chatboc response metadata type=%s keys=%s body_length=%s",
                type(bot_response_dict).__name__,
                sorted(str(key) for key in bot_response_dict.keys())[:24]
                if isinstance(bot_response_dict, dict)
                else [],
                len(str(bot_response_dict.get("message_body") or ""))
                if isinstance(bot_response_dict, dict)
                else 0,
            )

            # Validate the response from the bot logic
            if not isinstance(bot_response_dict, dict):
                log(
                    "warning",
                    "responder_chatboc returned an invalid response type=%s",
                    type(bot_response_dict).__name__,
                )
                if durable_replay:
                    raise replay_error(
                        "responder_chatboc_invalid_response"
                    )
                # Keep the default error response initialized earlier.
                bot_response_dict = {
                    'message_body': "Lo siento, hubo un error interno al procesar tu mensaje.",
                    'options_list': [], 'message_type': 'text', 'fuente': 'error_handler_non_dict_response'
                }

            # Ensure context_data is a dict for saving
            if not isinstance(session_context_db_entry.context_data, dict):
                log(
                    "warning",
                    "context_data in session_context_db_entry is not a dict. Resetting.",
                )
                session_context_db_entry.context_data = {
                    "historial_chat": [{"role": "system", "content": "Context was reset due to invalid format."}],
                    "estado_conversacion": "error_context"
                }

    except Exception as exc:
        log(
            "error",
            "Error calling real chatbot logic (responder_chatboc) error_type=%s",
            type(exc).__name__,
        )
        if durable_replay:
            raise replay_error("responder_chatboc_failed") from exc
        # Legacy mode keeps its user-facing fallback response.

    # Update respuesta_del_bot_text for logging from the final bot_response_dict
    respuesta_del_bot_text = bot_response_dict.get("message_body", "")
    log(
        "debug",
        "Bot response prepared. body_length=%s context_metadata=%s",
        len(respuesta_del_bot_text) if isinstance(respuesta_del_bot_text, str) else 0,
        safe_context_metadata(session_context_db_entry.context_data),
    )

    # --- Format Response and Save Session ---
    formatted_whatsapp_payload = {}
    try:
        from services.response_formatter import build_interactive_response

        receipt_payload = bot_response_dict.get("whatsapp_receipt")
        if receipt_payload:
            bot_response_dict["message_body"] = receipt_payload.get("body_text")
            bot_response_dict["options_list"] = []
            bot_response_dict["message_type"] = "text"
        is_completed_order = (
            bot_response_dict.get("fuente") == "pyme_pedido_confirmado"
            and bot_response_dict.get("success") is True
        )
        if receipt_payload or is_completed_order:
            # Receipt imagery is public organization branding, never a claim
            # attachment or a municipal default inherited by another tenant.
            bot_response_dict.pop("image_url", None)
            receipt_header_url = receipt_header()
            if receipt_header_url:
                bot_response_dict["image_url"] = receipt_header_url

        response_base_url = (
            (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
            or (public_base_url or "").rstrip("/")
        )
        normalize_flow_contract(bot_response_dict, base_url=response_base_url)
        body_text = bot_response_dict.get("message_body", "")

        # This call will modify bot_response_dict to include context for the numeric menu
        formatted_whatsapp_payload = build_interactive_response(
            options=bot_response_dict.get('options_list', []),
            body_text=body_text,
            channel='whatsapp',
            message_type=bot_response_dict.get('message_type', 'text'),
            original_bot_response=bot_response_dict,
            header_text=bot_response_dict.get('header_text'),
            footer_text=bot_response_dict.get('footer_text'),
            audio_url=bot_response_dict.get('audio_url')
        )

        # After formatting, the context might be updated (e.g., with last_options_sent).
        # We need to merge this updated context back into our main session object before saving.
        updated_context = formatted_whatsapp_payload.get('contexto_actualizado')

        # The existing context from the database
        db_context = session_context_db_entry.context_data or {}
        log(
            "info",
            "[CONTEXT_WHATSAPP] Context metadata db_key_count=%s update_key_count=%s",
            len(db_context) if isinstance(db_context, dict) else 0,
            len(updated_context) if isinstance(updated_context, dict) else 0,
        )


        # Merge the contexts
        if updated_context:
            merged_context = {**db_context, **updated_context}
        else:
            merged_context = db_context

        for key_to_delete in bot_response_dict.get("_context_keys_to_delete") or []:
            merged_context.pop(key_to_delete, None)

        claim_completion = bot_response_dict.get("_reclamo_completion")
        if isinstance(claim_completion, dict):
            _apply_completed_reclamo_context(merged_context, claim_completion)

        log(
            "info",
            "[CONTEXT_WHATSAPP] Context merged key_count=%s has_claim_completion=%s",
            len(merged_context) if isinstance(merged_context, dict) else 0,
            isinstance(claim_completion, dict),
        )


        # Save the merged context
        session_context_db_entry.context_data = merged_context
        safe_flag_modified(session_context_db_entry, "context_data")
        db.session.add(session_context_db_entry)
        if not force_chatboc_demo_hub and tenant_profile and getattr(tenant_profile, "id", None):
            try:
                contact = resolve_or_create_contact(
                    tenant_profile,
                    phone=from_number_cleaned,
                    whatsapp_id=from_number_cleaned,
                    name=locals().get("profile_name") or turn.original_profile_name,
                    legacy_user=end_user,
                    contact_type="neighbor" if client_user.tipo_chat == "municipio" else "customer",
                    source="whatsapp_inbound",
                )
                record_contact_interaction(
                    tenant=tenant_profile,
                    contact=contact,
                    message_body=message_body or selected_option_label or "",
                    channel="whatsapp",
                    direction="inbound",
                    source="whatsapp_webhook",
                    metadata={
                        "event_type": "whatsapp_inbound",
                        "message_sid": message_sid,
                        "selected_action_id": selected_action_id,
                        "selected_option": selected_option_label,
                        "owner_user_id": getattr(client_user, "id", None),
                        "end_user_id": getattr(end_user, "id", None),
                        "bot_fuente": bot_response_dict.get("fuente"),
                    },
                    content_type="text" if message_body else "event",
                    emit=True,
                )
            except Exception as crm_err:
                log(
                    "warning",
                    "[CRM] WhatsApp contact enrichment skipped tenant_id=%s error_type=%s",
                    getattr(tenant_profile, "id", None),
                    type(crm_err).__name__,
                )
        db.session.commit()
        log(
            "info",
            "Session saved. tenant_id=%s user_id=%s context_metadata=%s",
            getattr(session_context_db_entry, "tenant_id", None),
            getattr(session_context_db_entry, "user_id", None),
            safe_context_metadata(session_context_db_entry.context_data),
        )

    except Exception as exc:
        db.session.rollback()
        log(
            "error",
            "Error formatting response or saving session session_ref=%s error_type=%s",
            safe_reference(chat_session_id_internal),
            type(exc).__name__,
        )
        # A 200 here permanently acknowledges an event that we did not store.
        # The queue worker must observe an exception and transition its durable
        # turn to retry_wait; the direct webhook asks Twilio for a bounded 5xx
        # retry instead.
        return ConversationFinish(None, None, on_failure("response_format_or_session_commit_failed", exc))

    sink.emit_response(bot_response_dict, formatted_whatsapp_payload)
    return ConversationFinish(bot_response_dict, formatted_whatsapp_payload,
        response_text_before_format=respuesta_del_bot_text)
