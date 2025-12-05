from flask import Blueprint, jsonify, request, g
from models import TenantProfile, User, db
from utils.auth_helpers import token_requerido
from utils.admin_decorators import super_admin_required
from sqlalchemy import desc

super_admin_bp = Blueprint('super_admin', __name__, url_prefix='/api/admin')

@super_admin_bp.route('/tenants', methods=['GET'])
@token_requerido
@super_admin_required
def list_tenants(current_user):
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    query = TenantProfile.query.order_by(desc(TenantProfile.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    tenants_data = []
    for tenant in pagination.items:
        # Determine plan from owner
        owner = tenant.municipio or tenant.pyme
        plan = owner.plan if owner else "unknown"
        # Determine status (placeholder, maybe active if owner has access?)
        # We can assume active if not explicit field.
        # Or add a status field to TenantProfile later if needed. For now, derived or static.
        status = "active" # Default

        tenants_data.append({
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": plan,
            "status": status,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None
        })

    return jsonify({
        "tenants": tenants_data,
        "total": pagination.total,
        "pages": pagination.pages,
        "current_page": page
    })

@super_admin_bp.route('/tenants/<string:slug>/status', methods=['PUT'])
@token_requerido
@super_admin_required
def update_tenant_status(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    data = request.get_json()

    plan = data.get('plan')
    status = data.get('status') # Not yet stored in DB, but requested.

    owner = tenant.municipio or tenant.pyme
    if not owner:
        return jsonify({"error": "Tenant has no owner linked"}), 400

    if plan:
        owner.plan = plan
        db.session.add(owner)

    # If we had a status field on TenantProfile, update it here.
    # For now, we only update plan on user.

    try:
        db.session.commit()
        return jsonify({
            "message": "Tenant updated successfully",
            "slug": tenant.slug,
            "plan": owner.plan,
            "status": status or "active"
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
