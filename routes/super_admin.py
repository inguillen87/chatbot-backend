from flask import Blueprint, jsonify, request, g, current_app
from models import TenantProfile, User, db, generate_token
from utils.auth_helpers import token_requerido
from utils.admin_decorators import super_admin_required
from sqlalchemy import desc
from datetime import datetime, timezone, timedelta
import jwt

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

@super_admin_bp.route('/tenants', methods=['POST'])
@token_requerido
@super_admin_required
def create_tenant(current_user):
    data = request.get_json() or {}
    slug = data.get('slug')
    nombre = data.get('nombre')
    tipo = data.get('tipo', 'pyme')
    email_admin = data.get('email_admin')

    if not slug or not nombre or not email_admin:
        return jsonify({"error": "Faltan datos (slug, nombre, email_admin)"}), 400

    if TenantProfile.query.filter_by(slug=slug).first():
        return jsonify({"error": "Slug ya existe"}), 409

    # Create Owner User if not exists
    owner = User.query.filter_by(email=email_admin).first()
    if not owner:
        owner = User(
            email=email_admin,
            name=f"Admin {nombre}",
            rol="admin",
            tipo_chat=tipo,
            token=generate_token()
        )
        owner.set_password("changeme") # Default password, should be changed
        db.session.add(owner)
        db.session.flush() # Get ID

    # Create Tenant
    tenant = TenantProfile(
        slug=slug,
        nombre=nombre,
        tipo=tipo,
        municipio_id=owner.id if tipo == 'municipio' else None,
        pyme_id=owner.id if tipo == 'pyme' else None
    )
    db.session.add(tenant)
    db.session.commit()

    return jsonify({"message": "Tenant creado", "id": tenant.id, "slug": tenant.slug}), 201

@super_admin_bp.route('/tenants/<string:slug>/status', methods=['PUT'])
@token_requerido
@super_admin_required
def update_tenant_status(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    data = request.get_json()

    plan = data.get('plan')
    status = data.get('status')

    owner = tenant.municipio or tenant.pyme
    if not owner:
        return jsonify({"error": "Tenant has no owner linked"}), 400

    if plan:
        owner.plan = plan
        db.session.add(owner)

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

@super_admin_bp.route('/tenants/<string:slug>/impersonate', methods=['POST'])
@token_requerido
@super_admin_required
def impersonate_tenant(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme

    if not owner:
        return jsonify({"error": "Tenant sin owner"}), 400

    # Generate short-lived token for owner
    payload = {
        'user_id': owner.id,
        'rol': owner.rol,
        'tipo_chat': owner.tipo_chat,
        'empresa_id': owner.empresa_id,
        'municipio_id': owner.municipio_id,
        'tenant_slug': tenant.slug,
        'impersonated_by': current_user.id,
        'exp': datetime.now(timezone.utc) + timedelta(minutes=60)
    }
    token = jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    return jsonify({"token": token, "redirect_url": f"/portal/{tenant.slug}/admin"})
