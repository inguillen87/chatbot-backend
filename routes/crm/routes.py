from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, g, request
from sqlalchemy import or_

from utils.auth_helpers import token_requerido
from utils.auth_helpers import admin_o_empleado_requerido
from middleware.tenant_context import require_tenant
from models_memory import Contact, ContactSnapshot, InteractionEvent
from models import Order, TenantProfile, User
from extensions import db
from services.contact_intake import is_placeholder_email, normalize_email

crm_bp = Blueprint('crm_bp', __name__)


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _clean_phone(value: str | None) -> str:
    return str(value or "").strip()


def _looks_like_message_name(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    message_markers = (
        " es tu codigo",
        " es tu código",
        "no lo compartas",
        "quisiera saber",
        "hola ",
        "hola.",
        "hola hola",
        "para que servis",
        "para qué servís",
    )
    return any(marker in text for marker in message_markers) or len(text) > 80


def _safe_tag_list(*values) -> list[str]:
    out: list[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, list):
            raw_items = value
        else:
            raw_items = str(value).split(",")
        for item in raw_items:
            tag = str(item or "").strip()
            if tag and tag not in out:
                out.append(tag)
    return out


def _contact_channel(cliente: User, contact: Contact | None) -> str | None:
    prefs = (contact.preferences or {}) if contact else {}
    for key in ("channel", "canal", "source_channel", "last_channel"):
        value = str(prefs.get(key) or "").strip()
        if value:
            return value
    if _clean_phone(getattr(cliente, "telefono", None)) or is_placeholder_email(getattr(cliente, "email", None)):
        return "whatsapp"
    if normalize_email(getattr(cliente, "email", None)):
        return "email"
    return None


def _contact_source(cliente: User, contact: Contact | None) -> str:
    prefs = (contact.preferences or {}) if contact else {}
    source = str(prefs.get("source") or prefs.get("origen") or "").strip()
    if source:
        return source
    if is_placeholder_email(getattr(cliente, "email", None)):
        return "whatsapp_auto"
    return "crm"


def _serialize_cliente(cliente: User, contact: Contact | None = None) -> dict:
    raw_email = cliente.email or ""
    real_email = normalize_email(raw_email)
    phone = _clean_phone(cliente.telefono or (contact.phone if contact else None))
    raw_name = cliente.name or ""
    name_is_message = _looks_like_message_name(raw_name)
    display_name = (contact.name if contact and contact.name else raw_name).strip()
    if name_is_message or not display_name:
        display_name = "Contacto WhatsApp" if phone else "Contacto sin identificar"
    channel = _contact_channel(cliente, contact)
    source = _contact_source(cliente, contact)
    accepts_marketing = bool(
        getattr(cliente, "acepta_marketing", False)
        or ((contact.preferences or {}).get("marketing_opt_in") if contact else False)
    )
    last_seen = (contact.last_interaction_at if contact else None) or getattr(cliente, "fecha_creacion", None)
    tags = _safe_tag_list(cliente.tags, contact.tags if contact else None)

    return {
        "id": cliente.id,
        "name": display_name,
        "raw_name": raw_name,
        "name_quality": "message_excerpt" if name_is_message else "provided",
        "profile_excerpt": raw_name if name_is_message else "",
        "email": real_email or "",
        "email_raw": raw_email,
        "email_is_placeholder": is_placeholder_email(raw_email),
        "has_real_email": bool(real_email),
        "telefono": phone,
        "phone": phone,
        "whatsapp": phone if channel == "whatsapp" else "",
        "canal": channel,
        "channel": channel,
        "origen": source,
        "source": source,
        "acepta_marketing": accepts_marketing,
        "marketing": accepts_marketing,
        "latitud": cliente.latitud,
        "longitud": cliente.longitud,
        "tags": tags,
        "etiquetas": tags,
        "created_at": _iso_or_none(getattr(cliente, "fecha_creacion", None)),
        "last_seen": _iso_or_none(last_seen),
        "ultima_interaccion": _iso_or_none(last_seen),
        "contact_id": contact.id if contact else None,
        "contact_type": contact.type if contact else None,
        "ltv": float(contact.ltv_monetary or 0) if contact else 0,
        "total_orders": int(contact.total_orders or 0) if contact else 0,
    }


def _query_clientes_tenant(current_user: User):
    owner_id = current_user.empresa_id or current_user.id
    return User.query.filter_by(empresa_id=owner_id)


def _resolve_crm_tenant(current_user: User, slug: str | None = None) -> TenantProfile | None:
    raw_slug = (slug or request.args.get("tenant_slug") or request.args.get("tenant") or "").strip()
    slug_candidate = raw_slug.lower()
    if raw_slug and slug_candidate not in {"crm", "usuarios", "perfil", "admin", "app"}:
        tenant = TenantProfile.query.filter_by(slug=raw_slug).first()
        if tenant:
            return tenant

    if getattr(current_user, "tenant_id", None):
        tenant = TenantProfile.query.get(current_user.tenant_id)
        if tenant:
            return tenant

    owner_id = current_user.empresa_id or current_user.id
    return TenantProfile.query.filter(
        or_(
            TenantProfile.pyme_id == owner_id,
            TenantProfile.municipio_id == owner_id,
        )
    ).first()


def _contact_for_legacy_user(tenant: TenantProfile, cliente: User) -> Contact:
    contact = None
    if cliente.telefono:
        contact = Contact.query.filter_by(tenant_id=tenant.id, phone=cliente.telefono).first()
    real_email = normalize_email(cliente.email)
    if contact is None and real_email:
        contact = Contact.query.filter_by(tenant_id=tenant.id, email=real_email).first()
    if contact is not None:
        return contact

    contact = Contact(
        id=str(uuid4()),
        tenant_id=tenant.id,
        name=cliente.name,
        phone=cliente.telefono,
        email=real_email,
        type="lead",
        tags=[tag.strip() for tag in (cliente.tags or "").split(",") if tag.strip()],
        preferences={
            "marketing_opt_in": bool(cliente.acepta_marketing),
            "source": "legacy_user",
            "legacy_user_id": cliente.id,
        },
    )
    db.session.add(contact)
    db.session.flush()
    return contact


def _parse_scheduled_for(raw_value: str | None, tz_name: str | None) -> datetime | None:
    if not raw_value:
        return None

    try:
        parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError("scheduled_for debe ser ISO8601 válido") from exc

    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)

    zone = ZoneInfo(tz_name or "UTC")
    return parsed.replace(tzinfo=zone).astimezone(timezone.utc)


