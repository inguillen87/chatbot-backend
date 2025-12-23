from flask import Blueprint, request, jsonify, g, current_app
from utils.auth_helpers import token_requerido
from middleware.tenant_context import require_tenant
from models import db, TenantProfile, User, TenantConfig, Role, UserRole, CategoriaTicket, IntegrationAccount
from services.tenant_factory import create_tenant_from_template, assign_number_to_tenant
from services.tenant_resolver import apply_tenant_alias

admin_tenant_bp = Blueprint('admin_tenant_bp', __name__)


def _is_authorized_for_tenant(current_user: User, tenant: TenantProfile) -> bool:
    """Return True if ``current_user`` can manage the given tenant.

    Besides the explicit ``tenant_id`` match used in most flows, admins may be
    linked as the owning municipality/pyme user without ``tenant_id`` filled in.
    Allow those owners (and platform admins) to administer employees to avoid
    false 403 responses when legacy data lacks ``tenant_id``.
    """

    if not current_user or not tenant:
        return False

    if current_user.rol in ("platform_admin", "super_admin"):
        return True

    if current_user.tenant_id and current_user.tenant_id == tenant.id:
        return True

    if getattr(current_user, "tenant_slug", None) and tenant.slug and current_user.tenant_slug.lower() == tenant.slug.lower():
        return True

    # Unconditional Owner Check (Strongest)
    # Handle int vs str comparison just in case
    if tenant.municipio_id and str(current_user.id) == str(tenant.municipio_id):
        return True
    if tenant.pyme_id and str(current_user.id) == str(tenant.pyme_id):
        return True

    # 3) fallback LEGACY municipio
    if getattr(current_user, "tipo_chat", None) == "municipio":
        # a) usuario tiene municipio_id apuntando al tenant (ID)
        if getattr(current_user, "municipio_id", None) and str(current_user.municipio_id) == str(tenant.id):
            return True
        # c) usuario es empleado del dueño (mismo municipio_id)
        if getattr(tenant, "municipio_id", None) and getattr(current_user, "municipio_id", None):
            if str(current_user.municipio_id) == str(tenant.municipio_id):
                return True

    # 4) fallback LEGACY pyme/empresa
    tipo_chat = getattr(current_user, "tipo_chat", "")
    if tipo_chat in ("pyme", "empresa"):
        if getattr(current_user, "empresa_id", None) and current_user.empresa_id == tenant.id:
            return True
        if getattr(current_user, "pyme_id", None) and current_user.pyme_id == tenant.id:
             return True
        # Check affiliation (employee/admin of same owner)
        if getattr(tenant, "pyme_id", None):
            owner_id = tenant.pyme_id
            if getattr(current_user, "empresa_id", None) == owner_id:
                return True
            if getattr(current_user, "pyme_id", None) == owner_id:
                return True

    return False

# --- Tenant Management ---

@admin_tenant_bp.route('/api/admin/tenants', methods=['POST'])
def create_tenant():
    """Crea un nuevo tenant desde una plantilla. Actúa como registro público."""
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
@require_tenant
def get_tenant_config_bundle(current_user, slug):
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    configs = TenantConfig.query.filter_by(tenant_id=tenant.id).all()
    # Nested structure: key -> channel -> value
    config_dict = {}
    for cfg in configs:
        k = cfg.key
        c = cfg.channel or 'default'
        if k not in config_dict:
            config_dict[k] = {}
        config_dict[k][c] = cfg.json_value

    response = {
        "tenant": {
            "slug": tenant.slug,
            "nombre": tenant.nombre,
            "tipo": tenant.tipo,
            "plan": tenant.plan,
            "logo_url": tenant.logo_url,
            "whatsapp_sender_id": tenant.whatsapp_sender_id,
            "whatsapp_sender": tenant.whatsapp_sender
        },
        "configs": config_dict
    }
    return jsonify(response)

