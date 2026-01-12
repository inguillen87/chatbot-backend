from flask import Blueprint, jsonify, request, g, current_app
from models import TenantProfile, User, db, generate_token, WhatsappNumero, Rubro
from utils.auth_helpers import token_requerido
from utils.admin_decorators import super_admin_required
from sqlalchemy import desc
from datetime import datetime, timezone, timedelta
from services.tenant_management.folder_manager import ensure_tenant_folder_structure
from services.plan_config import apply_plan_to_user, get_plan_metadata
import jwt

super_admin_bp = Blueprint('super_admin', __name__, url_prefix='/api/admin')


def _normalize_plan_key(raw_plan: str | None) -> str:
    if not raw_plan:
        return "gratis"
    normalized = str(raw_plan).strip().lower()
    if normalized in {"free", "demo"}:
        normalized = "gratis"
    if get_plan_metadata(normalized) is None:
        return "gratis"
    return normalized

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
        # IMPROVED LOGIC: Determine plan from owner or tenant, ensuring owner is fetched.
        owner = tenant.municipio or tenant.pyme
        plan = "gratis"
        if owner:
            plan = owner.plan or tenant.plan or "gratis"
        else:
            plan = tenant.plan or "gratis"
        plan = _normalize_plan_key(plan)

        # IMPROVED LOGIC: Status is derived from is_active field.
        status = "active" if tenant.is_active else "inactive"

        tenants_data.append({
            "id": tenant.id,
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": plan,
            "status": status,
            "is_active": tenant.is_active,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
            "owner_email": owner.email if owner else None  # Added as requested
        })

    return jsonify({
        "tenants": tenants_data,
        "total": pagination.total,
        "pages": pagination.pages,
        "current_page": page
    })

