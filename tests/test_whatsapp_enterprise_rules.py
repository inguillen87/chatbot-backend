from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jwt

from app import db
from models import MessageTemplateRegistry, TenantProfile, User
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed():
    admin = User(email="wa-rules-admin@test.com", name="Admin", rol="admin", tipo_chat="pyme")
    admin.set_password("pass")
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(slug="wa-rules-tenant", nombre="WA Rules", tipo="pyme", pyme_id=admin.id)
    db.session.add(tenant)
    db.session.commit()
    return admin, tenant


def test_whatsapp_rules_update_and_notification_enforcement(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    put_resp = client.put(
        "/api/admin/whatsapp/rules",
        headers=headers,
        json={"enforce_template_outside_24h": True, "max_outbound_per_hour": 99, "blocked_keywords": ["prohibido"]},
    )
    assert put_resp.status_code == 200

    get_resp = client.get("/api/admin/whatsapp/rules", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.get_json()["enforce_template_outside_24h"] is True

    create = client.post(
        "/api/admin/notifications",
        headers=headers,
        json={
            "channel": "whatsapp",
            "recipient": "+5491112345678",
            "body": "mensaje normal",
            "idempotency_key": "wa-rule-1",
            "metadata": {"within_24h_window": False, "is_template": False},
        },
    )
    assert create.status_code == 201

    dispatch = client.post("/api/workers/notifications/dispatch", headers=headers)
    assert dispatch.status_code == 200
    data = dispatch.get_json()
    assert data["failed"] >= 1


def test_whatsapp_rules_uses_contact_state_24h_window(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    svc = WhatsAppEnterpriseRulesService(tenant.id)
    svc.register_inbound_activity(recipient="+5491112345678")
    db.session.commit()

    put_resp = client.put(
        "/api/admin/whatsapp/rules",
        headers=headers,
        json={"enforce_template_outside_24h": True},
    )
    assert put_resp.status_code == 200

    create = client.post(
        "/api/admin/notifications",
        headers=headers,
        json={
            "channel": "whatsapp",
            "recipient": "+5491112345678",
            "body": "mensaje freeform permitido por inbound reciente",
            "idempotency_key": "wa-rule-state-1",
            "metadata": {"is_template": False},
        },
    )
    assert create.status_code == 201

    dispatch = client.post("/api/workers/notifications/dispatch", headers=headers)
    assert dispatch.status_code == 200
    payload = dispatch.get_json()
    assert payload["sent"] >= 1


def test_whatsapp_template_catalog_and_policy_test_endpoint(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    created = client.post(
        "/api/admin/templates",
        headers=headers,
        json={
            "key": "wa_promo_1",
            "body_template": "Hola ${name}",
            "health_status": "healthy",
        },
    )
    assert created.status_code == 201
    template_id = created.get_json()["id"]

    listed = client.get("/api/admin/templates", headers=headers)
    assert listed.status_code == 200
    data = listed.get_json()
    assert any(t["id"] == template_id and t["health_status"] == "healthy" for t in data)

    patched = client.patch(
        f"/api/admin/templates/{template_id}",
        headers=headers,
        json={"health_status": "degraded", "is_active": False},
    )
    assert patched.status_code == 200
    assert patched.get_json()["updated"] is True

    policy = client.post(
        "/api/notifications/whatsapp/test",
        headers=headers,
        json={
            "recipient": "+5491112345678",
            "body": "hola",
            "metadata": {"within_24h_window": False, "is_template": False},
        },
    )
    assert policy.status_code == 200
    policy_data = policy.get_json()
    assert policy_data["allowed"] is False
    assert policy_data["reason"] == "template_required"


def test_twilio_content_sync_dry_run_returns_creation_payload(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    response = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "order_checkout", "dry_run": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True
    assert payload["ready_to_create"] is True
    assert payload["create_request"]["friendly_name"]
    assert payload["create_request"]["types"]["twilio/text"]["body"]
    assert payload["approval_request"]["category"] == "UTILITY"
    assert payload["content_family"] == "cta_webview"
    assert payload["action_capabilities"]["webview_ready"] is True
    assert payload["meta_business"]["recommended_surface"] in {"whatsapp_flow", "commerce_catalog"}
    assert payload["meta_business"]["cta_webview_candidate"] is True
    assert payload["meta_business"]["outside_24h_requires_approval"] is True
    assert payload["create_request"]["types"]["twilio/call-to-action"]["actions"][0]["type"] == "URL"
    cta_url = payload["create_request"]["types"]["twilio/call-to-action"]["actions"][0]["url"]
    assert cta_url.startswith("https://www.chatboc.ar/")
    assert cta_url != "{{3}}"
    assert cta_url.endswith("/{{3}}")
    assert payload["create_request"]["variables"]["3"] == f"t/{tenant.slug}/checkout"
    assert payload["quality_gate"]["webview_ready"] is True
    assert payload["quality_gate"]["requires_signed_url_for_cta"] is True
    assert payload["existing_registry"]["configured"] is False

    finance_response = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "finance_secure_payment", "dry_run": True},
    )
    assert finance_response.status_code == 200
    finance_payload = finance_response.get_json()
    finance_cta = finance_payload["create_request"]["types"]["twilio/call-to-action"]["actions"][0]
    assert finance_cta["url"] == "https://www.chatboc.ar/{{3}}"
    assert (
        finance_payload["create_request"]["variables"]["3"]
        == f"finanzas/{tenant.slug}/operacion/OP-1001?session=session-demo-123456"
    )
    assert finance_payload["content_family"] == "cta_webview"


def test_twilio_content_sync_creates_content_and_registry_row(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    app.config["TWILIO_ACCOUNT_SID"] = "ACtest"
    app.config["TWILIO_AUTH_TOKEN"] = "secret"

    class _FakeApprovalRequests:
        def create(self, **kwargs):
            assert kwargs["category"] == "UTILITY"
            return SimpleNamespace(status="PENDING")

    class _FakeContents:
        def __call__(self, content_sid):
            assert content_sid == "HXcreatedtemplate"
            return SimpleNamespace(approval_requests=_FakeApprovalRequests())

        def create(self, **kwargs):
            assert kwargs["friendly_name"]
            assert "twilio/text" in kwargs["types"]
            return SimpleNamespace(sid="HXcreatedtemplate")

    fake_client = SimpleNamespace(content=SimpleNamespace(v1=SimpleNamespace(contents=_FakeContents())))

    with patch("routes.whatsapp_rules.Client", return_value=fake_client) as client_factory:
        response = client.post(
            "/api/admin/templates/twilio-content/sync",
            headers=headers,
            json={"template_id": "order_checkout", "dry_run": False},
        )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["content_sid"] == "HXcreatedtemplate"
    assert payload["registry"]["status"] == "pending_approval"
    client_factory.assert_called_once_with("ACtest", "secret")

    row = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        content_sid="HXcreatedtemplate",
    ).one()
    assert row.status == "pending_approval"
    assert row.metadata_json["template_id"] == "order_checkout"


def test_twilio_content_sync_does_not_duplicate_existing_content_sid(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    dry_run = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "order_checkout", "dry_run": True},
    ).get_json()
    friendly_name = dry_run["create_request"]["friendly_name"]
    language = dry_run["create_request"]["language"]
    row = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=friendly_name,
        language=language,
        category="UTILITY",
        status="approved",
        content_sid="HXexistingtemplate",
    )
    db.session.add(row)
    db.session.commit()

    with patch("routes.whatsapp_rules.Client") as client_factory:
        response = client.post(
            "/api/admin/templates/twilio-content/sync",
            headers=headers,
            json={"template_id": "order_checkout", "dry_run": False},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["created"] is False
    assert payload["reason"] == "already_registered"
    assert payload["registry"]["content_sid"] == "HXexistingtemplate"
    client_factory.assert_not_called()


def test_twilio_content_refresh_updates_approval_status(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    app.config["TWILIO_ACCOUNT_SID"] = "ACtest"
    app.config["TWILIO_AUTH_TOKEN"] = "secret"

    dry_run = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "order_checkout", "dry_run": True},
    ).get_json()
    row = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=dry_run["create_request"]["friendly_name"],
        language=dry_run["create_request"]["language"],
        category="UTILITY",
        status="pending_approval",
        content_sid="HXpendingtemplate",
        metadata_json={"template_id": "order_checkout"},
    )
    db.session.add(row)
    db.session.commit()

    class _FakeContentHandle:
        def fetch(self):
            return SimpleNamespace(approval_requests=SimpleNamespace(status="APPROVED"))

    class _FakeContents:
        def __call__(self, content_sid):
            assert content_sid == "HXpendingtemplate"
            return _FakeContentHandle()

    fake_client = SimpleNamespace(content=SimpleNamespace(v1=SimpleNamespace(contents=_FakeContents())))

    with patch("routes.whatsapp_rules.Client", return_value=fake_client) as client_factory:
        response = client.post(
            "/api/admin/templates/twilio-content/refresh",
            headers=headers,
            json={"template_id": "order_checkout"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["refreshed"] is True
    assert payload["approval_status"] == "APPROVED"
    assert payload["registry"]["status"] == "approved"
    client_factory.assert_called_once_with("ACtest", "secret")

    refreshed = MessageTemplateRegistry.query.filter_by(content_sid="HXpendingtemplate").one()
    assert refreshed.status == "approved"
    assert refreshed.metadata_json["last_refresh_source"] == "twilio_content_fetch"
