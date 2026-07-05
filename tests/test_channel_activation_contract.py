from datetime import datetime, timedelta

import jwt

from database import db
from models import CatalogoItem, MessageTemplateRegistry, TenantProfile, User
from services.channel_activation import build_channel_activation_payload


def _auth_headers(app, user: User, tenant: TenantProfile) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tenant_slug": tenant.slug,
            "exp": datetime.utcnow() + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant.slug}


def _create_owner_and_tenant(*, slug: str, plan: str, configuracion: dict | None = None) -> tuple[User, TenantProfile]:
    owner = User(name=f"Owner {slug}", email=f"{slug}@chatboc.test", rol="admin", tenant_slug=slug)
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="municipio",
        plan=plan,
        municipio_id=owner.id,
        configuracion=configuracion or {},
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.add(owner)
    db.session.commit()
    return owner, tenant


def test_channel_activation_contract_blocks_productive_channels_without_secrets(client):
    owner, tenant = _create_owner_and_tenant(
        slug="free-activation",
        plan="free",
        configuracion={
            "widget_tokens": ["secret-widget-token"],
            "provisioning": {"status": "plan_required", "blocked_reason": "plan_full_required"},
            "onboarding": {"preferred_channels": ["whatsapp", "webchat"]},
        },
    )

    payload = build_channel_activation_payload(tenant)

    assert payload["contract_version"] == "tenant.channel_activation.v1"
    assert payload["tenant"]["slug"] == "free-activation"
    assert payload["security"]["secret_free"] is True
    assert "secret-widget-token" not in str(payload)
    assert payload["integration_access"]["enabled"] is False
    by_id = {item["id"]: item for item in payload["channels"]}
    assert by_id["crm"]["status"] == "ready"
    assert by_id["whatsapp"]["status"] == "locked"
    assert by_id["widget"]["status"] == "locked"
    assert by_id["templates"]["status"] == "locked"
    assert payload["preferred_channels"] == ["whatsapp", "webchat"]

    response = client.get(
        f"/api/v2/tenants/{tenant.slug}/activation/channels",
        headers=_auth_headers(client.application, owner, tenant),
    )
    assert response.status_code == 200
    route_payload = response.get_json()
    assert route_payload["contract_version"] == "tenant.channel_activation.v1"
    assert route_payload["tenant"]["slug"] == tenant.slug
    assert "secret-widget-token" not in str(route_payload)


def test_channel_activation_contract_marks_ready_full_tenant_channels(client):
    owner, tenant = _create_owner_and_tenant(
        slug="full-activation",
        plan="full",
        configuracion={
            "widget_tokens": ["secret-widget-token"],
            "whatsapp_onboarding": {"provider": "twilio_tech_provider", "status": "online"},
            "live_chat_schedule": {
                "enabled": True,
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start_time": "09:00",
                "end_time": "13:00",
                "timezone": "America/Argentina/Buenos_Aires",
            },
        },
    )
    tenant.whatsapp_sender_id = "whatsapp:+100000"
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Lampara LED",
            categoria="luminaria",
        )
    )
    db.session.add(
        MessageTemplateRegistry(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            name="gobiernos_reclamo_sla",
            language="es",
            status="approved",
            content_sid="HXtemplate",
        )
    )
    db.session.commit()

    payload = build_channel_activation_payload(tenant)

    assert payload["integration_access"]["enabled"] is True
    by_id = {item["id"]: item for item in payload["channels"]}
    assert by_id["whatsapp"]["status"] == "ready"
    assert by_id["widget"]["status"] == "ready"
    assert by_id["templates"]["status"] == "ready"
    assert by_id["catalog_marketplace"]["status"] == "ready"
    assert by_id["live_chat"]["status"] == "ready"
    assert payload["counts"]["approved_templates"] == 1
    assert payload["counts"]["catalog_items"] == 1
