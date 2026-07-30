from datetime import datetime, timedelta
import json
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


def _seed(*, plan=None, role="admin"):
    admin = User(email=f"wa-rules-{role}@test.com", name="Admin", rol=role, tipo_chat="pyme")
    admin.set_password("pass")
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(slug="wa-rules-tenant", nombre="WA Rules", tipo="pyme", pyme_id=admin.id, plan=plan)
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


def test_whatsapp_rules_accepts_canonical_tenant_admin_alias(client, app):
    admin, tenant = _seed(role="tenant_admin")
    headers = _auth_headers(app, admin, tenant.slug)

    response = client.get("/api/admin/whatsapp/rules", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["tenant_id"] == tenant.id


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
    admin, tenant = _seed(plan="full")
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
    assert payload["blocked"] is False
    assert payload["integration_access"]["enabled"] is True
    assert isinstance(payload["execute_confirmation"], str)
    assert len(payload["execute_confirmation"]) > 40
    assert "order_checkout" not in payload["execute_confirmation"]
    assert payload["frontend_contract"]["render_as"] == "whatsapp_template_creation_lock"
    assert payload["frontend_contract"]["hide_execute_controls"] is False
    assert payload["operator_guardrails"]["execute_confirmation_issued"] is True
    assert payload["operator_guardrails"]["twilio_call_allowed"] is True
    assert payload["operator_guardrails"]["next_action"] == "execute_twilio_content_call"
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
    assert isinstance(finance_payload["execute_confirmation"], str)
    assert len(finance_payload["execute_confirmation"]) > 40
    finance_cta = finance_payload["create_request"]["types"]["twilio/call-to-action"]["actions"][0]
    assert finance_cta["url"] == "https://www.chatboc.ar/{{3}}"
    assert (
        finance_payload["create_request"]["variables"]["3"]
        == f"finanzas/{tenant.slug}/operacion/OP-1001?session=session-demo-123456"
    )
    assert finance_payload["content_family"] == "cta_webview"


def test_twilio_content_sync_dry_run_locked_tenant_does_not_return_execute_confirmation(client, app):
    admin, tenant = _seed(plan="free")
    headers = _auth_headers(app, admin, tenant.slug)

    response = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "order_checkout", "dry_run": True},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True
    assert payload["ready_to_create"] is False
    assert payload["blocked"] is True
    assert payload["locked_reason"] == "plan_full_required"
    assert "execute_confirmation" not in payload
    assert payload["integration_access"]["enabled"] is False
    assert payload["integration_access"]["required_plan"] == "full"
    assert payload["frontend_contract"]["render_as"] == "whatsapp_template_creation_lock"
    assert payload["frontend_contract"]["hide_execute_controls"] is True
    assert payload["frontend_contract"]["allow_dry_run_preview"] is True
    assert payload["operator_guardrails"]["dry_run_preview_allowed"] is True
    assert payload["operator_guardrails"]["execute_confirmation_issued"] is False
    assert payload["operator_guardrails"]["twilio_call_allowed"] is False
    assert payload["operator_guardrails"]["next_action"] == "upgrade_to_full"


def test_twilio_content_sync_blocks_execute_for_free_plan(client, app):
    admin, tenant = _seed(plan="free")
    headers = _auth_headers(app, admin, tenant.slug)

    response = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={
            "template_id": "order_checkout",
            "dry_run": False,
            "execute_confirmation": "sync_twilio_content:order_checkout",
        },
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["blocked"] is True
    assert payload["reason"] == "plan_full_required"
    assert payload["action"] == "create_twilio_content_template"
    assert payload["integration_access"]["enabled"] is False
    assert payload["error"] == "plan_required"
    assert payload["frontend_contract"]["render_as"] == "whatsapp_template_creation_lock"
    assert payload["frontend_contract"]["hide_execute_controls"] is True
    assert payload["operator_guardrails"]["twilio_call_allowed"] is False


