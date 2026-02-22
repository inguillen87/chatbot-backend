import jwt

from app import db
from models import EncEncuesta, EncRespuesta, MunicipioTicket, TenantProfile, User


def _headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_tenant_admin_can_list_and_update_leads(client, app):
    owner = User(email="owner-tenant@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-leads", nombre="Tenant Leads", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        pregunta="Consulta inicial",
        asunto="Lead tenant",
        estado="nuevo",
        nombre_vecino="Lead Tenant",
        telefono_vecino="+549222222222",
    )
    db.session.add(ticket)
    db.session.commit()

    list_resp = client.get(f"/api/admin/tenants/{tenant.slug}/leads", headers=_headers(app, owner))
    assert list_resp.status_code == 200
    data = list_resp.get_json()
    assert data["total"] >= 1
    assert data["items"][0]["ticket_id"] == ticket.id

    stage_resp = client.patch(
        f"/api/admin/tenants/{tenant.slug}/leads/municipio/{ticket.id}/stage",
        json={"stage": "calificado", "note": "Interés alto"},
        headers=_headers(app, owner),
    )
    assert stage_resp.status_code == 200
    body = stage_resp.get_json()
    assert body["lead_stage"] == "calificado"



def test_tenant_admin_bulk_stage_and_timeline(client, app):
    owner = User(email="owner-tenant-bulk@test.com", name="Owner Bulk", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-leads-bulk", nombre="Tenant Leads Bulk", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        pregunta="Lead para seguimiento",
        asunto="Lead tenant bulk",
        estado="nuevo",
        nombre_vecino="Lead Bulk",
        telefono_vecino="+549444444444",
    )
    db.session.add(ticket)
    db.session.commit()

    bulk_resp = client.patch(
        f"/api/admin/tenants/{tenant.slug}/leads/bulk-stage",
        json={"stage": "contactado", "updates": [{"ticket_type": "municipio", "ticket_id": ticket.id}]},
        headers=_headers(app, owner),
    )
    assert bulk_resp.status_code == 200
    assert bulk_resp.get_json()["changed"] == 1

    note_resp = client.post(
        f"/api/admin/tenants/{tenant.slug}/leads/municipio/{ticket.id}/timeline",
        json={"note": "Llamado realizado"},
        headers=_headers(app, owner),
    )
    assert note_resp.status_code == 200
    assert any(ev.get("event") == "tenant_note" for ev in note_resp.get_json()["timeline"])



def test_tenant_auto_assign_and_surveys_overview(client, app):
    owner = User(email="owner-tenant-auto@test.com", name="Owner Auto", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-auto", nombre="Tenant Auto", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    emp = User(email="emp-auto@test.com", name="Emp Auto", rol="empleado", es_empleado=True, tenant_id=tenant.id)
    emp.set_password("pass")
    emp.accesibilidad = {"employee_scope": {"categorias": ["luminaria"], "zonas": ["centro"], "permisos": ["tickets_update"]}}
    db.session.add(emp)

    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        pregunta="Luz quemada",
        asunto="Incidente",
        categoria="luminaria",
        distrito="centro",
        estado="nuevo",
        nombre_vecino="Vecino",
    )
    db.session.add(ticket)

    encuesta = EncEncuesta(tenant_id=tenant.id, slug="enc-auto", titulo="Encuesta Auto", estado="publicada", tipo="opinion")
    db.session.add(encuesta)
    db.session.commit()

    db.session.add(EncRespuesta(encuesta_id=encuesta.id, tenant_id=tenant.id, canal="web"))
    db.session.commit()

    assign_resp = client.post(
        f"/api/admin/tenants/{tenant.slug}/tickets/municipio/{ticket.id}/auto-assign",
        headers=_headers(app, owner),
    )
    assert assign_resp.status_code == 200
    assign_payload = assign_resp.get_json()
    assert assign_payload["assigned"] is True
    assert assign_payload["employee"]["id"] == emp.id

    overview_resp = client.get(f"/api/admin/tenants/{tenant.slug}/encuestas/overview", headers=_headers(app, owner))
    assert overview_resp.status_code == 200
    body = overview_resp.get_json()
    assert body["total_surveys"] >= 1
    assert body["total_responses"] >= 1
