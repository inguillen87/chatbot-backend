from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from flask import current_app, has_app_context
from sqlalchemy import or_
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
    source = clean_text(metadata.get("source") or metadata.get("event_type"), 80).lower()
    action_id = clean_text(metadata.get("action_id"), 80).lower()
    lower = clean_text(text, 500).lower()
    combined = f"{source} {action_id} {lower}"

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
        if any(needle in combined for needle in needles):
            return intent, reason

    if lower in {"hola", "buenas", "buen dia", "buenas tardes", "menu"}:
        return "saludo", "Primer contacto / saludo"

    return "consulta_general", "Consulta general"


def lead_temperature(intent: str, text: str, metadata: Optional[dict[str, Any]] = None) -> tuple[str, int]:
    lower = clean_text(text, 500).lower()
    metadata = metadata or {}
    action = clean_text(metadata.get("action_id"), 120).lower()
    combined = f"{action} {lower}"

    if intent == "lead_hot" or any(
        word in combined
        for word in ("contratar", "precio", "demo", "ventas", "asesor", "cotizar", "propuesta", "llamen")
    ):
        return "hot", 85
    if intent in {"pedido_catalogo", "reclamo", "encuestas", "educacion", "soporte"}:
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
    if filters:
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
    return contact


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
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    text = clean_text(message_body, 1200)
    intent, reason = classify_reason(text, metadata)
    temp, score = lead_temperature(intent, text, metadata)
    actions = suggested_actions_for(intent, temp)
    summary = build_summary(text, reason=reason, channel=channel, source=source or metadata.get("source"))

    prefs = contact.preferences if isinstance(contact.preferences, dict) else {}
    prefs.update(
        {
            "last_channel": channel,
            "last_source": source or metadata.get("source") or prefs.get("source") or "crm_auto",
            "last_reason": reason,
            "last_summary": summary,
            "last_intent": intent,
            "lead_temperature": temp,
            "lead_score": score,
            "conversation_status": "hot" if temp == "hot" else ("en_seguimiento" if temp == "warm" else "frio"),
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
    db.session.flush()

    payload = serialize_crm_contact(contact, snapshot=snapshot)
    if emit:
        emit_crm_contact_update(tenant, payload)
    return payload


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
        "acepta_marketing": bool(prefs.get("marketing_opt_in")),
        "marketing": bool(prefs.get("marketing_opt_in")),
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
        "lead_temperature": prefs.get("lead_temperature") or "cold",
        "lead_score": int(prefs.get("lead_score") or 0),
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
