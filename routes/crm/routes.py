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
from models import Order, User
from extensions import db

crm_bp = Blueprint('crm_bp', __name__)


def _serialize_cliente(cliente: User) -> dict:
    return {
        "id": cliente.id,
        "name": cliente.name or "",
        "email": cliente.email or "",
        "telefono": cliente.telefono or "",
        "acepta_marketing": bool(cliente.acepta_marketing),
        "latitud": cliente.latitud,
        "longitud": cliente.longitud,
        "tags": cliente.tags.split(',') if cliente.tags else [],
    }


def _query_clientes_tenant(current_user: User):
    owner_id = current_user.empresa_id or current_user.id
    return User.query.filter_by(empresa_id=owner_id)


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

    text_query = request.args.get('q')
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

    return jsonify([_serialize_cliente(cliente) for cliente in query.all()])


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
    payload = request.get_json(silent=True) or {}

    template_slug = payload.get("template_slug")
    message = payload.get("message")
    contact_ids = payload.get("contact_ids") or []
    dry_run = bool(payload.get("dry_run", False))
    max_per_week = int(payload.get("max_per_week", 2) or 2)
    tz_name = payload.get("timezone") or "UTC"

    if not isinstance(contact_ids, list) or not contact_ids:
        return jsonify({"error": "contact_ids es obligatorio"}), 400

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

    included = []
    excluded_optout = []
    excluded_frequency = []

    for contact in contacts:
        prefs = contact.preferences or {}
        if prefs.get("marketing_opt_out"):
            excluded_optout.append(contact.id)
            continue

        sends_week = _campaign_sends_last_days(tenant.id, contact.id, days=7)
        if sends_week >= max_per_week:
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
                    channel="whatsapp",
                    direction="outbound",
                    content=message,
                    metadata_payload={
                        "event_type": "campaign_send",
                        "campaign_id": campaign_id,
                        "scheduled_for": scheduled_for_utc.isoformat() if scheduled_for_utc else None,
                        "status": "scheduled" if scheduled_for_utc else "queued",
                    },
                )
            )
        db.session.commit()

    return jsonify(
        {
            "campaign_id": campaign_id,
            "mode": "dry_run" if dry_run else "scheduled",
            "scheduled_for_utc": scheduled_for_utc.isoformat() if scheduled_for_utc else None,
            "totals": {
                "requested": len(contact_ids),
                "resolved": len(contacts),
                "eligible": len(included),
                "excluded_optout": len(excluded_optout),
                "excluded_frequency": len(excluded_frequency),
            },
            "excluded_optout": excluded_optout,
            "excluded_frequency": excluded_frequency,
            "eligible_contacts": [c.id for c in included],
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