@admin_tenant_bp.route('/api/admin/tenants/<slug>/config', methods=['PUT'])
@token_requerido
@require_tenant
def update_tenant_config_bundle(current_user, slug):
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check
    if current_user.tenant_id != tenant.id and current_user.rol != 'platform_admin':
         return jsonify({'error': 'Unauthorized'}), 403

    data = request.json or {}

    # Update Tenant fields
    tenant_data = data.get('tenant', {})
    if 'nombre' in tenant_data: tenant.nombre = tenant_data['nombre']
    if 'logo_url' in tenant_data: tenant.logo_url = tenant_data['logo_url']

    # Update Configs
    # Expecting: "configs": { "menu": { "default": {...}, "widget": {...} } }
    # Or simplified: "menu": { ... } (updates default)
    # Let's support both for backward compat with my prev implementation if used.

    valid_keys = ['menu', 'contacts', 'links', 'widget']

    configs_in = data.get('configs', {})
    # Also merge top level keys if they match valid_keys (legacy support)
    for k in valid_keys:
        if k in data:
            if k not in configs_in:
                configs_in[k] = {}
            configs_in[k]['default'] = data[k] # Assume default channel if top level

    for key, channels_map in configs_in.items():
        if key not in valid_keys: continue

        if not isinstance(channels_map, dict):
             # Maybe raw json? Treat as default
             channels_map = {'default': channels_map}

        for channel, value in channels_map.items():
            chan_val = None if channel == 'default' else channel

            cfg = TenantConfig.query.filter_by(tenant_id=tenant.id, key=key, channel=chan_val).first()
            if cfg:
                cfg.json_value = value
            else:
                cfg = TenantConfig(tenant_id=tenant.id, key=key, channel=chan_val, json_value=value)
                db.session.add(cfg)

    db.session.commit()
    return jsonify({"message": "Config updated"})

@admin_tenant_bp.route('/api/admin/tenants/<slug>/assign-whatsapp-number', methods=['POST'])
@token_requerido
@require_tenant
def assign_whatsapp_number(current_user, slug):
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check
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
@require_tenant
def create_employee(current_user):
    """
    Crea un nuevo empleado en el tenant actual.
    Requires X-Tenant header to resolve g.tenant_profile.
    """
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
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

    # Asignar roles
    roles = data.get('roles', ['empleado'])
    for role_name in roles:
        role = Role.query.filter_by(name=role_name).first()
        if not role:
            # Auto-create basic roles if missing
            role = Role(name=role_name, description=f'Rol {role_name}')
            db.session.add(role)
            db.session.commit()

        if not UserRole.query.filter_by(user_id=user.id, role_id=role.id, tenant_id=tenant.id).first():
            ur = UserRole(user_id=user.id, role_id=role.id, tenant_id=tenant.id)
            db.session.add(ur)

    # Asignar categorías
    category_ids = data.get('categories', [])
    if category_ids:
        valid_cats = CategoriaTicket.query.filter(
            CategoriaTicket.id.in_(category_ids),
            CategoriaTicket.tenant_id == tenant.id
        ).all()
        user.categorias_ticket = valid_cats

    db.session.commit()

    return jsonify({'message': 'Employee created', 'id': user.id}), 201

@admin_tenant_bp.route('/api/admin/employees/<int:user_id>/roles', methods=['POST'])
@token_requerido
@require_tenant
def assign_role(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
         return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
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
@require_tenant
def assign_categories(current_user, user_id):
    tenant = g.tenant_profile
    if not tenant:
         return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
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
@admin_tenant_bp.route('/api/admin/employees', methods=['GET'])
@token_requerido
@require_tenant
def list_current_tenant_employees(current_user):
    """
    List employees for the current tenant context.
    """
    tenant = g.tenant_profile
    if not tenant:
        return jsonify({'error': 'No tenant context'}), 400

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
        return jsonify({'error': 'Unauthorized'}), 403

    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()

    results = []
    for emp in employees:
        # Load roles using the dynamic relationship or query
        user_roles = UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).all()
        role_names = [ur.role.name for ur in user_roles if ur.role]

        results.append({
            "id": emp.id,
            "name": emp.name,
            "email": emp.email,
            "roles": role_names,
            "created_at": emp.fecha_creacion.isoformat() if emp.fecha_creacion else None
        })

    return jsonify(results)


