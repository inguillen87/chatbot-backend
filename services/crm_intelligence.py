from __future__ import annotations

import re
from copy import deepcopy
from functools import wraps
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from flask import current_app, has_app_context
from sqlalchemy import or_
from sqlalchemy.orm import object_session
from sqlalchemy.orm.attributes import flag_modified

from extensions import db
from models import TenantProfile, User
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services.contact_intake import is_placeholder_email, normalize_email
from services.crm_output_safety import (
    contains_crm_sensitive_content,
    redact_crm_sensitive_text,
    redact_crm_sensitive_value,
)


MAX_SUMMARY_CHARS = 280
COMMERCIAL_CONTACT_ACTIONS = frozenset({
    "chatboc_sales_lead", "capturar_lead_comercial", "chatboc_demo_limit_contact_yes",
})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: Any, limit: int = 420) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    return text[:limit].rstrip()


def normalize_phone(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = re.sub(r"[^\d+]", "", raw)
    return normalized or None


def safe_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.split(",")
    else:
        raw_items = []
    out: list[str] = []
    for item in raw_items:
        tag = clean_text(item, 48).lower()
        if tag and tag not in out:
            out.append(tag)
    return out


def merge_tags(*groups: Any) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for tag in safe_tags(group):
            if tag not in merged:
                merged.append(tag)
    return merged


def _looks_like_message_name(value: Any) -> bool:
    text = clean_text(value, 140).lower()
    if not text:
        return False
    markers = (
        " es tu codigo",
        "no lo compartas",
        "quisiera saber",
        "quiero saber",
        "hola ",
        "hola.",
        "para que servis",
    )
    return any(marker in text for marker in markers) or len(text) > 80


def display_contact_name(raw_name: Any, *, phone: Any = None, email: Any = None) -> str:
    name = clean_text(raw_name, 90)
    if name and not _looks_like_message_name(name) and not contains_crm_sensitive_content(name):
        return name
    if phone:
        return "Contacto WhatsApp"
    if email:
        return "Contacto web"
    return "Contacto sin identificar"


def classify_reason(text: str, metadata: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    """Small CRM classification layer, not conversation routing.

    The bot still decides flow with the LLM. This only labels contacts for ops.
    """

    metadata = metadata or {}
    action_id = clean_text(metadata.get("action_id"), 80).lower()
    lower = clean_text(text, 500).lower()
    combined = f"{action_id} {lower}"

    if action_id in COMMERCIAL_CONTACT_ACTIONS or metadata.get("commercial_contact_requested") is True:
        return "lead_hot", "Interes comercial o contratacion"

    checks: list[tuple[str, tuple[str, ...], str]] = [
        (
            "lead_hot",
            ("contratar", "precio", "demo", "ventas", "asesor", "llamen", "propuesta", "cotizar", "comprar", "pagar"),
            "Interes comercial o contratacion",
        ),
        (
            "reclamo",
            ("reclamo", "ticket", "luminaria", "bache", "arbol", "basura", "agua", "semaforo", "calle"),
            "Reclamo o seguimiento operativo",
        ),
        (
            "encuestas",
            ("encuesta", "votacion", "sondeo", "participacion"),
            "Participacion ciudadana / encuesta",
        ),
        (
            "pedido_catalogo",
            ("pedido", "catalogo", "carrito", "stock", "producto", "factura"),
            "Pedido, catalogo o compra",
        ),
        (
            "educacion",
            ("colegio", "escuela", "alumno", "padre", "madre", "secretaria", "cuota", "docente"),
            "Consulta educativa o familia",
        ),
        (
            "soporte",
            ("ayuda", "problema", "no funciona", "error", "soporte", "hablar con"),
            "Soporte o asistencia",
        ),
    ]

    for intent, needles, reason in checks:
        # Transport/source names and demo-navigation IDs are not buying intent.
        # Explicit commercial actions above and the person's text are signals.
        candidate = lower if intent == "lead_hot" else combined
        if any(needle in candidate for needle in needles):
            return intent, reason

    if lower in {"hola", "buenas", "buen dia", "buenas tardes", "menu"}:
        return "saludo", "Primer contacto / saludo"

    return "consulta_general", "Consulta general"


def lead_temperature(intent: str, text: str, metadata: Optional[dict[str, Any]] = None) -> tuple[str, int]:
    lower = clean_text(text, 500).lower()
    metadata = metadata or {}
    action = clean_text(metadata.get("action_id"), 120).lower()
    if intent == "lead_hot" or action in COMMERCIAL_CONTACT_ACTIONS or metadata.get("commercial_contact_requested") is True or any(
        word in lower
        for word in ("contratar", "precio", "demo", "ventas", "asesor", "cotizar", "propuesta", "llamen")
    ):
        return "hot", 85
    if intent in {"pedido_catalogo", "reclamo", "encuestas", "educacion", "soporte"} or metadata.get("prospect_engaged") is True:
        return "warm", 55
    return "cold", 25


def suggested_actions_for(intent: str, temperature: str) -> list[str]:
    if temperature == "hot":
        return ["contactar_ventas", "agendar_demo", "registrar_oportunidad"]
    if intent == "reclamo":
        return ["revisar_ticket", "asignar_responsable", "avisar_estado"]
    if intent == "encuestas":
        return ["enviar_link_encuesta", "segmentar_participacion"]
    if intent == "educacion":
        return ["responder_familia", "derivar_secretaria"]
    if intent == "pedido_catalogo":
        return ["validar_pedido", "enviar_catalogo"]
    if intent == "soporte":
        return ["responder_consulta", "derivar_humano"]
    return ["responder_consulta"]


def build_summary(text: str, *, reason: str, channel: str, source: Optional[str]) -> str:
    body = clean_text(text, 220)
    channel_label = clean_text(channel, 40) or "canal"
    source_label = clean_text(source, 80)
    if body:
        prefix = f"{reason}. Entro por {channel_label}"
        if source_label:
            prefix += f" ({source_label})"
        return clean_text(f"{prefix}: {body}", MAX_SUMMARY_CHARS)
    return clean_text(f"{reason}. Entro por {channel_label}.", MAX_SUMMARY_CHARS)


def merge_summary(previous: Optional[str], latest: str) -> str:
    previous_clean = clean_text(previous, MAX_SUMMARY_CHARS)
    latest_clean = clean_text(latest, MAX_SUMMARY_CHARS)
    if not previous_clean:
        return latest_clean
    if not latest_clean or latest_clean.lower() in previous_clean.lower():
        return previous_clean
    return clean_text(f"{latest_clean} Historial previo: {previous_clean}", MAX_SUMMARY_CHARS)




def _uow_phone_matches(value, expected):
    """Compare to the signed recipient; never infer a country or repair history."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if value.lower().startswith("whatsapp:"):
        value = value.split(":", 1)[1].strip()
    value = re.sub(r"[()\s.\-]", "", value)
    return bool(re.fullmatch(r"\+?[1-9][0-9]{6,14}", value)
                and value.removeprefix("+") == expected.removeprefix("+"))


def _uow_require_phones(unit, expected, *values):
    present = [value for value in values
               if value is not None and not (isinstance(value, str) and not value.strip())]
    if not present or not all(_uow_phone_matches(value, expected) for value in present):
        unit.fail("conversation_uow_crm_recipient_mismatch")


def _uow_recipient_contact(unit, tenant_id, expected):
    # Read persisted columns under the wrapper's no_autoflush. Do not refresh
    # or overwrite a caller's ORM changes, scan/rewrite legacy phone columns,
    # or select by a user-supplied email/external ID.
    variants = (expected, expected.removeprefix("+"))
    rows = (Contact.query.with_entities(Contact.id, Contact.phone, Contact.whatsapp_id)
        .filter(Contact.tenant_id == tenant_id,
                or_(Contact.phone.in_(variants), Contact.whatsapp_id.in_(variants)))
        .order_by(Contact.id).limit(2).all())
    if len(rows) > 1:
        unit.fail("conversation_uow_crm_recipient_ambiguous")
    if not rows:
        return None
    row = rows[0]
    _uow_require_phones(unit, expected, row.phone, row.whatsapp_id)
    contact = db.session.get(Contact, row.id)
    if contact is None or contact.tenant_id != tenant_id:
        unit.fail("conversation_uow_crm_contact_scope_mismatch")
    _uow_require_phones(unit, expected, contact.phone, contact.whatsapp_id)
    return contact


def _uow_require_legacy_user(unit, tenant, user, expected):
    from services.contact_service import resolve_contact, sanitize_profile_name
    actual = db.session()
    if (user in actual.new or user in actual.dirty or user in actual.deleted
            or type(user.id) is not int or not _uow_phone_matches(user.telefono, expected)):
        unit.fail("conversation_uow_crm_legacy_user_scope_unproven")
    # The fresh shared resolver, not a marker supplied by the caller, decides
    # whether this User is the unique current contact. Contact precedence,
    # ambiguous Users or a revoked profile require legacy_user=None.
    fresh = resolve_contact(expected, None, tenant=tenant)
    if (fresh.get("is_known_contact") is not True
            or (fresh.get("_scope") or {}).get("source") != "User"
            or type(fresh.get("user_id")) is not int or fresh["user_id"] != user.id
            or sanitize_profile_name(user.name) != fresh.get("nombre")
            or user.email != fresh.get("email")):
        unit.fail("conversation_uow_crm_legacy_user_scope_unproven")


def _conversation_crm_writer(method):
    """Scope new explicit UoW calls without changing the legacy call graph."""
    @wraps(method)
    def wrapped(*args, **kwargs):
        unit = kwargs.get("unit_of_work")
        if unit is None:
            return method(*args, **kwargs)
        from services.whatsapp_conversation_uow import (
            ConversationUnitOfWork, ConversationUnitOfWorkError,
        )
        if not isinstance(unit, ConversationUnitOfWork):
            raise ConversationUnitOfWorkError("conversation_uow_writer_capability_invalid")
        try:
            with db.session.no_autoflush:
                actual = db.session()
                tenant = kwargs.get("tenant") if "tenant" in kwargs else args[0]
                if not isinstance(tenant, TenantProfile) or object_session(tenant) is not actual:
                    unit.fail("conversation_uow_crm_tenant_session_mismatch")
                unit.require_writer_scope(session=actual, tenant_id=tenant.id,
                    turn_id=kwargs.get("conversation_turn_id"))
                contact = kwargs.get("contact")
                if contact is not None and (not isinstance(contact, Contact)
                        or object_session(contact) is not actual or contact.tenant_id != tenant.id):
                    unit.fail("conversation_uow_crm_contact_scope_mismatch")
                legacy_user = kwargs.get("legacy_user")
                if legacy_user is not None and (not isinstance(legacy_user, User)
                        or object_session(legacy_user) is not actual
                        or legacy_user.tenant_id != tenant.id):
                    unit.fail("conversation_uow_crm_legacy_user_scope_unproven")
                expected = unit.contact_identity
                if legacy_user is not None:
                    _uow_require_legacy_user(unit, tenant, legacy_user, expected)
                if contact is not None:
                    current = _uow_recipient_contact(unit, tenant.id, expected)
                    if current is None or current.id != contact.id:
                        unit.fail("conversation_uow_crm_contact_recipient_mismatch")
                return method(*args, **kwargs)
        except Exception as exc:
            unit.fail("conversation_uow_crm_writer_failed", cause=exc)
    return wrapped


@_conversation_crm_writer
def resolve_or_create_contact(
    tenant: TenantProfile,
    *,
    phone: Any = None,
    email: Any = None,
    external_id: Any = None,
    whatsapp_id: Any = None,
    name: Any = None,
    legacy_user: Optional[User] = None,
    contact_type: str = "lead",
    source: Optional[str] = None,
    unit_of_work=None,
    conversation_turn_id: Optional[str] = None,
) -> Contact:
    phone_norm = normalize_phone(phone)
    email_norm = normalize_email(str(email or "")) if email and not is_placeholder_email(str(email)) else None
    external_norm = clean_text(external_id, 255) or None
    whatsapp_norm = normalize_phone(whatsapp_id) or phone_norm

    filters = []
    if phone_norm:
        filters.append(Contact.phone == phone_norm)
    if whatsapp_norm:
        filters.append(Contact.whatsapp_id == whatsapp_norm)
    if email_norm:
        filters.append(Contact.email == email_norm)
    if external_norm:
        filters.append(Contact.external_id == external_norm)

    contact = None
    if unit_of_work is not None:
        expected = unit_of_work.contact_identity
        _uow_require_phones(unit_of_work, expected, phone, whatsapp_id)
        contact = _uow_recipient_contact(unit_of_work, tenant.id, expected)
        # Aliases may be values declared on this turn, never selectors for a
        # different recipient. Shared/conflicting aliases fail closed in UoW.
        alias_filters = []
        if email_norm:
            alias_filters.append(Contact.email == email_norm)
        if external_norm:
            alias_filters.append(Contact.external_id == external_norm)
        if alias_filters:
            alias_query = Contact.query.with_entities(Contact.id).filter(
                Contact.tenant_id == tenant.id, or_(*alias_filters))
            if contact is not None:
                alias_query = alias_query.filter(Contact.id != contact.id)
            if alias_query.first() is not None:
                unit_of_work.fail("conversation_uow_crm_alias_recipient_conflict")
        # Canonicalize only this new server-bound input, not stored history.
        phone_norm = whatsapp_norm = expected
    elif filters:
        contact = Contact.query.filter(Contact.tenant_id == tenant.id, or_(*filters)).first()

    display_name = display_contact_name(
        name or getattr(legacy_user, "name", None),
        phone=phone_norm or whatsapp_norm,
        email=email_norm,
    )
    legacy_email = normalize_email(getattr(legacy_user, "email", None)) if legacy_user else None
    legacy_phone = normalize_phone(getattr(legacy_user, "telefono", None)) if legacy_user else None
    tags = merge_tags(getattr(legacy_user, "tags", None), [source] if source else None)
    prefs = {
        "source": source or "crm_auto",
        "legacy_user_id": getattr(legacy_user, "id", None),
        "marketing_opt_in": bool(getattr(legacy_user, "acepta_marketing", False)) if legacy_user else None,
    }

    if contact is None:
        contact = Contact(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            name=display_name,
            phone=phone_norm or legacy_phone,
            email=email_norm or legacy_email,
            external_id=external_norm,
            whatsapp_id=whatsapp_norm,
            type=contact_type or "lead",
            tags=tags,
            preferences={k: v for k, v in prefs.items() if v is not None},
            last_interaction_at=utc_now(),
        )
        db.session.add(contact)
        if unit_of_work is not None:
            unit_of_work.flush()
        else:
            db.session.flush()
        return contact

    current_prefs = contact.preferences if isinstance(contact.preferences, dict) else {}
    for key, value in prefs.items():
        if value is not None:
            current_prefs[key] = value

    contact.name = contact.name if contact.name and not _looks_like_message_name(contact.name) else display_name
    contact.phone = contact.phone or phone_norm or legacy_phone
    contact.email = contact.email or email_norm or legacy_email
    contact.external_id = contact.external_id or external_norm
    contact.whatsapp_id = contact.whatsapp_id or whatsapp_norm
    contact.type = contact.type if contact.type not in {None, "", "unknown"} else (contact_type or "lead")
    contact.tags = merge_tags(contact.tags, tags)
    contact.preferences = current_prefs
    contact.last_interaction_at = utc_now()
    db.session.add(contact)
    if unit_of_work is not None:
        unit_of_work.flush()
    return contact


@_conversation_crm_writer
def record_contact_interaction(
    *,
    tenant: TenantProfile,
    contact: Contact,
    message_body: Any,
    channel: str,
    direction: str = "inbound",
    source: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    content_type: str = "text",
    media_url: Optional[str] = None,
    emit: bool = False,
    unit_of_work=None,
    conversation_turn_id: Optional[str] = None,
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    text = clean_text(message_body, 1200)
    intent, reason = classify_reason(text, metadata)
    temp, score = lead_temperature(intent, text, metadata)
    actions = suggested_actions_for(intent, temp)
    summary = build_summary(text, reason=reason, channel=channel, source=source or metadata.get("source"))

    prefs = contact.preferences if isinstance(contact.preferences, dict) else {}
    previous_stage = prefs.get("conversation_status")
    operator_closed_stage = bool(prefs.get("stage_updated_at")) and previous_stage in {"cliente", "perdido", "pausado"}
    retain_commercial_priority = prefs.get("commercial_contact_requested") is True and prefs.get("lead_temperature") == "hot"
    contact_temp, contact_score = temp, score
    contact_status = "hot" if temp == "hot" else ("en_seguimiento" if temp == "warm" else "frio")
    if temp != "hot" and operator_closed_stage:
        contact_temp = prefs.get("lead_temperature") or temp
        contact_score = prefs.get("lead_score", score)
        contact_status = previous_stage
    elif temp != "hot" and retain_commercial_priority:
        # The latest turn may be a greeting while an explicit sales request
        # still needs attention. Keep its contact priority, not a false intent.
        contact_temp, contact_score = "hot", 85
        contact_status = previous_stage or "hot"
    prefs.update(
        {
            "last_channel": channel,
            "last_source": source or metadata.get("source") or prefs.get("source") or "crm_auto",
            "last_reason": reason,
            "last_summary": summary,
            "last_intent": intent,
            "lead_temperature": contact_temp,
            "lead_score": contact_score,
            "conversation_status": contact_status,
            "last_lead_temperature": temp,
            "last_lead_score": score,
            "last_message_excerpt": clean_text(text, 180),
            "last_interaction_direction": direction,
            "service_window_until": (utc_now() + timedelta(hours=24)).isoformat() if channel == "whatsapp" else None,
        }
    )
    contact.preferences = {k: v for k, v in prefs.items() if v is not None}
    contact.tags = merge_tags(contact.tags, [intent, temp, channel, source])
    contact.last_interaction_at = utc_now()
    db.session.add(contact)
    flag_modified(contact, "preferences")
    flag_modified(contact, "tags")

    snapshot = ContactSnapshot.query.filter_by(contact_id=contact.id).first()
    if snapshot is None:
        snapshot = ContactSnapshot(contact_id=contact.id)
    snapshot.summary_text = merge_summary(snapshot.summary_text, summary)
    snapshot.last_intent = intent
    snapshot.suggested_actions = actions
    db.session.add(snapshot)
    flag_modified(snapshot, "suggested_actions")

    event_metadata = {
        **metadata,
        "source": source or metadata.get("source"),
        "intent": intent,
        "reason": reason,
        "lead_temperature": temp,
        "lead_score": score,
    }
    db.session.add(
        InteractionEvent(
            tenant_id=tenant.id,
            contact_id=contact.id,
            channel=channel,
            direction=direction,
            content_type=content_type,
            content=text,
            media_url=media_url,
            metadata_payload=event_metadata,
        )
    )
    if unit_of_work is not None:
        unit_of_work.flush()
    else:
        db.session.flush()

    payload = serialize_crm_contact(contact, snapshot=snapshot)
    if emit:
        if unit_of_work is not None:
            snapshot_payload = deepcopy(payload)
            unit_of_work.defer_after_commit("crm_contact_update",
                lambda: emit_crm_contact_update(tenant, snapshot_payload))
        else:
            emit_crm_contact_update(tenant, payload)
    return payload


def record_meta_business_interaction(*, unit_of_work, conversation_turn_id):
    """Record one source-bound text event, without classifying its author or text.

    Tenant, recipient, input and replay keys come from the owned claim. This
    private native entry does not alter the legacy classifier/writer contract.
    """
    from types import SimpleNamespace
    from models import ChatSessionContext
    from services.whatsapp_conversation_uow import ConversationUnitOfWork, ConversationUnitOfWorkError

    unit = unit_of_work
    if not isinstance(unit, ConversationUnitOfWork):
        raise ConversationUnitOfWorkError("meta_pyme_crm_owner_required")
    try:
        session = unit.session
        unit.require_writer_scope(session=session, tenant_id=unit.claim.tenant_id,
            turn_id=conversation_turn_id)
        tenant = session.get(TenantProfile, unit.claim.tenant_id)
        context = session.get(ChatSessionContext, unit.claim.chat_session_id)
        owner = session.get(User, context.user_id) if context is not None else None
        if (tenant is None or tenant.tipo != "pyme" or owner is None
                or tenant.pyme_id != owner.id or owner.tipo_chat != "pyme"
                or unit.claim.provider != "meta" or unit.claim.message_kind != "text"):
            unit.fail("meta_pyme_crm_scope_invalid")
        body = unit.claim.payload["input"]["text"]
        if not isinstance(body, str) or not 0 < len(body) <= 4096:
            unit.fail("meta_pyme_crm_input_invalid")
        try:
            body.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            unit.fail("meta_pyme_crm_text_unrepresentable")
        if chr(0) in body:
            unit.fail("meta_pyme_crm_text_unrepresentable")
        effect_key = f"whatsapp:{tenant.id}:{unit.claim.turn_id}:crm.native_business_inbound"
        unit.require_writer_scope(session=session, tenant_id=tenant.id,
            turn_id=conversation_turn_id, effect_key=effect_key)
        recipient = unit.contact_identity
        with session.no_autoflush:
            # The durable claim is the normal serialization/fencing authority.
            # This lookup also makes a repeated entry in that unit idempotent;
            # it does not add a global SQL uniqueness or cross-provider claim.
            events = (session.query(InteractionEvent).filter(
                InteractionEvent.tenant_id == tenant.id,
                InteractionEvent.channel == "whatsapp",
                InteractionEvent.direction == "inbound",
                InteractionEvent.metadata_payload["provider"].as_string() == "meta",
                InteractionEvent.metadata_payload["durable_turn_id"].as_string() == unit.claim.turn_id,
                InteractionEvent.metadata_payload["idempotency_key"].as_string() == effect_key,
            ).order_by(InteractionEvent.id).limit(2).all())
            if len(events) > 1:
                unit.fail("meta_pyme_crm_event_ambiguous")
            if events:
                contact = _uow_recipient_contact(unit, tenant.id, recipient)
                event = events[0]
                if (contact is None or event.contact_id != contact.id
                        or event.content_type != "text" or event.content != body
                        or (event.metadata_payload or {}).get("source_event_id") != unit.claim.provider_message_sid):
                    unit.fail("meta_pyme_crm_event_scope_invalid")
                return {"interaction_event_id": event.id, "contact_id": contact.id,
                    "payload": serialize_crm_contact(contact), "created": False}

        contact = resolve_or_create_contact(tenant, phone=recipient, whatsapp_id=recipient,
            name=None, legacy_user=None, contact_type="unknown", source="whatsapp_inbound",
            unit_of_work=unit, conversation_turn_id=conversation_turn_id)
        prefs = dict(contact.preferences) if isinstance(contact.preferences, dict) else {}
        # This writer applied the evidence contract; not historical origin,
        # human scoring, consent, or account authority. Preserve stored facts.
        prefs["native_business_evidence_contract"] = "crm.meta_native_business_evidence.v1"
        prefs.update(last_channel="whatsapp", last_source="whatsapp_webhook",
            last_message_excerpt=clean_text(body, 180), last_interaction_direction="inbound")
        contact.preferences = prefs
        flag_modified(contact, "preferences")
        event = InteractionEvent(tenant_id=tenant.id, contact_id=contact.id,
            channel="whatsapp", direction="inbound", content_type="text", content=body,
            media_url=None, metadata_payload={
                "event_type": "whatsapp_inbound", "source": "whatsapp_webhook", "provider": "meta",
                "message_sid": unit.claim.provider_message_sid,
                "source_event_id": unit.claim.provider_message_sid,
                "durable_turn_id": unit.claim.turn_id, "idempotency_key": effect_key,
                "native_business_contract": "pyme.meta_native_text.v1",
            })
        session.add(event)
        unit.flush()
        if type(event.id) is not int or event.id <= 0:
            unit.fail("meta_pyme_crm_event_id_invalid")
        payload = serialize_crm_contact(contact)
        realtime_tenant = SimpleNamespace(id=tenant.id, slug=tenant.slug)
        realtime_payload = deepcopy(payload)
        unit.defer_after_commit("crm_contact_update",
            lambda tenant=realtime_tenant, payload=realtime_payload:
                emit_crm_contact_update(tenant, payload))
        return {"interaction_event_id": event.id, "contact_id": contact.id,
            "payload": payload, "created": True}
    except Exception as exc:
        unit.fail("meta_pyme_crm_write_failed", cause=exc)


def _native_evidence_score(value):
    """Project existing finite evidence only; never calculate a score from text."""
    import math
    if value is None or type(value) is bool or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            number = float(value)
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def serialize_crm_contact(
    contact: Contact,
    *,
    snapshot: Optional[ContactSnapshot] = None,
    legacy_user: Optional[User] = None,
    interaction_count: Optional[int] = None,
) -> dict[str, Any]:
    prefs = contact.preferences if isinstance(contact.preferences, dict) else {}
    snapshot = snapshot or ContactSnapshot.query.filter_by(contact_id=contact.id).first()
    phone = normalize_phone(contact.phone or getattr(legacy_user, "telefono", None))
    raw_email = contact.email or getattr(legacy_user, "email", None) or ""
    real_email = normalize_email(raw_email)
    raw_name = contact.name or getattr(legacy_user, "name", None) or ""
    name_is_sensitive = contains_crm_sensitive_content(raw_name)
    serialized_raw_name = redact_crm_sensitive_text(raw_name)
    name = display_contact_name(raw_name, phone=phone, email=real_email)
    # Only contacts processed under the new unit-owned evidence contract
    # project absence as unknown. This does not certify historical origin.
    native_unknown = prefs.get("native_business_evidence_contract") == "crm.meta_native_business_evidence.v1"
    marketing = (prefs.get("marketing_opt_in") if type(prefs.get("marketing_opt_in")) is bool
                 else None) if native_unknown else bool(prefs.get("marketing_opt_in"))
    stored_temperature = prefs.get("lead_temperature")
    lead_temperature = ((stored_temperature if isinstance(stored_temperature, str) and stored_temperature.strip()
                         else None) if native_unknown else (stored_temperature or "cold"))
    lead_score = (_native_evidence_score(prefs.get("lead_score")) if native_unknown
                  else int(prefs.get("lead_score") or 0))
    summary = prefs.get("last_summary") or (snapshot.summary_text if snapshot else None)
    intent = prefs.get("last_intent") or (snapshot.last_intent if snapshot else None)
    actions = (snapshot.suggested_actions if snapshot else None) or prefs.get("suggested_actions") or []

    return {
        "id": getattr(legacy_user, "id", None) or f"contact:{contact.id}",
        "contact_id": contact.id,
        "name": name,
        "raw_name": serialized_raw_name,
        "name_quality": (
            "sensitive_redacted"
            if name_is_sensitive
            else ("message_excerpt" if _looks_like_message_name(raw_name) else "provided")
        ),
        "profile_excerpt": (
            redact_crm_sensitive_text(raw_name)
            if _looks_like_message_name(raw_name) or name_is_sensitive
            else ""
        ),
        "email": real_email or "",
        "email_raw": raw_email,
        "email_is_placeholder": is_placeholder_email(raw_email),
        "has_real_email": bool(real_email),
        "telefono": phone,
        "phone": phone,
        "whatsapp": phone if str(prefs.get("last_channel") or "").lower() == "whatsapp" else "",
        "canal": prefs.get("last_channel") or prefs.get("preferred_channel") or prefs.get("channel"),
        "channel": prefs.get("last_channel") or prefs.get("preferred_channel") or prefs.get("channel"),
        "origen": prefs.get("last_source") or prefs.get("source"),
        "source": prefs.get("last_source") or prefs.get("source"),
        "acepta_marketing": marketing,
        "marketing": marketing,
        "tags": safe_tags(contact.tags),
        "etiquetas": safe_tags(contact.tags),
        "created_at": contact.created_at.isoformat() if getattr(contact, "created_at", None) else None,
        "last_seen": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
        "ultima_interaccion": contact.last_interaction_at.isoformat() if contact.last_interaction_at else None,
        "contact_type": contact.type,
        "ltv": float(contact.ltv_monetary or 0),
        "total_orders": int(contact.total_orders or 0),
        "summary": redact_crm_sensitive_text(summary),
        "conversation_summary": redact_crm_sensitive_text(summary),
        "motivo": redact_crm_sensitive_text(prefs.get("last_reason") or intent),
        "last_intent": redact_crm_sensitive_text(intent),
        "lead_temperature": lead_temperature,
        "lead_score": lead_score,
        "conversation_status": prefs.get("conversation_status"),
        "suggested_actions": redact_crm_sensitive_value(actions) if isinstance(actions, list) else [],
        "interaction_count": interaction_count,
        "last_message_excerpt": redact_crm_sensitive_text(prefs.get("last_message_excerpt")),
    }


def emit_crm_contact_update(tenant: TenantProfile, payload: dict[str, Any]) -> None:
    if not has_app_context():
        return
    try:
        from socket_service import emit_crm_contact_update as _emit

        _emit(tenant, payload)
    except Exception as exc:
        current_app.logger.warning("[CRM] realtime emit skipped for tenant=%s: %s", getattr(tenant, "id", None), exc)
