import jwt

from app import db
from models import MunicipioTicket, User


def _sa_headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_super_admin_bulk_stage_and_timeline_note(client, app):
    sa = User(email="sa-adv@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)
    db.session.commit()

    ticket = MunicipioTicket(
        pregunta="Necesito demo",
        asunto="Lead demo",
        estado="nuevo",
        nombre_vecino="Lead Uno",
        telefono_vecino="+549111111111",
        email_vecino="lead1@test.com",
    )
    db.session.add(ticket)
    db.session.commit()

    bulk_resp = client.patch(
        "/api/admin/leads/bulk-stage",
        json={
            "stage": "contactado",
            "updates": [{"ticket_type": "municipio", "ticket_id": ticket.id, "note": "Primer contacto"}],
        },
        headers=_sa_headers(app, sa),
    )
    assert bulk_resp.status_code == 200
    assert bulk_resp.get_json()["changed"] == 1

    note_resp = client.post(
        f"/api/admin/leads/municipio/{ticket.id}/timeline",
        json={"note": "Cliente pidió propuesta"},
        headers=_sa_headers(app, sa),
    )
    assert note_resp.status_code == 200
    payload = note_resp.get_json()
    assert payload["ok"] is True
    assert any(item.get("event") == "note" for item in payload["timeline"])


def test_super_admin_playbook_run_dry_preview(client, app):
    sa = User(email="sa-play@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)
    db.session.commit()

    ticket = MunicipioTicket(
        pregunta="Quiero demo urgente y presupuesto",
        asunto="Lead caliente",
        estado="nuevo",
        nombre_vecino="Lead Dos",
        telefono_vecino="+549333333333",
        email_vecino="lead2@test.com",
    )
    db.session.add(ticket)
    db.session.commit()

    resp = client.post(
        "/api/admin/leads/playbooks/run",
        json={"dry_run": True, "only_sla_breached": False, "limit": 10},
        headers=_sa_headers(app, sa),
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["count"] >= 1
    first = payload["items"][0]
    assert any(action.get("channel") == "whatsapp" for action in first["actions"])
    assert any(action.get("channel") == "email" for action in first["actions"])



def test_super_admin_strategic_overview(client, app):
    sa = User(email="sa-overview@test.com", name="SA Overview", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)
    db.session.commit()

    db.session.add(MunicipioTicket(pregunta="Lead 1", asunto="L1", estado="nuevo", nombre_vecino="A"))
    db.session.add(MunicipioTicket(pregunta="Lead 2", asunto="L2", estado="cerrado", nombre_vecino="B", detalles='{"lead_stage":"ganado"}'))
    db.session.commit()

    resp = client.get('/api/admin/leads/strategic-overview?since_days=60', headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['totals']['total_leads'] >= 2
    assert 'by_stage' in body
    assert 'by_tenant' in body


def test_super_admin_heatmap_categories_zones(client, app):
    sa = User(email="sa-heat@test.com", name="SA Heat", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)
    db.session.commit()

    db.session.add(MunicipioTicket(pregunta="R1", asunto="A1", categoria="luminaria", distrito="centro", latitud=-32.9, longitud=-68.8))
    db.session.add(MunicipioTicket(pregunta="R2", asunto="A2", categoria="basura", distrito="norte", latitud=-32.91, longitud=-68.81))
    db.session.commit()

    resp = client.get('/api/admin/analytics/heatmap-categories-zones?since_days=90', headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['total'] >= 2
    assert isinstance(body['top_categories'], list)
    assert isinstance(body['top_zones'], list)
    assert isinstance(body['heatmap_points'], list)
