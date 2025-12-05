from flask import Blueprint, request, jsonify, g
from utils.auth_helpers import token_requerido
from models import db, TenantProfile, User, TenantConfig, Role, UserRole, CategoriaTicket
from services.tenant_factory import create_tenant_from_template, assign_number_to_tenant

admin_tenant_bp = Blueprint('admin_tenant_bp', __name__)

# --- Tenant Management ---

@admin_tenant_bp.route('/api/admin/tenants', methods=['POST'])
def create_tenant():
    """Crea un nuevo tenant desde una plantilla."""
    data = request.json or {}
    try:
        tenant = create_tenant_from_template(
            nombre=data.get('nombre'),
            slug=data.get('slug'),
            tipo=data.get('tipo'),
            template_key=data.get('template_key', 'municipio_default'),
            plan=data.get('plan', 'full'),
            auto_assign_whatsapp_number=data.get('auto_assign_whatsapp_number', False),
            owner_email=data.get('owner_email'),
            owner_password=data.get('owner_password')
        )

        widget_token = None
        if tenant.configuracion and 'widget_tokens' in tenant.configuracion:
             widget_token = tenant.configuracion['widget_tokens'][0]

        return jsonify({
            "slug": tenant.slug,
            "widget_token": widget_token,
            "id": tenant.id
        }), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # Log error
        return jsonify({"error": str(e)}), 500

@admin_tenant_bp.route('/api/admin/tenants/<slug>/config', methods=['GET'])
@token_requerido
def get_tenant_config_bundle(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # Security Check
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
         return jsonify({'error': 'Unauthorized'}), 403

    configs = TenantConfig.query.filter_by(tenant_id=tenant.id).all()
    config_dict = {}
    for cfg in configs:
        k = cfg.key
        if cfg.channel:
            k = f"{k}:{cfg.channel}"
        config_dict[k] = cfg.json_value

    response = {
        "tenant": {
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": tenant.plan,
            "logo_url": tenant.logo_url,
            "whatsapp_sender_id": tenant.whatsapp_sender_id
        },
        **config_dict
    }
    return jsonify(response)

@admin_tenant_bp.route('/api/admin/tenants/<slug>/config', methods=['PUT'])
@token_requerido
def update_tenant_config_bundle(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # Security Check
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
         return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}

    # Update Tenant fields
    tenant_data = data.get('tenant', {})
    if 'nombre' in tenant_data: tenant.nombre = tenant_data['nombre']
    if 'logo_url' in tenant_data: tenant.logo_url = tenant_data['logo_url']

    # Update Configs
    valid_keys = ['menu', 'contacts', 'links', 'widget']

    for full_key, value in data.items():
        if full_key == 'tenant': continue

        if ':' in full_key:
            key, channel = full_key.split(':', 1)
        else:
            key, channel = full_key, None

        if key in valid_keys:
            cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key=key, channel=channel).first()
            if cfg:
                cfg.json_value = value
            else:
                cfg = TenantConfig(tenant_id=tenant.id, key=key, channel=channel, json_value=value)
                db.session.add(cfg)

    db.session.commit()
    return jsonify({"message": "Config updated"})

@admin_tenant_bp.route('/api/admin/tenants/<slug>/assign-whatsapp-number', methods=['POST'])
@token_requerido
def assign_whatsapp_number(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # Security Check
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
         return jsonify({'error': 'Unauthorized'}), 403

    number = assign_number_to_tenant(tenant)
    if not number:
        return jsonify({"error": "No numbers available"}), 409

    db.session.commit()
    return jsonify({
        "assigned": True,
        "phone_number": number.phone_number,
        "sender_id": number.sender_id
    })

# --- Employee Management ---

@admin_tenant_bp.route('/api/admin/employees', methods=['POST'])
@token_requerido
def create_employee(current_user):
    """
    Crea un nuevo empleado en el tenant actual.
    Requires X-Tenant header to resolve g.tenant_profile.
    """
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({'error': 'No tenant context'}), 400

    # Security Check
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}
    email = data.get('email')
    name = data.get('name')
    password = data.get('password')

    if not email or not password:
        return jsonify({'error': 'Missing email or password'}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'User already exists'}), 400

    user = User(
        email=email,
        name=name or email.split('@')[0],
        tenant_id=tenant.id,
        es_empleado=True,
        rol='empleado'
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    # Asignar rol de empleado por defecto
    role_emp = Role.query.filter_by(name='empleado').first()
    if not role_emp:
        role_emp = Role(name='empleado', description='Empleado estándar')
        db.session.add(role_emp)
        db.session.commit()

    user_role = UserRole(user_id=user.id, role_id=role_emp.id, tenant_id=tenant.id)
    db.session.add(user_role)
    db.session.commit()

    return jsonify({'message': 'Employee created', 'id': user.id}), 201

@admin_tenant_bp.route('/api/admin/employees/<int:user_id>/roles', methods=['POST'])
@token_requerido
def assign_role(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
         return jsonify({'error': 'No tenant context'}), 400

    # Security Check
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}
    role_name = data.get('role')

    if not role_name:
         return jsonify({'error': 'Missing role name'}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    role = Role.query.filter_by(name=role_name).first()
    if not role:
        role = Role(name=role_name)
        db.session.add(role)
        db.session.commit()

    if not UserRole.query.filter_by(user_id=user.id, role_id=role.id, tenant_id=tenant.id).first():
        user_role = UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant.id)
        db.session.add(user_role)
        db.session.commit()

    return jsonify({'message': f'Role {role_name} assigned'}), 200

@admin_tenant_bp.route('/api/admin/employees/<int:user_id>/categories', methods=['POST'])
@token_requerido
def assign_categories(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
         return jsonify({'error': 'No tenant context'}), 400

    # Security Check
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}
    category_ids = data.get('category_ids', [])

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    valid_cats = CategoriaTicket.query.filter(
        CategoriaTicket.id.in_(category_ids),
        CategoriaTicket.tenant_id == tenant.id
    ).all()

    user.categorias_ticket = valid_cats
    db.session.commit()

    return jsonify({'message': 'Categories updated', 'count': len(valid_cats)}), 200
