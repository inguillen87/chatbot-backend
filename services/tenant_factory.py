import json
import re
import secrets
from copy import deepcopy
from pathlib import Path
from database import db
from models import TenantProfile, User, TenantConfig, TwilioNumber
from flask import current_app
from sqlalchemy.orm.attributes import flag_modified
from services.plan_access import (
    FULL_INTEGRATION_PLANS,
    normalize_plan,
    plan_allows_full_integrations,
)
from services.tenant_whatsapp_onboarding import refresh_tenant_whatsapp_onboarding
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


REQUIRED_TEMPLATE_CONFIGS = ("menu", "contacts", "links", "widget")
TEMPLATE_BUNDLE_CONTRACT = "tenant.template_bundle.v1"
PROVISIONING_READINESS_CONTRACT = "tenant.provisioning_readiness.v1"


def _template_roots() -> tuple[Path, ...]:
    project_root = Path(__file__).resolve().parents[1]
    candidates = (
        project_root / "data" / "templates",
        Path.cwd() / "data" / "templates",
        Path("/app/data/templates"),
    )
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _validated_template_key(template_key: str) -> str:
    normalized = str(template_key or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", normalized):
        raise ValueError("template_key is invalid")
    return normalized


def _load_template_bundle(
    template_key: str,
    *,
    expected_tenant_type: str | None = None,
) -> dict:
    template_key = _validated_template_key(template_key)
    for base in _template_roots():
        filepath = base / f"{template_key}.bundle.json"
        if not filepath.is_file():
            continue
        try:
            payload = json.loads(filepath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            current_app.logger.error("Invalid tenant template bundle: %s", filepath.name)
            raise ValueError(f"template bundle '{template_key}' is invalid") from exc

        if not isinstance(payload, dict):
            raise ValueError(f"template bundle '{template_key}' is invalid")

        configs = payload.get("configs")
        if (
            payload.get("contract_version") != TEMPLATE_BUNDLE_CONTRACT
            or payload.get("template_key") != template_key
            or not isinstance(configs, dict)
        ):
            raise ValueError(f"template bundle '{template_key}' is invalid")

        bundle_tenant_type = normalize_tenant_type(payload.get("tenant_type"), default="")
        if expected_tenant_type and bundle_tenant_type != expected_tenant_type:
            raise ValueError(
                f"template bundle '{template_key}' does not support tenant type '{expected_tenant_type}'"
            )

        missing = [
            key
            for key in REQUIRED_TEMPLATE_CONFIGS
            if not isinstance(configs.get(key), dict) or not configs[key]
        ]
        if missing:
            raise ValueError(
                f"template bundle '{template_key}' is incomplete: {', '.join(missing)}"
            )
        return {key: deepcopy(configs[key]) for key in REQUIRED_TEMPLATE_CONFIGS}

    raise ValueError(f"template bundle '{template_key}' was not found")


def load_template(template_key, config_type):
    """
    Loads a JSON template file.
    config_type: 'menu', 'contacts', 'links', 'widget'
    """
    template_key = _validated_template_key(template_key)
    if config_type not in REQUIRED_TEMPLATE_CONFIGS:
        raise ValueError("config_type is invalid")

    try:
        return _load_template_bundle(template_key)[config_type]
    except ValueError as bundle_error:
        if "was not found" not in str(bundle_error):
            raise

    paths_to_try = _template_roots()

    filepath = None
    for base in paths_to_try:
        p = base / f"{template_key}.{config_type}.json"
        if p.is_file():
            filepath = p
            break

    if not filepath:
        raise ValueError(f"template '{template_key}.{config_type}' was not found")

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        current_app.logger.error("Invalid tenant template: %s", filepath.name)
        raise ValueError(f"template '{template_key}.{config_type}' is invalid") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError(f"template '{template_key}.{config_type}' is empty")
    return data


def _provisioning_readiness(template_key: str, configured_keys: list[str]) -> dict:
    complete = set(configured_keys) == set(REQUIRED_TEMPLATE_CONFIGS)
    return {
        "contract_version": PROVISIONING_READINESS_CONTRACT,
        "evaluated_stage": "tenant_created",
        "requires_revalidation": True,
        "status": "configuration_required" if complete else "blocked",
        "ready": False,
        "template_key": template_key,
        "checks": {
            "base_configuration_valid": complete,
            "operator_configuration_complete": False,
            "provider_activation_performed": False,
        },
        "configured_keys": sorted(configured_keys),
        "missing": [
            "branding",
            "operator_team",
            "service_content",
            "channel_verification",
        ],
        "next_action": "complete_tenant_configuration",
    }

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
    allow_existing_owner: bool = False,
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
    if auto_assign_whatsapp_number and normalize_plan(plan) not in FULL_INTEGRATION_PLANS:
        raise ValueError("auto_assign_whatsapp_number requires a productive integration plan")

    # Resolve and validate the whole bundle before creating any owner or tenant.
    # A missing or partial package must never produce a half-configured account.
    template_configs = _load_template_bundle(
        template_key,
        expected_tenant_type=tipo,
    )

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
        if not allow_existing_owner:
            raise ValueError("owner_email already exists")
        owner.name = owner.name or nombre
        owner.rol = role_for_tenant_type(tipo)
        owner.tipo_chat = tipo
        owner.tenant_slug = slug
        owner.plan = plan
        if owner_password and reset_existing_owner_password:
            owner.set_password(owner_password)
    db.session.flush()

    # 2. Create Tenant
    configuracion = {
        "tenant_type": tipo,
        "owner_email_generated": owner_email_was_generated,
        "template": {
            "contract_version": TEMPLATE_BUNDLE_CONTRACT,
            "key": template_key,
            "configured_keys": sorted(template_configs),
        },
        "provisioning_readiness": _provisioning_readiness(
            template_key,
            list(template_configs),
        ),
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
    for cfg_key, data in template_configs.items():
        tenant_config = TenantConfig(
            tenant_id=tenant.id,
            key=cfg_key,
            channel=None,
            json_value=deepcopy(data),
        )
        db.session.add(tenant_config)

    # 4. Assign WhatsApp Number
    if auto_assign_whatsapp_number:
        assign_number_to_tenant(tenant)

    # 5. Publish a secret-free provider activation plan. Tenant creation never
    # calls provider provisioning APIs; activation remains an explicit admin
    # operation after readiness review.
    if plan_allows_full_integrations(tenant):
        try:
            onboarding_config = dict(current_app.config)
            onboarding_config["TWILIO_TENANT_AUTO_PROVISION_ENABLED"] = False
            refresh_tenant_whatsapp_onboarding(
                tenant,
                app_config=onboarding_config,
                source="tenant_factory_plan",
            )
        except Exception as exc:  # pragma: no cover - defensive guard for public signup
            current_app.logger.warning(
                "Tenant WhatsApp onboarding plan failed for %s: %s",
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
