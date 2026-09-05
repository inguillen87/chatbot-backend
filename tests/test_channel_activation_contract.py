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
    assert by_id["institutional_branding"]["status"] == "action_required"
    assert by_id["accessibility"]["status"] == "action_required"
    assert by_id["territorial_intelligence"]["status"] == "action_required"
    assert by_id["whatsapp"]["status"] == "locked"
    assert by_id["widget"]["status"] == "locked"
    assert by_id["templates"]["status"] == "locked"
    assert by_id["payments_checkout"]["status"] == "locked"
    assert by_id["payments_checkout"]["required_plan"] == "full"
    assert by_id["team_routing"]["status"] == "action_required"
    assert by_id["team_routing"]["reason_code"] == "team_required"
    assert payload["preferred_channels"] == ["whatsapp", "webchat"]
    journey = payload["implementation_journey"]
    assert journey["contract_version"] == "tenant.implementation_journey.v1"
    assert journey["summary"]["current_stage_id"] == "institutional_identity"
    assert journey["summary"]["next_action"]["id"] == "open_branding"
    assert [item["id"] for item in journey["stages"]] == [
        "institutional_identity",
        "channels",
        "knowledge",
        "team",
        "validation_release",
    ]
    knowledge = next(item for item in payload["channels"] if item["id"] == "knowledge_content")
    assert knowledge["status"] == "action_required"
    assert knowledge["reason_code"] == "knowledge_content_required"

    response = client.get(
        f"/api/v2/tenants/{tenant.slug}/activation/channels",
        headers=_auth_headers(client.application, owner, tenant),
    )
    assert response.status_code == 200
    route_payload = response.get_json()
    assert route_payload["contract_version"] == "tenant.channel_activation.v1"
    assert route_payload["tenant"]["slug"] == tenant.slug
    assert "secret-widget-token" not in str(route_payload)


def test_channel_activation_contract_requires_explicit_government_setup_evidence(client):
    owner, tenant = _create_owner_and_tenant(
        slug="government-implementation",
        plan="full",
        configuracion={
            "accessibility": {
                "enabled": True,
                "features": ["keyboard_navigation", "plain_language", "screen_reader"],
                "human_handoff": True,
            }
        },
    )
    owner_id = owner.id
    tenant.logo_url = "https://assets.example.test/tenant-logo.svg"
    tenant.tema = {"primaryColor": "#075985", "secondaryColor": "#e0f2fe"}
    tenant.dominio = "gobierno.example.test"
    tenant.jurisdiction_status = "verified"
    tenant.jurisdiction_ref = "official:government-implementation:v1"
    tenant.jurisdiction_evidence_ref = "evidence:government-implementation:v1"
    tenant.jurisdiction_verified_by_user_id = owner_id
    tenant.jurisdiction_verified_at = datetime.now().astimezone()
    db.session.commit()

    payload = build_channel_activation_payload(tenant)

    by_id = {item["id"]: item for item in payload["channels"]}
    assert by_id["institutional_branding"]["status"] == "ready"
    assert by_id["accessibility"]["status"] == "ready"
    assert by_id["territorial_intelligence"]["status"] == "ready"
    assert "tenant-logo.svg" not in str(by_id["institutional_branding"])
    assert "official:government-implementation:v1" not in str(by_id["territorial_intelligence"])
    assert "evidence:government-implementation:v1" not in str(by_id["territorial_intelligence"])


