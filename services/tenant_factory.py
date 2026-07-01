import json
import os
import re
import secrets
from database import db
from models import TenantProfile, User, TenantConfig, TwilioNumber
from flask import current_app
from sqlalchemy.orm.attributes import flag_modified
from services.plan_access import plan_allows_full_integrations
from services.tenant_whatsapp_onboarding import bootstrap_tenant_whatsapp_onboarding
from utils.roles import normalize_tenant_type, role_for_tenant_type


def _slugify(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    return raw.strip("-")


def _default_template_key(tipo: str) -> str:
    if tipo == "municipio":
        return "municipio_default"
    if tipo == "colegio":
        return "colegio_default"
    return "pyme_default"


def _owner_email_for_slug(slug: str) -> str:
    return f"admin@{slug}.chatboc.local"

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
    tenant.whatsapp_sender_id = number.sender_id
    db.session.add(number)
    db.session.add(tenant)
    return number

def create_tenant_from_template(
    nombre: str,
    slug: str,
    tipo: str,
    template_key: str = None,
    plan: str = "full",
    auto_assign_whatsapp_number: bool = False,
    owner_email: str = None,
    owner_password: str = None,
    reset_existing_owner_password: bool = True,
) -> TenantProfile:
    nombre = str(nombre or "").strip()
    slug = _slugify(slug or nombre)
    tipo = normalize_tenant_type(tipo)
    template_key = str(template_key or _default_template_key(tipo)).strip()

    if not nombre:
        raise ValueError("nombre is required")
    if not slug:
        raise ValueError("slug is required")

    if TenantProfile.query.filter_by(slug=slug).first():
        raise ValueError(f"Tenant with slug '{slug}' already exists")

    # 1. Create Owner User
    owner_email_was_generated = not bool(str(owner_email or "").strip())
    if not owner_email:
        owner_email = _owner_email_for_slug(slug)
    owner_email = str(owner_email).strip().lower()
    owner_password_was_generated = not bool(str(owner_password or "").strip())
    if not owner_password:
        owner_password = secrets.token_urlsafe(12)

    owner = User.query.filter_by(email=owner_email).first()
    if owner is None:
        owner = User(
            email=owner_email,
            name=nombre,
            rol=role_for_tenant_type(tipo),
            tipo_chat=tipo,
            token=secrets.token_urlsafe(32),
            tenant_slug=slug,
            plan=plan,
            acepto_terminos=True,
        )
        owner.set_password(owner_password)
        db.session.add(owner)
    else:
        owner.name = owner.name or nombre
        owner.rol = role_for_tenant_type(tipo)
        owner.tipo_chat = tipo
        owner.tenant_slug = slug
        owner.plan = plan
        if owner_password and (reset_existing_owner_password or not owner_password_was_generated):
            owner.set_password(owner_password)
    db.session.flush()

    # 2. Create Tenant
    configuracion = {
        "tenant_type": tipo,
        "owner_email_generated": owner_email_was_generated,
        "provisioning": {
            "status": "created",
            "channel_strategy": "tenant_scoped_sender",
        },
    }

    tenant = TenantProfile(
        slug=slug,
        nombre=nombre,
        tipo=tipo,
        plan=plan,
        is_active=True,
        configuracion=configuracion
    )

    if plan_allows_full_integrations(tenant):
        tenant.configuracion = {
            **(tenant.configuracion or {}),
            "widget_tokens": [secrets.token_urlsafe(32)],
        }

    if tipo == 'municipio':
        tenant.municipio_id = owner.id
        # Legacy: User.municipio_id points to the User ID that represents the municipality (self)
        owner.municipio_id = owner.id
        owner.pyme_id = None
    else:
        tenant.pyme_id = owner.id
        owner.pyme_id = owner.id
        owner.municipio_id = None

    db.session.add(tenant)
    db.session.flush()

    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug

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

    # 5. Prepare provider onboarding so every tenant starts with an API-first
    # WhatsApp/Twilio path. This is fail-soft: missing Meta/Twilio/Render env
    # should not block tenant creation.
    if plan_allows_full_integrations(tenant):
        try:
            bootstrap_tenant_whatsapp_onboarding(
                tenant,
                app_config=current_app.config,
                payload={"display_name": nombre},
                actor_user=owner,
                source="tenant_factory",
            )
        except Exception as exc:  # pragma: no cover - defensive guard for public signup
            current_app.logger.warning(
                "Tenant WhatsApp onboarding bootstrap failed for %s: %s",
                slug,
                exc,
                exc_info=True,
            )
    else:
        cfg = tenant.configuracion or {}
        provisioning = cfg.get("provisioning") if isinstance(cfg.get("provisioning"), dict) else {}
        provisioning.update(
            {
                "status": "plan_required",
                "blocked_reason": "plan_full_required",
                "channel_strategy": "upgrade_before_provisioning",
            }
        )
        cfg["provisioning"] = provisioning
        tenant.configuracion = cfg
        flag_modified(tenant, "configuracion")

    db.session.commit()

    return tenant
