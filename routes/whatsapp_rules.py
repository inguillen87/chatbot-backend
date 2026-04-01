from __future__ import annotations

from flask import Blueprint, abort, g, jsonify, request

from models import AuditEvent, NotificationTemplate, User, db
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import token_requerido
from utils.tenant import require_tenant

whatsapp_rules_bp = Blueprint("whatsapp_rules_bp", __name__)


def _guard(user: User, tenant):
    if getattr(user, "rol", None) not in {"admin", "super_admin"}:
        abort(403, description="Permisos insuficientes")
    if not _is_authorized_for_tenant(user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")


@whatsapp_rules_bp.route("/api/admin/whatsapp/rules", methods=["GET"])
@token_requerido
@require_tenant
def get_rules(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    rule = WhatsAppEnterpriseRulesService(tenant.id).get_or_create()
    return jsonify(
        {
            "tenant_id": tenant.id,
            "enforce_template_outside_24h": rule.enforce_template_outside_24h,
            "max_outbound_per_hour": rule.max_outbound_per_hour,
            "quiet_hours_start": rule.quiet_hours_start,
            "quiet_hours_end": rule.quiet_hours_end,
            "blocked_keywords": rule.blocked_keywords or [],
        }
    )


@whatsapp_rules_bp.route("/api/admin/whatsapp/rules", methods=["PUT"])
@token_requerido
@require_tenant
def update_rules(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)

    payload = request.get_json(silent=True) or {}
    svc = WhatsAppEnterpriseRulesService(tenant.id)
    rule = svc.get_or_create()

    if "enforce_template_outside_24h" in payload:
        rule.enforce_template_outside_24h = bool(payload.get("enforce_template_outside_24h"))
    if "max_outbound_per_hour" in payload:
        rule.max_outbound_per_hour = payload.get("max_outbound_per_hour")
    if "quiet_hours_start" in payload:
        rule.quiet_hours_start = payload.get("quiet_hours_start")
    if "quiet_hours_end" in payload:
        rule.quiet_hours_end = payload.get("quiet_hours_end")
    if isinstance(payload.get("blocked_keywords"), list):
        rule.blocked_keywords = payload.get("blocked_keywords")

    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_rules.updated",
            resource_type="whatsapp_enterprise_rule",
            resource_id=str(rule.id),
            details=payload,
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()

    return jsonify({"updated": True})


@whatsapp_rules_bp.route("/api/admin/templates", methods=["GET"])
@token_requerido
@require_tenant
def list_whatsapp_templates(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    templates = (
        NotificationTemplate.query.filter_by(tenant_id=tenant.id, channel="whatsapp")
        .order_by(NotificationTemplate.created_at.desc())
        .all()
    )
    out = []
    for t in templates:
        meta = t.metadata_json if isinstance(t.metadata_json, dict) else {}
        out.append(
            {
                "id": t.id,
                "key": t.key,
                "channel": t.channel,
                "body_template": t.body_template,
                "subject_template": t.subject_template,
                "is_active": t.is_active,
                "health_status": meta.get("health_status", "unknown"),
                "metadata": meta,
            }
        )
    return jsonify(out)


@whatsapp_rules_bp.route("/api/admin/templates", methods=["POST"])
@token_requerido
@require_tenant
def create_whatsapp_template(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}

    key = str(payload.get("key") or "").strip().lower()
    body_template = str(payload.get("body_template") or "").strip()
    if not key or not body_template:
        abort(400, description="key y body_template son requeridos")

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if payload.get("health_status"):
        metadata["health_status"] = str(payload.get("health_status")).strip().lower()

    template = NotificationTemplate(
        tenant_id=tenant.id,
        key=key,
        channel="whatsapp",
        body_template=body_template,
        subject_template=payload.get("subject_template"),
        is_active=bool(payload.get("is_active", True)),
        metadata_json=metadata,
    )
    db.session.add(template)
    db.session.flush()
    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_template.created",
            resource_type="notification_template",
            resource_id=str(template.id),
            details={"key": template.key, "channel": "whatsapp"},
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()
    return jsonify({"id": template.id, "created": True}), 201


@whatsapp_rules_bp.route("/api/admin/templates/<template_id>", methods=["PATCH"])
@token_requerido
@require_tenant
def patch_whatsapp_template(user: User, template_id: str):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}
    template = NotificationTemplate.query.filter_by(id=template_id, tenant_id=tenant.id, channel="whatsapp").first()
    if not template:
        abort(404, description="template not found")

    if "body_template" in payload:
        template.body_template = str(payload.get("body_template") or "").strip()
    if "subject_template" in payload:
        template.subject_template = payload.get("subject_template")
    if "is_active" in payload:
        template.is_active = bool(payload.get("is_active"))

    meta = template.metadata_json if isinstance(template.metadata_json, dict) else {}
    if "health_status" in payload:
        meta["health_status"] = str(payload.get("health_status") or "").strip().lower()
    template.metadata_json = meta

    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="whatsapp_template.updated",
            resource_type="notification_template",
            resource_id=str(template.id),
            details=payload,
            ip_address=request.remote_addr,
        )
    )
    db.session.commit()
    return jsonify({"updated": True, "id": template.id})


@whatsapp_rules_bp.route("/api/notifications/whatsapp/test", methods=["POST"])
@token_requerido
@require_tenant
def test_whatsapp_notification_policy(user: User):
    tenant = g.tenant_profile
    _guard(user, tenant)
    payload = request.get_json(silent=True) or {}
    recipient = payload.get("recipient")
    body = payload.get("body") or ""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata["recipient"] = recipient
    svc = WhatsAppEnterpriseRulesService(tenant.id)
    svc.get_or_create()
    allowed, reason = svc.evaluate_outbound(body=body, metadata=metadata)
    return jsonify({"allowed": allowed, "reason": reason})
