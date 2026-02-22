import jwt
from datetime import datetime, timedelta

from app import db
from models import MunicipioTicket, TenantProfile, User


def _sa_headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_super_admin_leads_pipeline_returns_stage_metrics(client, app):
    sa = User(email="sa-pipeline@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")

    owner = User(email="owner-tenant@test.com", name="Owner", rol="admin", tipo_chat="municipio")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.flush()

    tenant = TenantProfile(slug="tenant-demo", nombre="Tenant Demo", tipo="municipio", municipio_id=owner.id)
    db.session.add(tenant)
    db.session.flush()

    ticket_new = MunicipioTicket(
        asunto="Lead prospecto demo - Bodega",
        categoria="lead_demo_prospecto",
        pregunta="Prospecto generado desde demo pública",
        estado="nuevo",
        tenant_id=tenant.id,
        municipio_id=owner.id,
        nombre_vecino="Lead Uno",
        email_vecino="lead1@test.com",
        telefono_vecino="+5491111111111",
        fecha=datetime.utcnow() - timedelta(days=1),
        ultima_actividad=datetime.utcnow(),
    )

    ticket_won = MunicipioTicket(
        asunto="Lead prospecto demo - Retail",
        categoria="lead_demo_prospecto",
        pregunta="Prospecto generado desde demo pública",
        estado="cerrado",
        tenant_id=tenant.id,
        municipio_id=owner.id,
        nombre_vecino="Lead Dos",
        email_vecino="lead2@test.com",
        telefono_vecino="+5492222222222",
        fecha=datetime.utcnow() - timedelta(days=2),
        ultima_actividad=datetime.utcnow(),
    )

    db.session.add_all([ticket_new, ticket_won])
    db.session.commit()

    resp = client.get("/api/admin/leads/pipeline", headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["total"] >= 2
    assert payload["by_stage"]["nuevo"] >= 1
    assert payload["by_stage"]["ganado"] >= 1
    assert "tenant-demo" in payload["by_tenant"]
    assert isinstance(payload.get("items"), list)
