from app import db
from models import ChatSessionContext, TenantProfile, User


def test_public_lead_capture_persists_profile_and_conversation(client):
    owner = User(email="ownerlead@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="lead-tenant", nombre="Lead Tenant", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    resp = client.post(
        "/api/public/lead-capture?tenant_slug=lead-tenant",
        json={
            "nombre": "María Cliente",
            "email": "maria@example.com",
            "telefono": "+5491112345678",
            "interes": "demo completo",
            "mensaje": "Quiero probar tickets y whatsapp",
            "chat_session_id": "lead-session-1",
            "anon_id": "anon-lead-1",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True

    user = User.query.filter_by(email="maria@example.com").first()
    assert user is not None
    assert user.tenant_slug == "lead-tenant"

    ctx = ChatSessionContext.query.get("lead-session-1")
    assert ctx is not None
    assert (ctx.context_data or {}).get("lead_profile", {}).get("nombre") == "María Cliente"
