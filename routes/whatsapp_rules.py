from __future__ import annotations

from flask import Blueprint, abort, g, jsonify, request

from models import AuditEvent, User, db
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