def _tenant_templates(tenant) -> list[dict]:
    cfg = tenant.configuracion or {}
    templates = cfg.get("campaign_templates")
    return templates if isinstance(templates, list) else []


def _save_tenant_templates(tenant, templates: list[dict]) -> None:
    cfg = tenant.configuracion or {}
    cfg["campaign_templates"] = templates
    tenant.configuracion = cfg


def _campaign_sends_last_days(tenant_id: int, contact_id: str, days: int = 7) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return InteractionEvent.query.filter(
        InteractionEvent.tenant_id == tenant_id,
        InteractionEvent.contact_id == contact_id,
        InteractionEvent.direction == "outbound",
        InteractionEvent.metadata_payload["event_type"].astext == "campaign_send",
        InteractionEvent.created_at >= since,
    ).count()


def _campaign_sends_last_hours(tenant_id: int, contact_id: str, hours: int = 24) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return InteractionEvent.query.filter(
        InteractionEvent.tenant_id == tenant_id,
        InteractionEvent.contact_id == contact_id,
        InteractionEvent.direction == "outbound",
        InteractionEvent.metadata_payload["event_type"].astext == "campaign_send",
        InteractionEvent.created_at >= since,
    ).count()


@crm_bp.route('/api/crm/clientes', methods=['GET'])
@crm_bp.route('/crm/clientes', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def list_legacy_clients(current_user):
    """Compatibilidad para frontends legacy que consultan /crm/clientes."""
    query = _query_clientes_tenant(current_user)

    tag = request.args.get('tag')
    if tag:
        query = query.filter(User.tags.ilike(f"%{tag}%"))

    text_query = request.args.get('q') or request.args.get('search')
    if text_query:
        like = f"%{text_query}%"
        query = query.filter(
            or_(
                User.name.ilike(like),
                User.email.ilike(like),
                User.telefono.ilike(like),
            )
        )

    sort = request.args.get('sort')
    order = (request.args.get('order') or 'asc').lower()
    if sort not in {"name", "email", "telefono", "id"}:
        sort = "name"
    column = getattr(User, sort)
    query = query.order_by(column.desc() if order == 'desc' else column.asc())

    limit = request.args.get('limit')
    offset = request.args.get('offset')
    try:
        if offset is not None and int(offset) >= 0:
            query = query.offset(int(offset))
    except (TypeError, ValueError):
        pass
    try:
        if limit is not None and int(limit) >= 0:
            query = query.limit(int(limit))
    except (TypeError, ValueError):
        pass

    clientes = query.all()
    tenant = _resolve_crm_tenant(current_user)
    contact_by_phone: dict[str, Contact] = {}
    contact_by_email: dict[str, Contact] = {}
    if tenant and clientes:
        phones = [_clean_phone(cliente.telefono) for cliente in clientes if _clean_phone(cliente.telefono)]
        emails = [normalize_email(cliente.email) for cliente in clientes if normalize_email(cliente.email)]
        filters = []
        if phones:
            filters.append(Contact.phone.in_(phones))
        if emails:
            filters.append(Contact.email.in_(emails))
        if filters:
            for contact in Contact.query.filter(Contact.tenant_id == tenant.id, or_(*filters)).all():
                if contact.phone:
                    contact_by_phone[str(contact.phone).strip()] = contact
                if contact.email:
                    contact_by_email[str(contact.email).strip().lower()] = contact

    data = []
    for cliente in clientes:
        real_email = normalize_email(cliente.email)
        contact = (
            contact_by_phone.get(_clean_phone(cliente.telefono))
            or (contact_by_email.get(real_email) if real_email else None)
        )
        data.append(_serialize_cliente(cliente, contact))

    return jsonify(data)


@crm_bp.route('/api/admin/tenants/<slug>/contacts', methods=['GET'])
@token_requerido
@require_tenant
def list_contacts(current_user, slug):
    tenant = g.tenant_profile
    contacts = Contact.query.filter_by(tenant_id=tenant.id).limit(50).all()

    return jsonify({
        "contacts": [{
            "id": c.id,
            "name": c.name or "Unknown",
            "phone": c.phone,
            "type": c.type,
            "total_orders": c.total_orders,
            "ltv": float(c.ltv_monetary or 0),
            "last_interaction": c.last_interaction_at.isoformat() if c.last_interaction_at else None
        } for c in contacts]
    })


@crm_bp.route('/api/admin/tenants/<slug>/contacts/<contact_id>/history', methods=['GET'])
@token_requerido
@require_tenant
def get_contact_history(current_user, slug, contact_id):
    tenant = g.tenant_profile
    contact = Contact.query.filter_by(id=contact_id, tenant_id=tenant.id).first_or_404()
    snapshot = ContactSnapshot.query.filter_by(contact_id=contact.id).first()

    orders = Order.query.filter_by(tenant_id=tenant.id)\
        .filter(Order.buyer_phone == contact.phone)\
        .order_by(Order.created_at.desc()).limit(10).all()

    interactions = InteractionEvent.query.filter_by(contact_id=contact.id)\
        .order_by(InteractionEvent.created_at.desc()).limit(20).all()

    return jsonify({
        "contact": {
            "id": contact.id,
            "name": contact.name,
            "phone": contact.phone,
            "tags": contact.tags,
            "preferences": contact.preferences
        },
        "snapshot": {
            "summary": snapshot.summary_text if snapshot else None,
            "last_intent": snapshot.last_intent if snapshot else None,
            "suggested_actions": snapshot.suggested_actions if snapshot else []
        },
        "orders": [o.to_dict() for o in orders],
        "interactions": [{
            "channel": i.channel,
            "direction": i.direction,
            "content": i.content,
            "ts": i.created_at.isoformat()
        } for i in interactions]
    })


@crm_bp.route('/api/admin/tenants/<slug>/campaigns/templates', methods=['GET'])
@token_requerido
@require_tenant
def campaign_templates_list(current_user, slug):
    tenant = g.tenant_profile
    return jsonify({"templates": _tenant_templates(tenant)})


@crm_bp.route('/api/admin/tenants/<slug>/campaigns/templates', methods=['POST'])
@token_requerido
@require_tenant
def campaign_templates_create(current_user, slug):
    tenant = g.tenant_profile
    payload = request.get_json(silent=True) or {}

    slug_tpl = str(payload.get("slug") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    message = str(payload.get("message") or "").strip()

    if not slug_tpl or not name or not message:
        return jsonify({"error": "slug, name y message son obligatorios"}), 400

    templates = _tenant_templates(tenant)
    if any(t.get("slug") == slug_tpl for t in templates):
        return jsonify({"error": "Template ya existe"}), 409

    template = {
        "slug": slug_tpl,
        "name": name,
        "message": message,
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    templates.append(template)
    _save_tenant_templates(tenant, templates)
    db.session.commit()
    return jsonify(template), 201


@crm_bp.route('/api/admin/tenants/<slug>/campaigns/templates/<string:template_slug>', methods=['PUT'])
@token_requerido
@require_tenant
def campaign_templates_update(current_user, slug, template_slug):
    tenant = g.tenant_profile
    payload = request.get_json(silent=True) or {}
    templates = _tenant_templates(tenant)
    template = next((t for t in templates if t.get("slug") == template_slug), None)
    if not template:
        return jsonify({"error": "Template no encontrado"}), 404

    for key in ["name", "message"]:
        if key in payload:
            template[key] = str(payload.get(key) or "").strip()
    template["version"] = int(template.get("version") or 1) + 1
    template["updated_at"] = datetime.now(timezone.utc).isoformat()

    _save_tenant_templates(tenant, templates)
    db.session.commit()
    return jsonify(template)


@crm_bp.route('/api/admin/tenants/<slug>/campaigns/opt-out/<string:contact_id>', methods=['POST'])
@token_requerido
@require_tenant
def campaign_opt_out(current_user, slug, contact_id):
    tenant = g.tenant_profile
    contact = Contact.query.filter_by(id=contact_id, tenant_id=tenant.id).first_or_404()
    prefs = contact.preferences or {}
    prefs["marketing_opt_out"] = True
    contact.preferences = prefs

    db.session.add(
        InteractionEvent(
            tenant_id=tenant.id,
            contact_id=contact.id,
            channel="crm",
            direction="outbound",
            content="opt-out registrado",
            metadata_payload={"event_type": "campaign_opt_out"},
        )
    )
    db.session.commit()
    return jsonify({"contact_id": contact.id, "marketing_opt_out": True})


@crm_bp.route('/api/admin/tenants/<slug>/campaigns/send', methods=['POST'])
@token_requerido
@require_tenant
def campaign_send(current_user, slug):
    tenant = g.tenant_profile
    return _send_campaign_for_tenant(current_user, tenant)


@crm_bp.route('/api/crm/campaigns/send', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def legacy_campaign_send(current_user):
    tenant = _resolve_crm_tenant(current_user)
    if not tenant:
        return jsonify({
            "ok": False,
            "reason_code": "tenant_not_resolved",
            "message": "No se pudo resolver el tenant para enviar la campania.",
        }), 400
    return _send_campaign_for_tenant(current_user, tenant)


def _send_campaign_for_tenant(current_user: User, tenant: TenantProfile):
    payload = request.get_json(silent=True) or {}

    template_slug = payload.get("template_slug")
    message = payload.get("message")
    contact_ids = payload.get("contact_ids") or []
    user_ids = payload.get("user_ids") or payload.get("legacy_user_ids") or []
    dry_run = bool(payload.get("dry_run", False))
    try:
        max_per_week = int(payload.get("max_per_week", 2) or 2)
        min_interval_hours = int(payload.get("min_interval_hours", 24) or 24)
    except (TypeError, ValueError):
        return jsonify({"error": "max_per_week y min_interval_hours deben ser numericos"}), 400
    if max_per_week < 1 or min_interval_hours < 1:
        return jsonify({"error": "max_per_week y min_interval_hours deben ser mayores a cero"}), 400
    channel = str(payload.get("channel") or "whatsapp").strip().lower()
    tz_name = payload.get("timezone") or "UTC"

    if not isinstance(contact_ids, list):
        contact_ids = []
    if not isinstance(user_ids, list):
        user_ids = []
    if not contact_ids and not user_ids:
        return jsonify({"error": "contact_ids o user_ids es obligatorio"}), 400
    if channel not in {"whatsapp", "email"}:
        return jsonify({"error": "channel debe ser whatsapp o email"}), 400

    if template_slug and not message:
        template = next((t for t in _tenant_templates(tenant) if t.get("slug") == template_slug), None)
        if not template:
            return jsonify({"error": "Template no encontrado"}), 404
        message = template.get("message")

    if not message:
        return jsonify({"error": "message es obligatorio"}), 400

    try:
        scheduled_for_utc = _parse_scheduled_for(payload.get("scheduled_for"), tz_name)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    contacts = Contact.query.filter(
        Contact.tenant_id == tenant.id,
        Contact.id.in_(contact_ids),
    ).all()

    if user_ids:
        try:
            normalized_user_ids = [int(value) for value in user_ids]
        except (TypeError, ValueError):
            return jsonify({"error": "user_ids debe contener ids numericos"}), 400

        legacy_clients = _query_clientes_tenant(current_user).filter(User.id.in_(normalized_user_ids)).all()
        existing_contact_ids = {contact.id for contact in contacts}
        for cliente in legacy_clients:
            contact = _contact_for_legacy_user(tenant, cliente)
            if contact.id not in existing_contact_ids:
                contacts.append(contact)
                existing_contact_ids.add(contact.id)

    included = []
    excluded_optout = []
    excluded_frequency = []
    excluded_without_channel = []

    for contact in contacts:
        prefs = contact.preferences or {}
        if prefs.get("marketing_opt_out") or prefs.get("marketing_opt_in") is False:
            excluded_optout.append(contact.id)
            continue

        if channel == "whatsapp" and not contact.phone:
            excluded_without_channel.append(contact.id)
            continue
        if channel == "email" and not contact.email:
            excluded_without_channel.append(contact.id)
            continue

        sends_week = _campaign_sends_last_days(tenant.id, contact.id, days=7)
        sends_interval = _campaign_sends_last_hours(tenant.id, contact.id, hours=min_interval_hours)
        if sends_week >= max_per_week or sends_interval > 0:
            excluded_frequency.append(contact.id)
            continue

        included.append(contact)

    campaign_id = str(uuid4())

    if not dry_run:
        for contact in included:
            db.session.add(
                InteractionEvent(
                    tenant_id=tenant.id,
                    contact_id=contact.id,
                    channel=channel,
                    direction="outbound",
                    content=message,
                    metadata_payload={
                        "event_type": "campaign_send",
                        "campaign_id": campaign_id,
                        "scheduled_for": scheduled_for_utc.isoformat() if scheduled_for_utc else None,
                        "status": "scheduled" if scheduled_for_utc else "queued",
                        "channel": channel,
                        "min_interval_hours": min_interval_hours,
                    },
                )
            )
        db.session.commit()

    return jsonify(
        {
            "campaign_id": campaign_id,
            "mode": "dry_run" if dry_run else "scheduled",
            "channel": channel,
            "scheduled_for_utc": scheduled_for_utc.isoformat() if scheduled_for_utc else None,
            "totals": {
                "requested": len(contact_ids) + len(user_ids),
                "resolved": len(contacts),
                "eligible": len(included),
                "excluded_optout": len(excluded_optout),
                "excluded_frequency": len(excluded_frequency),
                "excluded_without_channel": len(excluded_without_channel),
            },
            "excluded_optout": excluded_optout,
            "excluded_frequency": excluded_frequency,
            "excluded_without_channel": excluded_without_channel,
            "eligible_contacts": [c.id for c in included],
            "request_id": campaign_id,
        }
    )


@crm_bp.route('/api/admin/tenants/<slug>/campaigns/<string:campaign_id>/metrics', methods=['GET'])
@token_requerido
@require_tenant
def campaign_metrics(current_user, slug, campaign_id):
    tenant = g.tenant_profile
    base = InteractionEvent.query.filter(
        InteractionEvent.tenant_id == tenant.id,
        InteractionEvent.metadata_payload["campaign_id"].astext == campaign_id,
        InteractionEvent.metadata_payload["event_type"].astext == "campaign_send",
    )

    sent = base.count()
    scheduled = base.filter(InteractionEvent.metadata_payload["status"].astext == "scheduled").count()
    queued = base.filter(InteractionEvent.metadata_payload["status"].astext == "queued").count()

    return jsonify(
        {
            "campaign_id": campaign_id,
            "metrics": {
                "sent_or_queued": sent,
                "scheduled": scheduled,
                "queued": queued,
            },
        }
    )
