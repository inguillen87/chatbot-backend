from datetime import datetime, timedelta, timezone

import jwt

from app import db
from models import CatalogoItem, EncEncuesta, EncRespuesta, MunicipioTicket, TenantProfile, TicketComentario, TicketRealtimeState, User


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



def test_tenant_suggest_and_workload_balance(client, app):
    owner = User(email="owner-work@test.com", name="Owner Work", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-workload", nombre="Tenant Workload", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    emp_heavy = User(email="emp-heavy@test.com", name="Emp Heavy", rol="empleado", es_empleado=True, tenant_id=tenant.id)
    emp_heavy.set_password("pass")
    emp_heavy.accesibilidad = {"employee_scope": {"categorias": ["luminaria"], "zonas": ["centro"], "permisos": ["tickets_assign"]}}

    emp_light = User(email="emp-light@test.com", name="Emp Light", rol="empleado", es_empleado=True, tenant_id=tenant.id)
    emp_light.set_password("pass")
    emp_light.accesibilidad = {"employee_scope": {"categorias": ["luminaria"], "zonas": ["centro"], "permisos": ["tickets_assign"]}}

    db.session.add(emp_heavy)
    db.session.add(emp_light)
    db.session.commit()

    for i in range(3):
        db.session.add(MunicipioTicket(tenant_id=tenant.id, asunto=f"H{i}", pregunta="x", categoria="luminaria", distrito="centro", estado="nuevo", asignado_a_id=emp_heavy.id))
    db.session.commit()

    suggest_resp = client.post(
        f"/api/admin/tenants/{tenant.slug}/employees/suggest-assignee",
        json={"categoria": "luminaria", "zona": "centro", "required_permission": "tickets_assign"},
        headers=_headers(app, owner),
    )
    assert suggest_resp.status_code == 200
    suggestions = suggest_resp.get_json()["suggestions"]
    assert suggestions[0]["employee_id"] == emp_light.id

    wl_resp = client.get(f"/api/admin/tenants/{tenant.slug}/employees/workload", headers=_headers(app, owner))
    assert wl_resp.status_code == 200
    items = wl_resp.get_json()["items"]
    assert items[0]["employee_id"] == emp_heavy.id
    assert items[0]["workload_open_tickets"] >= 3



def test_tenant_live_chat_schedule_config_and_public_status(client, app):
    owner = User(email="owner-schedule@test.com", name="Owner Schedule", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-schedule", nombre="Tenant Schedule", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    put_resp = client.put(
        f"/api/admin/tenants/{tenant.slug}/live-chat/schedule",
        json={
            "enabled": True,
            "days": [0, 1, 2, 3, 4, 5],
            "start_time": "08:00",
            "end_time": "20:00",
            "timezone": "America/Argentina/Buenos_Aires",
        },
        headers=_headers(app, owner),
    )
    assert put_resp.status_code == 200
    assert put_resp.get_json()["source"] == "tenant_config"

    get_resp = client.get(f"/api/admin/tenants/{tenant.slug}/live-chat/schedule", headers=_headers(app, owner))
    assert get_resp.status_code == 200
    body = get_resp.get_json()
    assert body["start_time"] == "08:00"
    assert body["end_time"] == "20:00"

    public_resp = client.get(f"/api/{tenant.slug}/live-chat/schedule")
    assert public_resp.status_code == 200
    public_body = public_resp.get_json()
    assert public_body["tenant_slug"] == tenant.slug
    assert public_body["source"] == "tenant_config"
    assert public_body.get("socket_transport_hint") == "disabled"
    assert public_body.get("socket_transports") == []
    assert public_body.get("socket_fallback_enabled") is False
    assert public_body.get("fallback_mode") == "http_chat"





def test_public_api_live_chat_schedule_alias_includes_socket_hints(client, app):
    owner = User(email="owner-schedule-api@test.com", name="Owner Schedule API", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-schedule-api", nombre="Tenant Schedule API", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    tenant.configuracion = {"live_chat_schedule": {"enabled": True, "days": [0,1,2,3,4], "start_time": "09:00", "end_time": "18:00"}}
    db.session.add(tenant)
    db.session.commit()

    resp = client.get("/api/live-chat/schedule", query_string={"tenant_slug": tenant.slug, "tenant": tenant.slug})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("tenant_slug") == tenant.slug
    assert body.get("socket_transport_hint") == "disabled"
    assert body.get("socket_transports") == []
    assert body.get("socket_fallback_enabled") is False
    assert body.get("fallback_mode") == "http_chat"


def test_public_live_chat_schedule_aliases_never_404_for_demo_widget(client, app):
    for path in (
        "/api/colegio-demo/live-chat/schedule",
        "/colegio-demo/live-chat/schedule",
        "/api/demo/live-chat/schedule",
        "/demo/live-chat/schedule",
    ):
        resp = client.get(path, query_string={"tenant_slug": "colegio-demo", "tenant": "colegio-demo"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("contract_version") == "live_chat.schedule.v1"
        assert body.get("tenant_slug") == "colegio-demo"
        assert body.get("socket_transport_hint") == "disabled"
        assert body.get("socket_transports") == []
        assert body.get("socket_enabled") is False
        assert body.get("realtime") is False
        assert body.get("fallback_mode") == "http_chat"

        options_resp = client.open(path, method="OPTIONS")
        assert options_resp.status_code == 200
        assert options_resp.headers.get("Access-Control-Allow-Origin")

def test_tenant_unread_ticket_summary(client, app):
    owner = User(email="owner-unread@test.com", name="Owner Unread", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-unread", nombre="Tenant Unread", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    ticket = MunicipioTicket(tenant_id=tenant.id, pregunta="x", asunto="x", estado="nuevo")
    db.session.add(ticket)
    db.session.commit()

    db.session.add(TicketComentario(municipio_ticket_id=ticket.id, comentario="hola", es_admin=False))
    db.session.add(TicketComentario(municipio_ticket_id=ticket.id, comentario="respuesta admin", es_admin=True))
    db.session.commit()

    resp = client.get(f"/api/admin/tenants/{tenant.slug}/tickets/unread-summary?since_minutes=60", headers=_headers(app, owner))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total_tickets_with_unread"] >= 1
    assert body["items"][0]["ticket_type"] in {"municipio", "pyme"}


def test_tenant_dashboard_bundle(client, app):
    owner = User(email="owner-dashboard@test.com", name="Owner Dashboard", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-dashboard", nombre="Tenant Dashboard", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    employee = User(email="emp-dashboard@test.com", name="Emp Dashboard", rol="empleado", es_empleado=True, tenant_id=tenant.id)
    employee.set_password("pass")
    db.session.add(employee)
    db.session.commit()

    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        pregunta="Necesito seguimiento",
        asunto="Lead dashboard",
        estado="nuevo",
        nombre_vecino="Lead Dashboard",
        asignado_a_id=employee.id,
    )
    db.session.add(ticket)

    survey = EncEncuesta(tenant_id=tenant.id, slug="enc-dashboard", titulo="Encuesta Dashboard", estado="publicada", tipo="opinion")
    db.session.add(survey)
    db.session.commit()

    db.session.add(EncRespuesta(encuesta_id=survey.id, tenant_id=tenant.id, canal="web"))
    db.session.add(TicketComentario(municipio_ticket_id=ticket.id, comentario="mensaje sin leer", es_admin=False))
    db.session.commit()
    db.session.add(
        TicketRealtimeState(
            ticket_type="municipio",
            ticket_id=ticket.id,
            viewer_key=f"user:{employee.id}",
            viewer_user_id=employee.id,
            viewer_role="empleado",
            presence_status="active",
            last_read_comment_id=0,
        )
    )
    db.session.commit()

    resp = client.get(
        f"/api/admin/tenants/{tenant.slug}/dashboard-bundle?since_minutes=60",
        headers=_headers(app, owner),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["tenant"]["slug"] == tenant.slug
    assert "summary" in body
    assert "leads" in body
    assert "surveys" in body
    assert "unread" in body
    assert "team" in body
    assert "recommended_actions" in body
    assert body["summary"]["total_leads"] >= 1
    assert body["summary"]["tickets_with_unread"] >= 1
    assert body["summary"]["active_viewers"] >= 1
    assert body["summary"]["unread_viewers"] >= 1
    assert body["leads"]["items"][0]["collaboration_state"]["active_viewers_count"] >= 1
    assert body["leads"]["items"][0]["priority_score"] >= 1
    assert body["leads"]["items"][0]["priority_breakdown"]["active_viewers"] >= 1
    assert len(body["leads"]["items"][0]["priority_reasons"]) >= 1
    assert body["leads"]["items"][0]["collaboration_state"]["operational_status"] in {"attention_needed", "actively_managed"}
    assert body["unread"]["items"][0]["collaboration_state"]["unread_viewer_count"] >= 1
    assert body["team"]["items"][0]["active_ticket_views"] >= 1
    assert body["team"]["items"][0]["unread_ticket_views"] >= 1


def test_tenant_heatmap_summary(client, app):
    owner = User(email="owner-heatmap@test.com", name="Owner Heatmap", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-heatmap", nombre="Tenant Heatmap", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    db.session.add(
        MunicipioTicket(
            tenant_id=tenant.id,
            pregunta="Luminaria rota",
            asunto="Luminaria",
            categoria="luminaria",
            distrito="centro",
            latitud=-32.9,
            longitud=-68.8,
            estado="nuevo",
        )
    )
    db.session.add(
        MunicipioTicket(
            tenant_id=tenant.id,
            pregunta="Bache",
            asunto="Bache",
            categoria="baches",
            distrito="norte",
            latitud=-32.91,
            longitud=-68.81,
            estado="nuevo",
        )
    )
    db.session.commit()

    resp = client.get(
        f"/api/admin/tenants/{tenant.slug}/heatmap-summary?limit_points=500",
        headers=_headers(app, owner),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["tenant_slug"] == tenant.slug
    assert body["total"] >= 2
    assert isinstance(body["top_categories"], list)
    assert isinstance(body["top_zones"], list)
    assert isinstance(body["hotspots"], list)
    assert isinstance(body["heatmap_points"], list)


def test_tenant_dashboard_bundle_counts_full_backlog_even_when_items_are_limited(client, app):
    owner = User(email="owner-backlog@test.com", name="Owner Backlog", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-backlog", nombre="Tenant Backlog", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    now = datetime.now(timezone.utc)
    older = now - timedelta(hours=2)

    ticket_recent = MunicipioTicket(
        tenant_id=tenant.id,
        pregunta="Lead reciente",
        asunto="Lead reciente",
        estado="nuevo",
        nombre_vecino="Lead reciente",
        ultima_actividad=now,
        fecha=now,
    )
    ticket_old_sla = MunicipioTicket(
        tenant_id=tenant.id,
        pregunta="Lead viejo",
        asunto="Lead viejo",
        estado="nuevo",
        nombre_vecino="Lead viejo",
        ultima_actividad=older,
        fecha=older,
    )
    db.session.add_all([ticket_recent, ticket_old_sla])
    db.session.commit()

    resp = client.get(
        f"/api/admin/tenants/{tenant.slug}/dashboard-bundle?since_minutes=180&leads_limit=1",
        headers=_headers(app, owner),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["meta"]["leads_limit"] == 1
    assert body["summary"]["total_leads"] == 2
    assert body["leads"]["total"] == 2
    assert body["summary"]["sla_breached"] == 1
    assert len(body["leads"]["items"]) == 1
    assert any(action["kind"] == "review_sla" for action in body["recommended_actions"])


def test_admin_tenant_catalog_supports_commercial_filters(client, app):
    owner = User(email="owner-catalog@test.com", name="Owner Catalog", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tenant-catalog", nombre="Tenant Catalog", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Producto Promo",
            categoria="Bebidas",
            precio="1000",
            precio_monetario=1000,
            moneda="ARS",
            modalidad="venta",
            promocion_info="2x1",
            disponible=True,
        )
    )
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Producto Sin Promo",
            categoria="Bebidas",
            precio="5000",
            precio_monetario=5000,
            moneda="ARS",
            modalidad="venta",
            disponible=True,
        )
    )
    db.session.commit()

    resp = client.get(
        f"/api/admin/tenants/{tenant.slug}/catalog/items?en_promocion=true&precio_max=2000&sort=precio_asc",
        headers=_headers(app, owner),
    )
    assert resp.status_code == 200
    items = resp.get_json()
    assert len(items) == 1
    assert items[0]["nombre"] == "Producto Promo"
    assert items[0]["channel_availability"]["whatsapp"] is True
    assert items[0]["price_numeric"] == 1000.0
