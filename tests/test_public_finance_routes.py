from models import AnalyticsEventV2, TenantProfile, TenantTicket, User, db


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


def test_finance_webview_public_contract(client):
    _seed_finance_tenant()

    response = client.get("/finanzas/banco-demo/operacion/OP-123?session=session-123456&amount=1500.75")

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
    assert payload["security_policy"]["session_state"] == "present"
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
    assert payload["security_policy"]["session_state"] == "missing_or_short"
    assert payload["actions"]["primary"]["enabled"] is False
    assert payload["actions"]["primary"]["label"] == "Iniciar alta segura"
    assert payload["experience"]["webview_flow_id"] == "finance_onboarding_kyc"
    assert payload["experience"]["templates"] == ["finance_account_onboarding", "finance_kyc_review"]
    assert payload["frontend_contract"]["empty_state"] == "finance_session_required"


def test_finance_action_creates_crm_ticket_analytics_event_and_is_idempotent(client):
    tenant = _seed_finance_tenant("cooperativa-demo")

    request_payload = {
        "session": "session-123456",
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

    ticket = TenantTicket.query.get(payload["ticket"]["id"])
    assert ticket is not None
    assert ticket.tenant_id == tenant.id
    assert ticket.origen == "webview"
    assert ticket.categoria == "Cobranzas y planes de pago"
    assert ticket.datos_extra["type"] == "finance_operation"
    assert ticket.datos_extra["finance"]["action_id"] == "request_payment_plan"
    assert ticket.datos_extra["finance"]["amount"] == "45000.00"
    assert ticket.datos_extra["finance"]["session_state"] == "present"
    assert ticket.datos_extra["contact"]["contact_key"] == "wa:+5492610000000"

    event = AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="finance_payment_plan_requested",
        entity_ref=f"tenant_ticket:{ticket.id}",
    ).first()
    assert event is not None
    assert event.metadata_payload["crm_queue"] == "collections"
    assert event.metadata_payload["webview_flow_id"] == "finance_credit_collection_signature"

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

    response = client.post(
        "/api/public/finance/fintech-demo/operacion/OP-RISK/actions",
        json={
            "session": "session-123456",
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

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["reason_code"] == "finance_session_required"
    assert payload["action"]["id"] == "pay_tax"