def test_channel_activation_contract_marks_ready_full_tenant_channels(client):
    owner, tenant = _create_owner_and_tenant(
        slug="full-activation",
        plan="full",
        configuracion={
            "widget_tokens": ["secret-widget-token"],
            "mercadopago_access_token": "APP_USR-secret-token",
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
    operator = User(
        name="Operador Obras",
        email="operador-obras@chatboc.test",
        rol="empleado",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
    )
    operator.set_password("secret123")
    db.session.add(operator)
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
    assert by_id["payments_checkout"]["status"] == "ready"
    assert by_id["team_routing"]["status"] == "ready"
    assert by_id["live_chat"]["status"] == "ready"
    assert payload["counts"]["approved_templates"] == 1
    assert payload["counts"]["catalog_items"] == 1
    assert payload["counts"]["team_members"] == 1
    assert "APP_USR-secret-token" not in str(payload)


def test_channel_activation_contract_keeps_registered_sender_pending_until_twilio_approves(client):
    _, tenant = _create_owner_and_tenant(
        slug="pending-sender-activation",
        plan="full",
        configuracion={
            "whatsapp_onboarding": {
                "provider": "twilio_tech_provider",
                "status": "sender_registered",
                "connect": {
                    "register_sender_endpoint": "/api/v2/tenants/pending-sender-activation/whatsapp/tech-provider/register-sender",
                },
            },
            "twilio_tech_provider": {
                "sender_sid": "XEpending",
                "sender_status": "PENDING",
            },
        },
    )

    payload = build_channel_activation_payload(tenant)

    whatsapp = {item["id"]: item for item in payload["channels"]}["whatsapp"]
    assert whatsapp["status"] == "pending"
    assert whatsapp["ready"] is False
    assert whatsapp["reason_code"] == "sender_not_online"
    assert "sender:pending" in whatsapp["evidence"]
    assert whatsapp["actions"][0]["id"] == "open_sender_status"
    assert whatsapp["actions"][0]["href"] == "/t/pending-sender-activation/integracion?channel=whatsapp&action=sender-status"
    assert whatsapp["actions"][1]["id"] == "poll_sender_status"
    assert whatsapp["actions"][1]["kind"] == "api"


def test_channel_activation_contract_register_sender_action_when_meta_signup_completed(client):
    _, tenant = _create_owner_and_tenant(
        slug="register-sender-activation",
        plan="full",
        configuracion={
            "whatsapp_onboarding": {
                "provider": "twilio_tech_provider",
                "status": "pending_sender_registration",
                "connect": {
                    "register_sender_endpoint": "/api/v2/tenants/register-sender-activation/whatsapp/tech-provider/register-sender",
                },
            },
            "twilio_tech_provider": {
                "waba_id": "123456",
                "phone_number_id": "987654",
            },
        },
    )

    payload = build_channel_activation_payload(tenant)

    whatsapp = {item["id"]: item for item in payload["channels"]}["whatsapp"]
    assert whatsapp["status"] == "action_required"
    assert whatsapp["ready"] is False
    assert whatsapp["reason_code"] == "register_sender"
    assert whatsapp["actions"][0]["id"] == "open_register_sender"
    assert whatsapp["actions"][0]["href"] == "/t/register-sender-activation/integracion?channel=whatsapp&action=register-sender"
    assert whatsapp["actions"][1]["href"].endswith("/whatsapp/tech-provider/register-sender")


def test_channel_activation_contract_marks_provider_online_sender_ready(client):
    _, tenant = _create_owner_and_tenant(
        slug="online-sender-activation",
        plan="full",
        configuracion={
            "whatsapp_onboarding": {"provider": "twilio_tech_provider", "status": "sender_registered"},
            "twilio_tech_provider": {"sender_sid": "XEonline", "sender_status": "ONLINE"},
        },
    )

    payload = build_channel_activation_payload(tenant)

    whatsapp = {item["id"]: item for item in payload["channels"]}["whatsapp"]
    assert whatsapp["status"] == "ready"
    assert whatsapp["ready"] is True
    assert whatsapp["reason_code"] is None
    assert whatsapp["actions"][0]["id"] == "open_whatsapp_setup"


def test_channel_activation_contract_surfaces_public_intake_security_readiness(client, monkeypatch):
    monkeypatch.delenv("VITE_CLOUDFLARE_TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_CLOUDFLARE_TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", raising=False)
    _, tenant = _create_owner_and_tenant(slug="turnstile-activation", plan="full")

    payload = build_channel_activation_payload(tenant)

    security_channel = {item["id"]: item for item in payload["channels"]}["public_intake_security"]
    assert security_channel["status"] == "action_required"
    assert security_channel["reason_code"] == "turnstile_config_missing"
    assert "VITE_CLOUDFLARE_TURNSTILE_SITE_KEY" in security_channel["progress_hint"]
    assert "CLOUDFLARE_TURNSTILE_SECRET_KEY" in security_channel["progress_hint"]

    monkeypatch.setenv("VITE_CLOUDFLARE_TURNSTILE_SITE_KEY", "site-key-visible")
    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_SECRET_KEY", "secret-never-exposed")
    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "true")

    ready_payload = build_channel_activation_payload(tenant)
    ready_security = {item["id"]: item for item in ready_payload["channels"]}["public_intake_security"]
    assert ready_security["status"] == "ready"
    assert "enforcement activo" in ready_security["evidence"]
    assert "site-key-visible" not in str(ready_payload)
    assert "secret-never-exposed" not in str(ready_payload)


def test_channel_activation_contract_surfaces_clerk_identity_readiness(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("VITE_CLERK_PUBLISHABLE_KEY", "pk_live_visible_key")
    monkeypatch.setenv("CLERK_ISSUER", "https://chatboc.clerk.accounts.dev")
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    monkeypatch.delenv("CLERK_WEBHOOK_SECRET", raising=False)
    _, tenant = _create_owner_and_tenant(
        slug="clerk-ready-tenant",
        plan="full",
        configuracion={
            "auth": {"provider": "clerk"},
            "onboarding": {"source": "clerk", "preferred_channels": ["whatsapp", "webchat"]},
        },
    )

    payload = build_channel_activation_payload(tenant)

    by_id = {item["id"]: item for item in payload["channels"]}
    identity = by_id["identity_auth"]
    assert identity["status"] == "pending"
    assert identity["reason_code"] == "clerk_webhook_recommended"
    assert "CLERK_WEBHOOK_SIGNING_SECRET" in identity["progress_hint"]
    assert "pk_live_visible_key" not in str(payload)

    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_secret_value")
    ready_payload = build_channel_activation_payload(tenant)

    ready_identity = {item["id"]: item for item in ready_payload["channels"]}["identity_auth"]
    assert ready_identity["status"] == "ready"
    assert ready_identity["reason_code"] is None
    assert "whsec_secret_value" not in str(ready_payload)


def test_channel_activation_contract_blocks_clerk_development_keys_for_production(client, monkeypatch):
    monkeypatch.setenv("CLERK_ENABLED", "true")
    monkeypatch.setenv("VITE_CLERK_PUBLISHABLE_KEY", "pk_test_visible_key")
    monkeypatch.setenv("CLERK_ISSUER", "https://skilled-walleye-28.clerk.accounts.dev")
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", "whsec_secret_value")
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")
    _, tenant = _create_owner_and_tenant(
        slug="clerk-dev-tenant",
        plan="full",
        configuracion={
            "auth": {"provider": "clerk"},
            "onboarding": {"source": "clerk", "preferred_channels": ["whatsapp", "webchat"]},
        },
    )

    payload = build_channel_activation_payload(tenant)

    identity = {item["id"]: item for item in payload["channels"]}["identity_auth"]
    assert identity["status"] == "blocked"
    assert identity["reason_code"] == "clerk_production_keys_missing"
    assert "development/test" in identity["progress_hint"]
    assert "entorno Clerk:development" in identity["evidence"]
    assert "pk_test_visible_key" not in str(payload)
    assert "whsec_secret_value" not in str(payload)
