from app import db
from models import MunicipioTicket, User
from tests.auth_test_utils import clerk_superadmin_headers


def _sa_headers(app, user):
    return clerk_superadmin_headers(user)


def test_super_admin_can_update_lead_stage(client, app):
    sa = User(email="sa-stage@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner-stage@test.com", name="Owner", rol="admin", tipo_chat="municipio")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.flush()

    lead = MunicipioTicket(
        asunto="Lead prospecto demo - Salud",
        categoria="lead_demo_prospecto",
        pregunta="Prospecto generado desde demo pública",
        estado="nuevo",
        municipio_id=owner.id,
        nombre_vecino="Lead Stage",
        email_vecino="leadstage@test.com",
        telefono_vecino="+5493333333333",
    )
    db.session.add(lead)
    db.session.commit()

    resp = client.patch(
        f"/api/admin/leads/municipio/{lead.id}/stage",
        headers=_sa_headers(app, sa),
        json={"stage": "ganado", "note": "Cierre confirmado"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["lead_stage"] == "ganado"
    assert payload["ticket_status"] == "cerrado"

    db.session.refresh(lead)
    assert lead.estado == "cerrado"
    assert "Cierre confirmado" in (lead.detalles or "")


def test_super_admin_rejects_invalid_stage(client, app):
    sa = User(email="sa-stage2@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner-stage2@test.com", name="Owner", rol="admin", tipo_chat="municipio")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.flush()

    lead = MunicipioTicket(
        asunto="Lead prospecto demo - Retail",
        categoria="lead_demo_prospecto",
        pregunta="Prospecto generado desde demo pública",
        estado="nuevo",
        municipio_id=owner.id,
    )
    db.session.add(lead)
    db.session.commit()

    resp = client.patch(
        f"/api/admin/leads/municipio/{lead.id}/stage",
        headers=_sa_headers(app, sa),
        json={"stage": "etapa_inventada"},
    )
    assert resp.status_code == 400