def test_twilio_content_sync_creates_content_and_registry_row(client, app):
    admin, tenant = _seed(plan="full")
    headers = _auth_headers(app, admin, tenant.slug)
    app.config["TWILIO_ACCOUNT_SID"] = "ACtest"
    app.config["TWILIO_AUTH_TOKEN"] = "secret"

    class _FakeClient:
        def request(self, method, url, *, data, headers, timeout):
            if url == "https://content.twilio.com/v1/Content":
                assert method == "POST"
                assert data["friendly_name"]
                assert "twilio/text" in data["types"]
                return SimpleNamespace(status_code=201, text=json.dumps({"sid": "HXcreatedtemplate"}))
            assert url == (
                "https://content.twilio.com/v1/Content/"
                "HXcreatedtemplate/ApprovalRequests/whatsapp"
            )
            assert method == "POST"
            assert data["category"] == "UTILITY"
            return SimpleNamespace(status_code=201, text=json.dumps({"status": "PENDING"}))

    fake_client = _FakeClient()

    missing_confirmation = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "order_checkout", "dry_run": False},
    )
    assert missing_confirmation.status_code == 409

    preview = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "order_checkout", "dry_run": True},
    ).get_json()

    with patch("routes.whatsapp_rules.Client", return_value=fake_client) as client_factory:
        response = client.post(
            "/api/admin/templates/twilio-content/sync",
            headers=headers,
            json={
                "template_id": "order_checkout",
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["content_sid"] == "HXcreatedtemplate"
    assert payload["registry"]["status"] == "approval_pending"
    assert payload["registry"]["provider_status"] == "pending_approval"
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
    admin, tenant = _seed(plan="full")
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


def test_twilio_content_sync_blocks_automatic_retry_after_uncertain_provider_result(client, app):
    admin, tenant = _seed(plan="full")
    headers = _auth_headers(app, admin, tenant.slug)
    app.config["TWILIO_ACCOUNT_SID"] = "ACtest"
    app.config["TWILIO_AUTH_TOKEN"] = "secret"
    preview = client.post(
        "/api/admin/templates/twilio-content/sync",
        headers=headers,
        json={"template_id": "order_checkout", "dry_run": True},
    ).get_json()

    class _TimeoutClient:
        calls = 0

        def request(self, method, url, *, data, headers, timeout):
            self.calls += 1
            raise TimeoutError("provider timeout")

    fake_client = _TimeoutClient()
    with patch("routes.whatsapp_rules.Client", return_value=fake_client):
        first = client.post(
            "/api/admin/templates/twilio-content/sync",
            headers=headers,
            json={
                "template_id": "order_checkout",
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )
        second = client.post(
            "/api/admin/templates/twilio-content/sync",
            headers=headers,
            json={
                "template_id": "order_checkout",
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )

    assert first.status_code == 502
    assert second.status_code == 409
    assert fake_client.calls == 1
    row = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
    ).one()
    assert row.status == "sync_uncertain"
    assert row.metadata_json["sync_state"] == "uncertain"


def test_twilio_content_refresh_updates_approval_status(client, app):
    admin, tenant = _seed(plan="full")
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

    class _FakeClient:
        def request(self, method, url, *, data, headers, timeout):
            assert method == "GET"
            assert url == (
                "https://content.twilio.com/v1/Content/"
                "HXpendingtemplate/ApprovalRequests"
            )
            assert data is None
            return SimpleNamespace(
                status_code=200,
                text=json.dumps({"whatsapp": {"status": "APPROVED"}}),
            )

    fake_client = _FakeClient()

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
    assert refreshed.metadata_json["last_refresh_source"] == "twilio_approval_fetch"


def test_twilio_content_refresh_blocks_free_plan_before_twilio_call(client, app):
    admin, tenant = _seed(plan="free")
    headers = _auth_headers(app, admin, tenant.slug)
    row = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="demo_template",
        language="es",
        category="UTILITY",
        status="pending_approval",
        content_sid="HXpendingtemplate",
        metadata_json={"template_id": "order_checkout"},
    )
    db.session.add(row)
    db.session.commit()

    with patch("routes.whatsapp_rules.Client") as client_factory:
        response = client.post(
            "/api/admin/templates/twilio-content/refresh",
            headers=headers,
            json={"content_sid": "HXpendingtemplate"},
        )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["blocked"] is True
    assert payload["action"] == "refresh_twilio_content_template"
    assert payload["integration_access"]["enabled"] is False
    client_factory.assert_not_called()
