from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt

from app import create_app
from config import TestConfig
from database import db
from models import CategoriaTicket, TenantConfig, TenantProfile, User
from services.tenant_factory import REQUIRED_TEMPLATE_CONFIGS, _provisioning_readiness
from services.tenant_provisioning_readiness import build_tenant_provisioning_readiness


def _token(app, user: User) -> str:
    return jwt.encode(
        {
            "user_id": user.id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )


def _create_tenant(slug: str) -> tuple[TenantProfile, User]:
    owner = User(
        email=f"owner@{slug}.test",
        name=f"Owner {slug}",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug=slug,
    )
    owner.set_password("test-password")
    db.session.add(owner)
    db.session.flush()

    config = {
        "tenant_type": "municipio",
        "template": {
            "contract_version": "tenant.template_bundle.v1",
            "key": "municipio_default",
            "configured_keys": sorted(REQUIRED_TEMPLATE_CONFIGS),
        },
        "provisioning_readiness": _provisioning_readiness(
            "municipio_default",
            list(REQUIRED_TEMPLATE_CONFIGS),
        ),
        "provisioning": {
            "status": "created",
            "channel_strategy": "tenant_scoped_sender",
        },
    }
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Municipio {slug}",
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
        is_active=True,
        configuracion=config,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id

    for key in REQUIRED_TEMPLATE_CONFIGS:
        payload = {
            "contract_version": f"tenant.{key}.v1",
            "status": "configuration_required",
            "items": [],
        }
        if key == "widget":
            payload = {
                "contract_version": "tenant.widget.v1",
                "status": "configuration_required",
                "enabled": False,
            }
        db.session.add(
            TenantConfig(
                tenant_id=tenant.id,
                key=key,
                channel=None,
                json_value=payload,
            )
        )
    db.session.commit()
    return tenant, owner


def _complete_configuration(tenant: TenantProfile) -> None:
    tenant.logo_url = f"https://assets.example.test/{tenant.slug}/logo.svg"
    tenant.theme_json = {"light": {"primary": "#155EEF"}}
    config = dict(tenant.configuracion or {})
    config["widget_tokens"] = [f"widget-{tenant.slug}"]
    config["onboarding"] = {"preferred_channels": ["webchat"]}
    tenant.configuracion = config

    menu = TenantConfig.query.filter_by(
        tenant_id=tenant.id,
        key="menu",
        channel=None,
    ).one()
    menu.json_value = {
        "contract_version": "tenant.menu.v1",
        "status": "configured",
        "items": [{"id": "street-lighting", "label": "Alumbrado publico"}],
    }
    widget = TenantConfig.query.filter_by(
        tenant_id=tenant.id,
        key="widget",
        channel=None,
    ).one()
    widget.json_value = {
        "contract_version": "tenant.widget.v1",
        "status": "configured",
        "enabled": True,
    }

    category = CategoriaTicket(
        tenant_id=tenant.id,
        nombre="Alumbrado publico",
        tipo="ticket",
    )
    operator = User(
        email=f"operator@{tenant.slug}.test",
        name="Operator",
        rol="empleado",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
    )
    operator.set_password("test-password")
    db.session.add_all([category, operator])
    db.session.flush()
    operator.categorias_ticket = [category]
    db.session.commit()


def test_readiness_advances_from_tenant_created_to_current_complete_configuration():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        try:
            tenant, _ = _create_tenant("dynamic-ready")
            initial = build_tenant_provisioning_readiness(tenant)
            assert initial["evaluated_stage"] == "tenant_created"
            assert initial["ready"] is False
            assert initial["missing"] == [
                "branding",
                "operator_team",
                "service_content",
                "channel_verification",
            ]

            stored_creation_snapshot = deepcopy(tenant.configuracion["provisioning_readiness"])
            _complete_configuration(tenant)
            current = build_tenant_provisioning_readiness(tenant)

            assert current["evaluated_stage"] == "configuration_complete"
            assert current["status"] == "ready"
            assert current["ready"] is True
            assert current["production_ready"] is False
            assert current["requires_revalidation"] is False
            assert current["missing"] == []
            assert current["checks"]["provider_activation_performed"] is False
            assert current["evidence"]["channels"] == {
                "selected": ["widget"],
                "verified": ["widget"],
                "missing": [],
                "complete": True,
                "provider_activation_performed": False,
            }
            assert tenant.configuracion["provisioning_readiness"] == stored_creation_snapshot
        finally:
            db.session.remove()
            db.drop_all()


def test_readiness_is_tenant_scoped_for_team_content_and_channels():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        try:
            tenant_a, _ = _create_tenant("readiness-a")
            tenant_b, _ = _create_tenant("readiness-b")
            _complete_configuration(tenant_b)

            tenant_a.logo_url = "https://assets.example.test/readiness-a/logo.svg"
            tenant_a.theme_json = {"primary": "#155EEF"}
            db.session.commit()

            payload_a = build_tenant_provisioning_readiness(tenant_a)
            payload_b = build_tenant_provisioning_readiness(tenant_b)

            assert payload_a["ready"] is False
            assert payload_a["evaluated_stage"] == "configuration_in_progress"
            assert payload_a["evidence"]["operator_team"] == {
                "members": 0,
                "ticket_categories": 0,
                "routed_members": 0,
            }
            assert payload_a["evidence"]["service_content"] == {
                "catalog_items": 0,
                "menu_items": 0,
            }
            assert payload_a["evidence"]["channels"]["verified"] == []
            assert payload_b["ready"] is True
        finally:
            db.session.remove()
            db.drop_all()


def test_readiness_endpoint_is_current_secret_free_and_side_effect_free():
    app = create_app(TestConfig)
    client = app.test_client()
    with app.app_context():
        db.create_all()
        try:
            tenant, owner = _create_tenant("readiness-endpoint")
            _complete_configuration(tenant)
            before = deepcopy(tenant.configuracion)
            headers = {
                "Authorization": f"Bearer {_token(app, owner)}",
                "X-Tenant": tenant.slug,
            }

            with patch("services.twilio_tech_provider.poll_whatsapp_sender_status") as poll, patch(
                "services.twilio_tech_provider.provision_twilio_subaccount"
            ) as provision:
                response = client.get(
                    f"/api/admin/tenants/{tenant.slug}/provisioning-readiness",
                    headers=headers,
                )

            assert response.status_code == 200, response.get_json()
            payload = response.get_json()
            assert payload["ready"] is True
            assert payload["safety"] == {
                "server_derived": True,
                "side_effects_performed": False,
                "provider_calls_performed": False,
                "production_cutover_assessed": False,
            }
            assert response.headers["Cache-Control"] == "no-store"
            poll.assert_not_called()
            provision.assert_not_called()

            config_response = client.get(
                f"/api/admin/tenants/{tenant.slug}/config",
                headers=headers,
            )
            assert config_response.status_code == 200, config_response.get_json()
            assert config_response.get_json()["provisioning_readiness"]["evaluated_stage"] == "configuration_complete"
            db.session.refresh(tenant)
            assert tenant.configuracion == before
        finally:
            db.session.remove()
            db.drop_all()


def test_readiness_endpoint_rejects_foreign_tenant():
    app = create_app(TestConfig)
    client = app.test_client()
    with app.app_context():
        db.create_all()
        try:
            tenant_a, owner_a = _create_tenant("readiness-owner-a")
            tenant_b, _ = _create_tenant("readiness-owner-b")
            response = client.get(
                f"/api/admin/tenants/{tenant_b.slug}/provisioning-readiness",
                headers={
                    "Authorization": f"Bearer {_token(app, owner_a)}",
                    "X-Tenant": tenant_a.slug,
                },
            )
            assert response.status_code == 403
            assert response.get_json()["error"] == "Unauthorized"
        finally:
            db.session.remove()
            db.drop_all()


def test_whatsapp_readiness_requires_persisted_sender_evidence():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        try:
            tenant, _ = _create_tenant("readiness-whatsapp")
            config = dict(tenant.configuracion or {})
            config["onboarding"] = {"preferred_channels": ["whatsapp"]}
            config["whatsapp_onboarding"] = {
                "provider": "twilio_tech_provider",
                "status": "online",
            }
            tenant.configuracion = config
            db.session.commit()

            without_sender = build_tenant_provisioning_readiness(tenant)
            assert without_sender["checks"]["channel_verification_complete"] is False
            assert without_sender["checks"]["provider_activation_performed"] is False

            tenant.whatsapp_sender_id = "whatsapp:+15550009999"
            db.session.commit()
            with_sender = build_tenant_provisioning_readiness(tenant)
            assert with_sender["checks"]["channel_verification_complete"] is True
            assert with_sender["checks"]["provider_activation_performed"] is True
        finally:
            db.session.remove()
            db.drop_all()


def test_config_update_returns_recalculated_readiness_in_same_response():
    app = create_app(TestConfig)
    client = app.test_client()
    with app.app_context():
        db.create_all()
        try:
            tenant, owner = _create_tenant("readiness-config-update")
            _complete_configuration(tenant)
            tenant.logo_url = None
            tenant.theme_json = None
            menu = TenantConfig.query.filter_by(
                tenant_id=tenant.id,
                key="menu",
                channel=None,
            ).one()
            menu.json_value = {
                "contract_version": "tenant.menu.v1",
                "status": "configuration_required",
                "items": [],
            }
            widget = TenantConfig.query.filter_by(
                tenant_id=tenant.id,
                key="widget",
                channel=None,
            ).one()
            widget.json_value = {
                "contract_version": "tenant.widget.v1",
                "status": "configuration_required",
                "enabled": False,
            }
            db.session.commit()
            assert build_tenant_provisioning_readiness(tenant)["ready"] is False

            response = client.put(
                f"/api/admin/tenants/{tenant.slug}/config",
                headers={
                    "Authorization": f"Bearer {_token(app, owner)}",
                    "X-Tenant": tenant.slug,
                },
                json={
                    "tenant": {
                        "logo_url": "https://assets.example.test/configured/logo.svg",
                        "theme_json": {"primary": "#155EEF"},
                    },
                    "configs": {
                        "menu": {
                            "default": {
                                "contract_version": "tenant.menu.v1",
                                "status": "configured",
                                "items": [{"id": "claims", "label": "Reclamos"}],
                            }
                        },
                        "widget": {
                            "default": {
                                "contract_version": "tenant.widget.v1",
                                "status": "configured",
                                "enabled": True,
                            }
                        },
                    },
                },
            )

            assert response.status_code == 200, response.get_json()
            payload = response.get_json()
            assert payload["provisioning_readiness"]["ready"] is True
            assert payload["provisioning_readiness"]["evaluated_stage"] == "configuration_complete"
            assert payload["readiness_endpoint"].endswith("/provisioning-readiness")
        finally:
            db.session.remove()
            db.drop_all()
