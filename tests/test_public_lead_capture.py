from app import db
from models import AnalyticsEventV2, ChatSessionContext, TenantProfile, TenantTicket, User


def test_public_lead_capture_persists_profile_ticket_and_event(client):
    owner = User(email="ownerlead@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="lead-tenant", nombre="Lead Tenant", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    lead_payload = {
        "nombre": "Maria Cliente",
        "email": "maria@example.com",
        "telefono": "+5491112345678",
        "interes": "demo completo",
        "mensaje": "Quiero probar tickets y whatsapp",
        "chat_session_id": "lead-session-1",
        "anon_id": "anon-lead-1",
    }

    resp = client.post(
        "/api/public/lead-capture?tenant_slug=lead-tenant",
        json=lead_payload,
        headers={"X-Request-Id": "lead-req-1", "Idempotency-Key": "lead-key-1"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["contract_version"] == "public.lead_capture.v1"
    assert payload["request_id"] == "lead-req-1"
    assert resp.headers.get("X-Request-Id") == "lead-req-1"
    assert payload["tenant"]["slug"] == "lead-tenant"
    assert payload["ticket_type"] == "tenant_ticket"
    assert payload["ticket_id"]
    assert payload["lead"]["id"] == payload["lead_id"]
    assert payload["lead"]["status"] == "created"
    assert payload["idempotency_key"] == "lead-key-1"

    user = User.query.filter_by(email="maria@example.com").first()
    assert user is not None
    assert user.tenant_slug == "lead-tenant"

    ctx = ChatSessionContext.query.get("lead-session-1")
    assert ctx is not None
    assert (ctx.context_data or {}).get("lead_profile", {}).get("nombre") == "Maria Cliente"

    ticket = TenantTicket.query.get(payload["ticket_id"])
    assert ticket is not None
    assert ticket.fingerprint == "lead-key-1"
    assert ticket.categoria == "lead_capture"
    assert (ticket.datos_extra or {}).get("lead_stage") == "nuevo"
    assert (ticket.datos_extra or {}).get("lead_profile", {}).get("email") == "maria@example.com"

    event = AnalyticsEventV2.query.filter_by(event_name="lead_capture_created").first()
    assert event is not None
    assert event.entity_ref == f"tenant_ticket:{ticket.id}"

    retry = client.post(
        "/api/public/lead-capture?tenant_slug=lead-tenant",
        json=lead_payload,
        headers={"Idempotency-Key": "lead-key-1"},
    )

    assert retry.status_code == 200
    retry_payload = retry.get_json()
    assert retry_payload["ticket_id"] == ticket.id
    assert retry_payload["deduplicated"] is True
    assert TenantTicket.query.filter_by(fingerprint="lead-key-1").count() == 1


def test_public_lead_capture_creates_lead_from_landing_demo(client):
    owner = User(email="ownerlanding@test.com", name="Owner Landing", rol="admin", tipo_chat="municipio")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="municipio", nombre="Municipio", tipo="municipio", municipio_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    resp = client.post(
        "/api/public/lead-capture?tenant_slug=municipio",
        json={
            "tenant_slug": "municipio",
            "sector": "gobierno",
            "source": "landing_demo",
            "name": "Marcelo",
            "email": "marcelo@example.com",
            "phone": "2611234567",
            "message": "Quiero probar reclamos con ubicacion.",
            "demo_session_id": "demo-token-landing",
            "chat_session_id": "chat-landing-lead-1",
            "anon_id": "anon-landing-lead-1",
        },
        headers={"X-Request-Id": "landing-lead-1"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["contract_version"] == "public.lead_capture.v1"
    assert payload["ok"] is True
    assert payload["lead"]["status"] == "created"
    assert payload["ticket_id"]


def test_public_lead_capture_normalizes_long_chat_session_id(client):
    owner = User(email="ownerlead2@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="lead-tenant-2", nombre="Lead Tenant 2", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    long_session_id = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + ("x" * 160)
    resp = client.post(
        "/api/public/lead-capture?tenant_slug=lead-tenant-2",
        json={
            "nombre": "Lead Demo",
            "telefono": "+5491112345678",
            "interes": "demo gobierno",
            "chat_session_id": long_session_id,
            "anon_id": "anon-lead-2",
        },
        headers={"Idempotency-Key": "lead-key-2"},
    )

    assert resp.status_code == 200
    ctx = ChatSessionContext.query.filter(ChatSessionContext.chat_session_id.like("sid_%")).first()
    assert ctx is not None
    assert len(ctx.chat_session_id) <= 36
    assert (ctx.context_data or {}).get("source_chat_session_id") == long_session_id
    assert (ctx.context_data or {}).get("lead_profile", {}).get("source_chat_session_id") == long_session_id


def test_public_lead_capture_validation_error_is_contract_json(client):
    resp = client.post(
        "/api/public/lead-capture?tenant_slug=municipio&tenant=municipio",
        json={
            "tenant_slug": "municipio",
            "sector": "gobierno",
            "source": "landing.hero",
            "message": "Quiero probar una demo",
            "demo_session_id": "demo-token",
            "chat_session_id": "chat-lead-validation-1",
            "anon_id": "anon-lead-validation-1",
        },
        headers={"Origin": "https://www.chatboc.ar", "X-Request-Id": "lead-validation-1"},
    )

    assert resp.status_code == 400
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["contract_version"] == "public.lead_capture.v1"
    assert payload["request_id"] == "lead-validation-1"
    assert payload["reason_code"] == "validation_failed"
    assert payload["required_fields"] == ["name", "phone"]
    assert "contact" in payload["field_errors"]
    assert payload["field_errors"]["phone"]
    assert resp.headers.get("X-Request-Id") == "lead-validation-1"


def test_public_lead_capture_validation_returns_field_errors(client):
    test_public_lead_capture_validation_error_is_contract_json(client)
