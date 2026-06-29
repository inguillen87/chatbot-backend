from models import TenantProfile, User, db


def _seed_finance_tenant():
    owner = User(
        name="Finance Owner",
        email="finance-owner@test.com",
        password_hash="test",
        rol="admin",
        tipo_chat="pyme",
    )
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="banco-demo",
        nombre="Banco Demo",
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
