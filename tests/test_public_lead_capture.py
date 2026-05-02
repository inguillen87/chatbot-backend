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
