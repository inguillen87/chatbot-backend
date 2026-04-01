from __future__ import annotations

from flask import Blueprint, abort, g, jsonify, request

from models import AuditEvent, OrgUnit, Role, User, UserOrgUnit, UserRole, db
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import token_requerido
from utils.tenant import require_tenant

access_control_bp = Blueprint("access_control_bp", __name__)


def _require_admin_for_tenant(current_user, tenant):
    if getattr(current_user, "rol", None) not in {"admin", "super_admin"}:
        abort(403, description="Permisos insuficientes")
    if not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        abort(403, description="Acceso denegado")


def _audit(tenant_id: int, actor_user_id: int | None, event_type: str, resource_type: str, resource_id: str | None, details: dict | None):
    db.session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            ip_address=request.remote_addr,
        )
    )


@access_control_bp.route("/api/admin/org-units", methods=["POST"])
@token_requerido
@require_tenant
def create_org_unit(current_user: User):
    tenant = g.tenant_profile
    _require_admin_for_tenant(current_user, tenant)

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    org_unit = OrgUnit(
        tenant_id=tenant.id,
        name=name,
        parent_id=payload.get("parent_id"),
        is_active=bool(payload.get("is_active", True)),
    )
    db.session.add(org_unit)
    db.session.flush()

    _audit(tenant.id, current_user.id, "org_unit.created", "org_unit", str(org_unit.id), {"name": name})
    db.session.commit()

    return jsonify({"id": org_unit.id, "name": org_unit.name, "tenant_id": org_unit.tenant_id}), 201


@access_control_bp.route("/api/admin/users/<int:user_id>/roles", methods=["POST"])
@token_requerido
@require_tenant
def assign_user_role(current_user: User, user_id: int):
    tenant = g.tenant_profile
    _require_admin_for_tenant(current_user, tenant)

    payload = request.get_json(silent=True) or {}
    role_name = str(payload.get("role") or "").strip().lower()
    if not role_name:
        return jsonify({"error": "role is required"}), 400

    role = Role.query.filter_by(name=role_name).first()
    if not role:
        role = Role(name=role_name)
        db.session.add(role)
        db.session.flush()

    assignment = UserRole.query.filter_by(user_id=user_id, role_id=role.id, tenant_id=tenant.id).first()
    created = False
    if not assignment:
        assignment = UserRole(user_id=user_id, role_id=role.id, tenant_id=tenant.id)
        db.session.add(assignment)
        created = True

    _audit(
        tenant.id,
        current_user.id,
        "user_role.assigned",
        "user",
        str(user_id),
        {"role": role_name, "created": created},
    )
    db.session.commit()

    return jsonify({"user_id": user_id, "role": role_name, "created": created}), (201 if created else 200)


@access_control_bp.route("/api/admin/users/<int:user_id>/org-units", methods=["POST"])
@token_requerido
@require_tenant
def assign_user_org_unit(current_user: User, user_id: int):
    tenant = g.tenant_profile
    _require_admin_for_tenant(current_user, tenant)

    payload = request.get_json(silent=True) or {}
    org_unit_id = payload.get("org_unit_id")
    if not org_unit_id:
        return jsonify({"error": "org_unit_id is required"}), 400

    org_unit = OrgUnit.query.filter_by(id=org_unit_id, tenant_id=tenant.id).first()
    if not org_unit:
        return jsonify({"error": "org_unit not found"}), 404

    assignment = UserOrgUnit.query.filter_by(user_id=user_id, org_unit_id=org_unit.id).first()
    created = False
    if not assignment:
        assignment = UserOrgUnit(user_id=user_id, org_unit_id=org_unit.id, tenant_id=tenant.id)
        db.session.add(assignment)
        created = True

    _audit(
        tenant.id,
        current_user.id,
        "user_org_unit.assigned",
        "user",
        str(user_id),
        {"org_unit_id": org_unit.id, "created": created},
    )
    db.session.commit()

    return jsonify({"user_id": user_id, "org_unit_id": org_unit.id, "created": created}), (201 if created else 200)


@access_control_bp.route("/api/admin/audit/events", methods=["GET"])
@token_requerido
@require_tenant
def list_audit_events(current_user: User):
    tenant = g.tenant_profile
    _require_admin_for_tenant(current_user, tenant)

    limit = min(int(request.args.get("limit") or 50), 200)
    event_type = (request.args.get("event_type") or "").strip()

    query = AuditEvent.query.filter_by(tenant_id=tenant.id)
    if event_type:
        query = query.filter(AuditEvent.event_type == event_type)

    items = query.order_by(AuditEvent.created_at.desc()).limit(limit).all()
    return jsonify(
        [
            {
                "id": item.id,
                "event_type": item.event_type,
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "actor_user_id": item.actor_user_id,
                "details": item.details or {},
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in items
        ]
    )
