from datetime import datetime, timedelta, timezone

import jwt

from models import AnalyticsEventV2, TenantProfile, TenantTicket, User, db
from services.finance_webview_access import (
    FINANCE_WEBVIEW_TOKEN_AUDIENCE,
    FINANCE_WEBVIEW_TOKEN_ISSUER,
    FINANCE_WEBVIEW_TOKEN_SCOPE,
    issue_finance_webview_token,
    verify_finance_webview_token,
)


def _seed_finance_tenant(slug="banco-demo"):
    owner = User(
        name="Finance Owner",
        email=f"finance-owner-{slug}@test.com",
        password_hash="test",
        rol="admin",
        tipo_chat="pyme",
    )
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=slug.replace("-", " ").title(),
        tipo="pyme",
        pyme_id=owner.id,
        vertical="finanzas",
        plan="full",
    )
    db.session.add(tenant)
    db.session.commit()
    return tenant


def _issue_finance_session(
    tenant_slug: str,
    flow: str,
    operation_code: str,
    *,
    amount=None,
    currency="ARS",
    contact_key=None,
):
    return issue_finance_webview_token(
        tenant_slug,
        flow,
        operation_code,
        amount=amount,
        currency=currency,
        contact_key=contact_key,
    )


def test_finance_webview_public_contract(client):
    _seed_finance_tenant()
    token = _issue_finance_session(
        "banco-demo",
        "operacion",
        "OP-123",
        amount="1500.75",
    )

    response = client.get(
        "/finanzas/banco-demo/operacion/OP-123",
        query_string={"session": token, "amount": "1500.75"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "finance.webview.v1"
    assert payload["tenant"]["slug"] == "banco-demo"
    assert payload["tenant"]["nombre"] == "Banco Demo"
    assert payload["operation"]["flow"] == "operacion"
    assert payload["operation"]["code"] == "OP-123"
    assert payload["operation"]["amount"] == "1500.75"
    assert payload["security_policy"]["card_data_in_chat_allowed"] is False
    assert payload["security_policy"]["identity_data_in_chat_allowed"] is False
    assert payload["security_policy"]["session_state"] == "verified"
    assert payload["security_policy"]["requires_idempotency_key"] is True
    assert "CVV" in payload["security_policy"]["never_request_in_chat"]
    assert payload["actions"]["primary"]["enabled"] is True
    assert payload["actions"]["primary"]["label"] == "Revisar y continuar"
    assert payload["experience"]["webview_flow_id"] == "finance_credit_collection_signature"
    assert payload["experience"]["crm_queue"]["id"] == "collections"
    assert payload["frontend_contract"]["render_as"] == "finance_secure_operation_view"
    assert "request_payment_plan" in {item["id"] for item in payload["action_catalog"]}
    assert "payment_webhook" in payload["events"]["success"]
    assert "payment_started" in payload["analytics"]["events"]


def test_finance_webview_api_alias_marks_missing_session(client):
    response = client.get("/api/public/finance/banco-demo/alta/ONB-9")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "finance.webview.v1"
    assert payload["operation"]["status"] == "session_required"
    assert payload["security_policy"]["session_state"] == "missing"
    assert payload["actions"]["primary"]["enabled"] is False
    assert payload["actions"]["primary"]["label"] == "Iniciar alta segura"
    assert payload["experience"]["webview_flow_id"] == "finance_onboarding_kyc"
    assert payload["experience"]["templates"] == ["finance_account_onboarding", "finance_kyc_review"]
    assert payload["frontend_contract"]["empty_state"] == "finance_session_required"


def test_finance_webview_legacy_session_keeps_safe_read_but_disables_actions(client):
    response = client.get(
        "/api/public/finance/banco-demo/operacion/OP-LEGACY",
        query_string={"session": "session-123456", "amount": "99.00"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["operation"]["amount"] == "99.00"
    assert payload["operation"]["status"] == "session_required"
    assert payload["security_policy"]["session_state"] == "invalid"
    assert payload["actions"]["support"]["enabled"] is False
    assert all(action["enabled"] is False for action in payload["action_catalog"])


def test_finance_action_creates_crm_ticket_analytics_event_and_is_idempotent(client):
    tenant = _seed_finance_tenant("cooperativa-demo")
    token = _issue_finance_session(
        "cooperativa-demo",
        "operacion",
        "OP-PLAN-1",
        amount="45000",
        currency="ARS",
        contact_key="wa:+5492610000000",
    )
    verified = verify_finance_webview_token(
        token,
        expected_tenant_slug="cooperativa-demo",
        expected_flow="operacion",
        expected_operation_code="OP-PLAN-1",
        amount="45000",
        currency="ARS",
        contact_key="wa:+5492610000000",
    )

    request_payload = {
        "session": token,
        "action_id": "request_payment_plan",
        "amount": "45000",
        "currency": "ARS",
        "comment": "Necesito pagar en 3 cuotas.",
        "contact": {
            "name": "Marcelo",
            "phone": "+5492610000000",
            "contact_key": "wa:+5492610000000",
        },
        "idempotency_key": "idem-plan-1",
    }
    response = client.post(
        "/api/public/finance/cooperativa-demo/operacion/OP-PLAN-1/actions",
        json=request_payload,
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["contract_version"] == "finance.action.v1"
    assert payload["status"] == "accepted"
    assert payload["action"]["id"] == "request_payment_plan"
    assert payload["crm_followup"]["queue"]["id"] == "collections"
    assert "finance_collection_due" in payload["crm_followup"]["template_candidates"]

    ticket = db.session.get(TenantTicket, payload["ticket"]["id"])
    assert ticket is not None
    assert ticket.tenant_id == tenant.id
    assert ticket.origen == "webview"
    assert ticket.categoria == "Cobranzas y planes de pago"
    assert ticket.datos_extra["type"] == "finance_operation"
    assert ticket.datos_extra["finance"]["action_id"] == "request_payment_plan"
    assert ticket.datos_extra["finance"]["amount"] == "45000.00"
    assert ticket.datos_extra["finance"]["session_state"] == "verified"
    assert ticket.datos_extra["finance"]["session_jti"] == verified["jti"]
    assert ticket.datos_extra["contact"]["contact_key"] == "wa:+5492610000000"

    event = AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="finance_payment_plan_requested",
        entity_ref=f"tenant_ticket:{ticket.id}",
    ).first()
    assert event is not None
    assert event.metadata_payload["crm_queue"] == "collections"
    assert event.metadata_payload["webview_flow_id"] == "finance_credit_collection_signature"
    assert event.session_id == verified["jti"]
    assert token not in str(ticket.datos_extra)
    assert token not in str(event.metadata_payload)
    assert event.session_id != token
    assert verified["iss"] == FINANCE_WEBVIEW_TOKEN_ISSUER
    assert verified["aud"] == FINANCE_WEBVIEW_TOKEN_AUDIENCE
    assert verified["scope"] == FINANCE_WEBVIEW_TOKEN_SCOPE
    assert verified["tenant_slug"] == "cooperativa-demo"
    assert verified["flow"] == "operacion"
    assert verified["operation_code"] == "OP-PLAN-1"
    assert verified["iat"] < verified["exp"]
    assert verified["jti"]

    duplicate = client.post(
        "/api/public/finance/cooperativa-demo/operacion/OP-PLAN-1/actions",
        json=request_payload,
    )
    assert duplicate.status_code == 200
    duplicate_payload = duplicate.get_json()
    assert duplicate_payload["status"] == "duplicate"
    assert duplicate_payload["ticket"]["id"] == ticket.id
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 1


def test_finance_action_rejects_sensitive_card_or_key_text(client):
    tenant = _seed_finance_tenant("fintech-demo")
    token = _issue_finance_session("fintech-demo", "operacion", "OP-RISK")

    response = client.post(
        "/api/public/finance/fintech-demo/operacion/OP-RISK/actions",
        json={
            "session": token,
            "action_id": "pay_securely",
            "comment": "mi CVV es 123 y mi clave bancaria es 0000",
            "idempotency_key": "risk-1",
        },
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["reason_code"] == "sensitive_finance_data_rejected"
    assert payload["security_policy"]["card_data_in_chat_allowed"] is False
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0


def test_finance_action_requires_signed_session_for_secure_ctas(client):
    _seed_finance_tenant("mutual-demo")

    response = client.post(
        "/finanzas/mutual-demo/financiacion/TASA-7/actions",
        json={"action_id": "pay_tax", "idempotency_key": "no-session"},
    )

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["reason_code"] == "finance_session_required"
    assert payload["action"]["id"] == "pay_tax"


def test_finance_action_rejects_legacy_length_only_session(client):
    tenant = _seed_finance_tenant("legacy-write-demo")

    response = client.post(
        "/api/public/finance/legacy-write-demo/operacion/OP-LEGACY-WRITE/actions",
        json={
            "session": "session-123456",
            "action_id": "pay_securely",
            "amount": "100",
            "currency": "ARS",
            "idempotency_key": "legacy-write",
        },
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "finance_session_invalid"
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0


def test_finance_action_rejects_forged_token(client):
    tenant = _seed_finance_tenant("forge-demo")
    valid = _issue_finance_session("forge-demo", "operacion", "OP-FORGE", amount="100")
    claims = jwt.decode(valid, options={"verify_signature": False})
    forged = jwt.encode(claims, "attacker-controlled-secret", algorithm="HS256")

    response = client.post(
        "/api/public/finance/forge-demo/operacion/OP-FORGE/actions",
        json={
            "session": forged,
            "action_id": "pay_securely",
            "amount": "100",
            "currency": "ARS",
            "idempotency_key": "forged-token",
        },
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "finance_session_invalid"
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0


def test_finance_action_rejects_expired_token(client):
    tenant = _seed_finance_tenant("expired-demo")
    valid = _issue_finance_session("expired-demo", "operacion", "OP-OLD", amount="100")
    claims = jwt.decode(valid, options={"verify_signature": False})
    now = datetime.now(timezone.utc)
    claims["iat"] = now - timedelta(minutes=10)
    claims["exp"] = now - timedelta(seconds=1)
    signing_secret = (
        client.application.config.get("FINANCE_WEBVIEW_TOKEN_SECRET")
        or client.application.config["SECRET_KEY"]
    )
    expired = jwt.encode(
        claims,
        signing_secret,
        algorithm="HS256",
    )

    response = client.post(
        "/api/public/finance/expired-demo/operacion/OP-OLD/actions",
        json={
            "session": expired,
            "action_id": "pay_securely",
            "amount": "100",
            "currency": "ARS",
            "idempotency_key": "expired-token",
        },
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "finance_session_expired"
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0


def test_finance_action_rejects_token_for_altered_tenant(client):
    _seed_finance_tenant("signed-tenant")
    target_tenant = _seed_finance_tenant("altered-tenant")
    token = _issue_finance_session("signed-tenant", "operacion", "OP-TENANT", amount="100")

    response = client.post(
        "/api/public/finance/altered-tenant/operacion/OP-TENANT/actions",
        json={
            "session": token,
            "action_id": "pay_securely",
            "amount": "100",
            "currency": "ARS",
            "idempotency_key": "altered-tenant",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "finance_session_context_mismatch"
    assert TenantTicket.query.filter_by(tenant_id=target_tenant.id).count() == 0


def test_finance_action_rejects_token_for_altered_operation(client):
    tenant = _seed_finance_tenant("operation-demo")
    token = _issue_finance_session("operation-demo", "operacion", "OP-ORIGINAL", amount="100")

    response = client.post(
        "/api/public/finance/operation-demo/operacion/OP-ALTERED/actions",
        json={
            "session": token,
            "action_id": "pay_securely",
            "amount": "100",
            "currency": "ARS",
            "idempotency_key": "altered-operation",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "finance_session_context_mismatch"
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0


def test_finance_action_rejects_token_for_altered_flow(client):
    tenant = _seed_finance_tenant("flow-demo")
    token = _issue_finance_session("flow-demo", "operacion", "OP-FLOW", amount="100")

    response = client.post(
        "/api/public/finance/flow-demo/financiacion/OP-FLOW/actions",
        json={
            "session": token,
            "action_id": "request_payment_plan",
            "amount": "100",
            "currency": "ARS",
            "idempotency_key": "altered-flow",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "finance_session_context_mismatch"
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0


def test_finance_action_rejects_token_for_altered_amount(client):
    tenant = _seed_finance_tenant("amount-demo")
    token = _issue_finance_session("amount-demo", "operacion", "OP-AMOUNT", amount="100")

    response = client.post(
        "/api/public/finance/amount-demo/operacion/OP-AMOUNT/actions",
        query_string={"amount": "100"},
        json={
            "session": token,
            "action_id": "pay_securely",
            "amount": "1000",
            "currency": "ARS",
            "idempotency_key": "altered-amount",
        },
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "finance_session_values_mismatch"
    assert TenantTicket.query.filter_by(tenant_id=tenant.id).count() == 0
