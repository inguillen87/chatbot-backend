import jwt

from app import db
from models import ChatSessionContext, Conversacion, User


def _sa_headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_super_admin_leads_interactions_returns_ranked_items(client, app):
    sa = User(email="sa-leads@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    lead = User(email="leaduser@test.com", name="Lead User", rol="usuario", tipo_chat="pyme", tenant_slug="lead-tenant")
    lead.set_password("pass")
    db.session.add_all([sa, lead])
    db.session.commit()

    ctx = ChatSessionContext(
        chat_session_id="lead-session-feed",
        user_id=lead.id,
        anon_id="anon-feed-1",
        context_data={
            "lead_profile": {
                "nombre": "Lead User",
                "email": "leaduser@test.com",
                "telefono": "+5491111111111",
                "interes": "municipio+pyme",
                "mensaje": "Necesito demo urgente",
                "tenant_slug": "lead-tenant",
            }
        },
    )
    db.session.add(ctx)

    db.session.add(
        Conversacion(
            user_id=lead.id,
            pyme_id=None,
            pregunta="Necesito resolver algo urgente con reclamos",
            respuesta="ok",
            fuente="web",
            rubro="pyme",
            session_id="anon-feed-1",
        )
    )
    db.session.commit()

    resp = client.get("/api/admin/leads/interactions", headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] >= 1
    assert isinstance(payload.get("top_questions"), list)
    first = payload["items"][0]
    assert "relevance_score" in first
    assert first["lead"]["email"] == "leaduser@test.com"