# --- Integration Management ---

@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations', methods=['GET'])
@token_requerido
@require_tenant
def list_integrations(current_user, slug):
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    integrations = IntegrationAccount.query.filter_by(tenant_id=tenant.id).all()

    # Mock status for known types if missing
    known_types = ["MercadoLibre", "TiendaNube", "WhatsApp"]
    result = {}

    # Fill from DB
    for integ in integrations:
        result[integ.type] = {
            "connected": integ.status == 'active',
            "lastSync": integ.last_sync_at.isoformat() if integ.last_sync_at else None,
            "account": integ.metadata_payload.get('account_name') if integ.metadata_payload else None
        }

    # Fill missing
    for t in known_types:
        if t not in result:
            result[t] = {"connected": False}

    return jsonify(result)


@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/<string:integration_type>/connect', methods=['GET'])
@token_requerido
@require_tenant
def connect_integration(current_user, slug, integration_type):
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    # Stub for OAuth redirect generation
    if integration_type.lower() == 'tiendanube':
        client_id = current_app.config.get("TIENDANUBE_CLIENT_ID") or "12345"
        redirect_uri = f"https://chatboc.ar/api/admin/tenants/{slug}/integrations/tiendanube/callback"
        auth_url = f"https://www.tiendanube.com/apps/authorize?client_id={client_id}&redirect_uri={redirect_uri}"
        return jsonify({"redirect_url": auth_url})

    elif integration_type.lower() == 'mercadolibre':
        client_id = current_app.config.get("MERCADOLIBRE_APP_ID") or "APP_USR_123"
        redirect_uri = f"https://chatboc.ar/api/admin/tenants/{slug}/integrations/mercadolibre/callback"
        auth_url = f"https://auth.mercadolibre.com.ar/authorization?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
        return jsonify({"redirect_url": auth_url})

    return jsonify({"error": "Integration type not supported"}), 400

@admin_tenant_bp.route('/api/admin/tenants/<slug>/employees', methods=['GET'])
@token_requerido
@require_tenant
def list_employees_by_slug(current_user, slug):
    """
    List employees for a specific tenant slug (supports admin dashboard deep linking).
    """
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    # IDOR Check
    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    employees = User.query.filter_by(tenant_id=tenant.id, es_empleado=True).all()

    results = []
    for emp in employees:
        user_roles = UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).all()
        role_names = [ur.role.name for ur in user_roles if ur.role]

        results.append({
            "id": emp.id,
            "name": emp.name,
            "email": emp.email,
            "roles": role_names,
            "created_at": emp.fecha_creacion.isoformat() if emp.fecha_creacion else None
        })

    return jsonify(results)

@admin_tenant_bp.route('/api/admin/tenants/<slug>/integrations/<string:integration_type>/sync', methods=['POST'])
@token_requerido
@require_tenant
def sync_integration(current_user, slug, integration_type):
    slug = apply_tenant_alias(slug) or slug
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404

    if not _is_authorized_for_tenant(current_user, tenant):
         return jsonify({'error': 'Unauthorized'}), 403

    if integration_type.lower() == 'mercadolibre':
        from services.integrations.mercadolibre import MercadoLibreService
        service = MercadoLibreService(tenant)
        result = service.sync_catalog()
        return jsonify(result)

    elif integration_type.lower() == 'tiendanube':
        from services.integrations.tiendanube import TiendaNubeService
        service = TiendaNubeService(tenant)
        result = service.import_products()
        return jsonify(result)

    return jsonify({"error": "Integration not supported"}), 400
