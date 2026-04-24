import jwt

from app import db
from models import CatalogoItem, ChatSessionContext, EncEncuesta, EncRespuesta, LlmInteractionLog, MunicipioTicket, TenantProfile, User


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
    assert 'portfolio' in body
    assert 'alerts' in body


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



def test_super_admin_realtime_ai_and_surveys_overview(client, app):
    sa = User(email="sa-rt@test.com", name="SA RT", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)

    tenant_owner = User(email="owner-surv@test.com", name="Owner Surv", rol="admin", tipo_chat="pyme")
    tenant_owner.set_password("pass")
    db.session.add(tenant_owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-surv", nombre="Tenant Surv", tipo="pyme", pyme_id=tenant_owner.id)
    db.session.add(tenant)
    db.session.commit()

    db.session.add(MunicipioTicket(tenant_id=tenant.id, pregunta="R1", asunto="A1", estado="nuevo"))
    enc = EncEncuesta(tenant_id=tenant.id, slug="enc-rt", titulo="Encuesta RT", estado="publicada", tipo="opinion", es_votacion_envivo=True)
    db.session.add(enc)
    db.session.commit()

    db.session.add(EncRespuesta(encuesta_id=enc.id, tenant_id=tenant.id, canal="web"))
    db.session.add(ChatSessionContext(chat_session_id="session-rt-1", context_data={}))
    db.session.commit()
    db.session.add(LlmInteractionLog(chat_session_id="session-rt-1", user_query="hola", status="pending_review"))
    db.session.commit()

    rt_resp = client.get('/api/admin/analytics/realtime-ai?minutes=120', headers=_sa_headers(app, sa))
    assert rt_resp.status_code == 200
    rt_body = rt_resp.get_json()
    assert 'llm' in rt_body
    assert 'tickets' in rt_body

    surv_resp = client.get('/api/admin/encuestas/overview?since_days=60', headers=_sa_headers(app, sa))
    assert surv_resp.status_code == 200
    surv_body = surv_resp.get_json()
    assert surv_body['total_surveys'] >= 1
    assert surv_body['total_responses'] >= 1



def test_super_admin_tenant_health(client, app):
    sa = User(email="sa-health@test.com", name="SA Health", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)

    owner = User(email="owner-health@test.com", name="Owner Health", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-health", nombre="Tenant Health", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    db.session.add(MunicipioTicket(tenant_id=tenant.id, pregunta="a", asunto="a", estado="nuevo"))
    db.session.add(MunicipioTicket(tenant_id=tenant.id, pregunta="b", asunto="b", estado="cerrado", detalles='{"lead_stage":"ganado"}'))
    encuesta = EncEncuesta(tenant_id=tenant.id, slug="enc-health", titulo="Encuesta Health", estado="publicada", tipo="opinion")
    db.session.add(encuesta)
    db.session.commit()

    db.session.add(EncRespuesta(encuesta_id=encuesta.id, tenant_id=tenant.id, canal="web"))
    db.session.commit()

    resp = client.get('/api/admin/analytics/tenant-health?since_days=60', headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['total_tenants'] >= 1
    row = next(item for item in body['items'] if item['tenant_slug'] == tenant.slug)
    assert row['health_score'] >= 0
    assert 'alerts' in row
    assert 'onboarding_completion' in row


def test_super_admin_tenant_profile_360(client, app):
    sa = User(email="sa-profile@test.com", name="SA Profile", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)

    owner = User(email="owner-profile@test.com", name="Owner Profile", rol="admin", tipo_chat="pyme", token="profile-token")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(
        slug="tenant-profile-360",
        nombre="Tenant Profile 360",
        tipo="pyme",
        pyme_id=owner.id,
        plan="growth",
        dominio="tenant.example.com",
        logo_url="https://cdn.example.com/logo.png",
        whatsapp_sender_id="5491112345678",
        configuracion={"analytics_enabled": True},
    )
    db.session.add(tenant)
    db.session.commit()

    db.session.add(CatalogoItem(user_id=owner.id, tenant_id=tenant.id, nombre="Producto", descripcion="Desc"))
    encuesta = EncEncuesta(tenant_id=tenant.id, slug="enc-profile", titulo="Encuesta", estado="publicada", tipo="opinion")
    db.session.add(encuesta)
    db.session.add(MunicipioTicket(tenant_id=tenant.id, pregunta="Lead", asunto="Lead", estado="nuevo"))
    db.session.commit()

    db.session.add(EncRespuesta(encuesta_id=encuesta.id, tenant_id=tenant.id, canal="web"))
    db.session.commit()

    resp = client.get(f'/api/admin/tenants/{tenant.slug}/profile-360?since_days=60', headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['tenant']['slug'] == tenant.slug
    assert body['owner']['email'] == owner.email
    assert body['health']['score'] >= 0
    assert body['metrics']['catalog_items'] >= 1
    assert body['onboarding']['checklist']['catalog'] is True


def test_super_admin_executive_summary_bundle(client, app):
    sa = User(email="sa-bundle@test.com", name="SA Bundle", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    db.session.add(sa)

    owner = User(email="owner-bundle@test.com", name="Owner Bundle", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-bundle", nombre="Tenant Bundle", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    db.session.add(MunicipioTicket(tenant_id=tenant.id, pregunta="Lead bundle", asunto="Lead bundle", estado="nuevo"))
    db.session.add(CatalogoItem(user_id=owner.id, tenant_id=tenant.id, nombre="Bundle product", descripcion="Desc"))
    db.session.commit()

    resp = client.get('/api/admin/analytics/executive-summary?since_days=60&minutes=120', headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'strategic_overview' in body
    assert 'tenant_health' in body
    assert 'realtime' in body
    assert 'heatmap' in body
    assert 'recommended_actions' in body
    assert body['strategic_overview']['totals']['total_leads'] >= 1
    assert body['tenant_health']['total_tenants'] >= 1
