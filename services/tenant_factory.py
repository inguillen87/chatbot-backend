import json
import os
import secrets
from database import db
from models import TenantProfile, User, TenantConfig, TwilioNumber
from flask import current_app
import re


def normalize_whatsapp_sender(sender: str | None) -> str | None:
    if not sender:
        return None
    candidate = str(sender).strip()
    if not candidate:
        return None
    if candidate.lower().startswith("whatsapp:"):
        candidate = candidate.split(":", 1)[1].strip()
    digits = re.sub(r"\D", "", candidate)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    return f"whatsapp:+{digits}"

def load_template(template_key, config_type):
    """
    Loads a JSON template file.
    config_type: 'menu', 'contacts', 'links', 'widget'
    """
    paths_to_try = [
        os.path.join(os.getcwd(), 'data', 'templates'),
        os.path.join(os.getcwd(), '..', 'data', 'templates'), # If in subfolder
        '/app/data/templates'
    ]

    filepath = None
    for base in paths_to_try:
        p = os.path.join(base, f"{template_key}.{config_type}.json")
        if os.path.exists(p):
            filepath = p
            break

    if not filepath:
        current_app.logger.warning(f"Template not found: {template_key}.{config_type}.json")
        return {}

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        current_app.logger.error(f"Error loading template {filepath}: {e}")
        return {}

def assign_number_to_tenant(tenant: TenantProfile):
    number = (
        TwilioNumber.query
        .filter_by(status="available")
        .order_by(TwilioNumber.id.asc())
        .first()
    )
    if not number:
        current_app.logger.warning("No Twilio numbers available")
        return None

    number.status = "assigned"
    number.tenant_id = tenant.id
    normalized_sender = normalize_whatsapp_sender(number.sender_id) or number.sender_id
    tenant.whatsapp_sender_id = normalized_sender
    tenant.whatsapp_sender = normalized_sender
    db.session.add(number)
    db.session.add(tenant)
    return number

def create_tenant_from_template(
    nombre: str,
    slug: str,
    tipo: str,
    template_key: str = "municipio_default",
    plan: str = "full",
    auto_assign_whatsapp_number: bool = False,
    owner_email: str = None,
    owner_password: str = None
) -> TenantProfile:

    if TenantProfile.query.filter_by(slug=slug).first():
        raise ValueError(f"Tenant with slug '{slug}' already exists")

    # 1. Create Owner User
    if not owner_email:
        owner_email = f"admin@{slug}.chatboc.local"
    if not owner_password:
        owner_password = secrets.token_urlsafe(12)

    owner = User(
        email=owner_email,
        name=nombre,
        rol='admin',
        tipo_chat=tipo
    )
    owner.set_password(owner_password)
    db.session.add(owner)
    db.session.flush()

    # 2. Create Tenant
    widget_token = secrets.token_urlsafe(32)
    configuracion = {"widget_tokens": [widget_token]}

    tenant = TenantProfile(
        slug=slug,
        nombre=nombre,
        tipo=tipo,
        plan=plan,
        configuracion=configuracion
    )

    if tipo == 'municipio':
        tenant.municipio_id = owner.id
        # Legacy: User.municipio_id points to the User ID that represents the municipality (self)
        owner.municipio_id = owner.id
    else:
        tenant.pyme_id = owner.id
        owner.pyme_id = owner.id

    db.session.add(tenant)
    db.session.flush()

    owner.tenant_id = tenant.id

    # 3. Create Configs from Template
    configs_to_load = ['menu', 'contacts', 'links', 'widget']
    for cfg_key in configs_to_load:
        data = load_template(template_key, cfg_key)
        if data:
            tenant_config = TenantConfig(
                tenant_id=tenant.id,
                key=cfg_key,
                channel=None,
                json_value=data
            )
            db.session.add(tenant_config)

    # 4. Assign WhatsApp Number
    if auto_assign_whatsapp_number:
        assign_number_to_tenant(tenant)

    db.session.commit()

    return tenant