@super_admin_bp.route('/tenants/<string:slug>/metrics', methods=['GET'])
@token_requerido
@super_admin_required
def get_tenant_metrics(current_user, slug):
    """
    Returns summarized CRM metrics for a specific tenant:
    - Messages received
    - Orders placed
    - Tickets/Claims created
    - Basic heatmap data (aggregated points)
    """
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()

    # 1. Message Volume (Conversations or NotificationLog)
    # Using Conversacion for inbound/outbound estimation
    # Filter last 30 days
    since = datetime.now(timezone.utc) - timedelta(days=30)

    # Note: Conversacion table uses 'pyme_id' or 'user_id' which maps to User, not directly tenant_id often.
    # We resolve the owner user ID.
    owner_id = getattr(tenant, 'pyme_id', None) or getattr(tenant, 'municipio_id', None)

    msg_count = 0
    if owner_id:
        # Count user interactions (questions)
        from models import Conversacion
        msg_count = Conversacion.query.filter(
            (Conversacion.pyme_id == owner_id) | (Conversacion.user_id == owner_id),
            Conversacion.timestamp >= since
        ).count()

    # 2. Orders (MarketOrder)
    from models import MarketOrder
    order_count = MarketOrder.query.filter_by(tenant_id=tenant.id).filter(
        MarketOrder.created_at >= since
    ).count()

    # 3. Claims/Tickets
    ticket_count = 0
    if tenant.tipo == 'municipio':
        from models import MunicipioTicket
        ticket_count = MunicipioTicket.query.filter_by(tenant_id=tenant.id).filter(
            MunicipioTicket.fecha >= since
        ).count()
    else:
        from models import PymeTicket
        ticket_count = PymeTicket.query.filter_by(tenant_id=tenant.id).filter(
            PymeTicket.fecha >= since
        ).count()

    return jsonify({
        "period": "30d",
        "messages": msg_count,
        "orders": order_count,
        "tickets": ticket_count,
        "plan": tenant.plan
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
        plan=_normalize_plan_key(data.get('plan')),
        is_active=True,
        municipio_id=owner.id if tipo == 'municipio' else None,
        pyme_id=owner.id if tipo == 'pyme' else None
    )
    db.session.add(tenant)
    db.session.commit()

    # Ensure Professional Folder Structure
    try:
        folder_path = ensure_tenant_folder_structure(slug, nombre, tipo)
        current_app.logger.info(f"Created professional folder structure for new tenant {slug} at {folder_path}")
    except Exception as e:
        current_app.logger.error(f"Failed to create folder structure for {slug}: {e}")
        # Proceed, don't fail the request, but log it.

    return jsonify({"message": "Tenant creado", "id": tenant.id, "slug": tenant.slug}), 201

@super_admin_bp.route('/tenants/<string:slug>', methods=['GET'])
@token_requerido
@super_admin_required
def get_tenant_detail(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme

    return jsonify({
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "plan": tenant.plan,
        "is_active": getattr(tenant, 'is_active', True),
        "owner_email": owner.email if owner else None,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "whatsapp_sender_id": tenant.whatsapp_sender_id
    })

@super_admin_bp.route('/tenants/<string:slug>', methods=['PUT'])
@token_requerido
@super_admin_required
def update_tenant_full(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    data = request.get_json() or {}

    if 'nombre' in data: tenant.nombre = data['nombre']
    if 'plan' in data:
        normalized_plan = _normalize_plan_key(data['plan'])
        tenant.plan = normalized_plan

        users_to_update = set()

        # Method 1: Find the owner via TenantProfile's FK and their employees
        owner = tenant.municipio or tenant.pyme
        if owner:
            users_to_update.add(owner)
            if owner.id: # safety check
                employees = User.query.filter(User.empresa_id == owner.id).all()
                for emp in employees:
                    users_to_update.add(emp)

        # Method 2: Find all users directly linked via User.tenant_id
        direct_members = User.query.filter(User.tenant_id == tenant.id).all()
        for member in direct_members:
            users_to_update.add(member)

        # Method 3 (Fallback for demo/legacy tenants): Find users whose pyme_id/municipio_id points to this tenant's ID
        if tenant.tipo == 'pyme':
            fallback_members = User.query.filter(User.pyme_id == tenant.id).all()
            for member in fallback_members:
                users_to_update.add(member)
        elif tenant.tipo == 'municipio':
            fallback_members = User.query.filter(User.municipio_id == tenant.id).all()
            for member in fallback_members:
                users_to_update.add(member)

        if not users_to_update:
            current_app.logger.warning(f"SA:update_plan: No associated users found for tenant {tenant.slug}. Plan will not be propagated.")
        else:
             current_app.logger.info(f"SA:update_plan: Found {len(users_to_update)} users to update for tenant {tenant.slug}.")

        for user in users_to_update:
            apply_plan_to_user(user, normalized_plan)
            current_app.logger.info(f"Applied plan '{normalized_plan}' to user {user.id} ({user.email}) for tenant {tenant.slug}")

    if 'is_active' in data: tenant.is_active = bool(data['is_active'])
    if 'whatsapp_sender_id' in data: tenant.whatsapp_sender_id = data['whatsapp_sender_id']

    # Handle domain, etc if needed
    if 'dominio' in data: tenant.dominio = data['dominio']

    try:
        db.session.commit()
        return jsonify({
            "message": "Tenant updated successfully",
            "slug": tenant.slug,
            "is_active": tenant.is_active,
            "plan_applied": tenant.plan
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@super_admin_bp.route('/tenants/<string:slug>', methods=['DELETE'])
@token_requerido
@super_admin_required
def delete_tenant_soft(current_user, slug):
    """Soft delete (deactivate) tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    tenant.is_active = False
    db.session.commit()
    return jsonify({"message": "Tenant deactivated successfully"})

@super_admin_bp.route('/tenants/<string:slug>/activate', methods=['POST'])
@token_requerido
@super_admin_required
def activate_tenant(current_user, slug):
    """Re-activate tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    tenant.is_active = True
    db.session.commit()
    return jsonify({"message": "Tenant activated successfully"})

# Deprecated but kept for backward compatibility if frontend uses it
@super_admin_bp.route('/tenants/<string:slug>/status', methods=['PUT'])
@token_requerido
@super_admin_required
def update_tenant_status(current_user, slug):
    return update_tenant_full(current_user, slug)

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

@super_admin_bp.route('/tenants/<string:slug>/admin-user', methods=['POST'])
@token_requerido
@super_admin_required
def create_tenant_admin(current_user, slug):
    """Create or link an admin user to the tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    data = request.get_json() or {}
    email = data.get('email')
    password = data.get('password')
    name = data.get('name')

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos"}), 400

    existing_user = User.query.filter_by(email=email).first()

    if existing_user:
        # Check if already linked
        if existing_user.tenant_id == tenant.id:
            return jsonify({"message": "Usuario ya existe y está vinculado al tenant", "user_id": existing_user.id}), 200
        # If user exists but not linked, we might link them, but be careful of overriding
        return jsonify({"error": "El usuario ya existe pero no está vinculado a este tenant. Use endpoints de actualización."}), 409

    # Create new user
    rubro = Rubro.query.filter_by(clave="default").first() or Rubro.query.first()

    new_user = User(
        email=email,
        name=name or f"Admin {tenant.nombre}",
        rol=f"admin_{tenant.tipo}", # admin_pyme or admin_municipio
        tipo_chat=tenant.tipo,
        tenant_id=tenant.id,
        rubro_id=rubro.id if rubro else None,
        email_verified=True,
        acepto_terminos=True,
        token=generate_token()
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.flush()

    # Link as owner if missing
    if tenant.tipo == 'pyme' and not tenant.pyme_id:
        tenant.pyme_id = new_user.id
    elif tenant.tipo == 'municipio' and not tenant.municipio_id:
        tenant.municipio_id = new_user.id

    db.session.commit()
    return jsonify({"message": "Admin user created successfully", "user_id": new_user.id}), 201

@super_admin_bp.route('/tenants/<string:slug>/password', methods=['PUT'])
@token_requerido
@super_admin_required
def reset_tenant_password(current_user, slug):
    """Reset password for the tenant's owner/admin."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme
    data = request.get_json() or {}
    new_password = data.get('password')

    if not owner:
        return jsonify({"error": "Tenant no tiene usuario propietario asignado"}), 404

    if not new_password:
        return jsonify({"error": "Password requerido"}), 400

    owner.set_password(new_password)
    db.session.commit()
    return jsonify({"message": "Contraseña actualizada correctamente"})

@super_admin_bp.route('/tenants/<string:slug>/whatsapp', methods=['PUT'])
@token_requerido
@super_admin_required
def configure_tenant_whatsapp(current_user, slug):
    """Configure WhatsApp number mapping for the tenant."""
    tenant = TenantProfile.query.filter_by(slug=slug).first_or_404()
    owner = tenant.municipio or tenant.pyme
    data = request.get_json() or {}
    number = data.get('number') # Expected format: +549...

    if not owner:
        return jsonify({"error": "Tenant no tiene usuario propietario asignado"}), 404

    if not number:
        return jsonify({"error": "Número de WhatsApp requerido"}), 400

    # 1. Update Tenant Profile
    tenant.whatsapp_sender_id = f"whatsapp:{number}" # Ensure Twilio format

    # 2. Update/Create WhatsappNumero mapping
    mapping = WhatsappNumero.query.filter_by(numero_whatsapp=number).first()
    if mapping:
        # Reassign if needed
        if mapping.user_id != owner.id:
            mapping.user_id = owner.id
            mapping.is_active = True
    else:
        mapping = WhatsappNumero(
            numero_whatsapp=number,
            user_id=owner.id,
            is_active=True
        )
        db.session.add(mapping)

    db.session.commit()
    return jsonify({"message": "WhatsApp configurado correctamente", "number": number})
