from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from extensions import db
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.v2.sla_service import (
    detect_sla_breaches_for_tenant,
    get_policies_for_tenant,
    save_policies_for_tenant,
)

v2_sla_bp = Blueprint("v2_sla", __name__, url_prefix="/api/v2/sla")


def _resolve_tenant_or_error():
    explicit_slug = (request.headers.get("X-Tenant-Slug") or request.args.get("tenant_slug") or "").strip()
    if not explicit_slug:
        return None, (jsonify({"error": "X-Tenant-Slug es obligatorio en SLA v2"}), 400)
    try:
        return resolve_tenant_v2(required=True, explicit_slug=explicit_slug), None
    except V2TenantResolutionError as exc:
        return None, (jsonify({"error": exc.message}), exc.status_code)


@v2_sla_bp.route("/policies", methods=["GET"])
def get_sla_policies_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error
    return jsonify({"policies": get_policies_for_tenant(tenant)})


@v2_sla_bp.route("/policies", methods=["POST"])
def save_sla_policies_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    policies = save_policies_for_tenant(tenant, payload.get("policies") or payload)
    db.session.commit()
    return jsonify({"policies": policies})


@v2_sla_bp.route("/breaches", methods=["GET"])
def list_sla_breaches_v2():
    tenant, error = _resolve_tenant_or_error()
    if error:
        return error

    breaches = detect_sla_breaches_for_tenant(tenant, actor_user=getattr(g, "viewer", None))
    db.session.commit()
    return jsonify({"items": breaches, "total": len(breaches)})
