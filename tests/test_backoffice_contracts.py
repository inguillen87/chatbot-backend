import jwt
from datetime import datetime, timedelta

from flask import current_app

from extensions import db
from models import (
    EncComentario,
    EncEncuesta,
    EncRespuesta,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TenantProfile,
    TicketComentario,
    User,
)


def _auth_headers(user: User) -> dict[str, str]:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)},
        current_app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return {"Authorization": f"Bearer {token}"}


def test_backoffice_navigation_exposes_role_based_modules(client):
    owner = User(
        email="backoffice-junin@test.com",
        name="Backoffice Junin",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="junin-backoffice",
        municipio_id=601,
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="junin-backoffice",
        nombre="Junin Backoffice",
        tipo="municipio",
        municipio_id=owner.id,
        plan="enterprise",
        capabilities_json={"advanced_analytics": True, "surveys": True, "maps": True, "people": True},
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers={**_auth_headers(owner), "X-Request-Id": "req-backoffice-nav-1"},
    )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id") == "req-backoffice-nav-1"
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.navigation.v1"
    assert payload["tenant_slug"] == tenant.slug
    assert payload["role"] == "admin"
    module_ids = [item["id"] for item in payload["modules"] if item["enabled"]]
    assert {"operations", "reports", "surveys", "people", "maps", "advanced_analytics"}.issubset(set(module_ids))
    assert payload["analytics_modes"]["statistics"]["enabled"] is True
    assert payload["analytics_modes"]["advanced_analytics"]["enabled"] is True
    assert payload["surveys_overview"]["route"] == "/admin/encuestas"
    action_ids = {item["id"] for item in payload["actions"]}
    assert {"export_backoffice", "executive_summary"}.issubset(action_ids)


def test_backoffice_summary_counts_real_operations_and_surveys(client):
    owner = User(
        email="backoffice-summary@test.com",
        name="Backoffice Summary",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="summary-tenant",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="summary-tenant",
        nombre="Summary Tenant",
        tipo="municipio",
        municipio_id=owner.id,
        plan="pro",
        capabilities_json={"advanced_analytics": True, "surveys": True},
    )
    db.session.add(tenant)
    db.session.flush()

    now = datetime.utcnow()
    db.session.add(
        MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            pregunta="Luminaria apagada",
            categoria="luminarias",
            estado="nuevo",
            fecha=now - timedelta(days=1),
            latitud=-34.6,
            longitud=-58.4,
        )
    )
    db.session.add(
        MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            pregunta="Caso resuelto",
            categoria="limpieza",
            estado="resuelto",
            fecha=now - timedelta(days=2),
        )
    )
    encuesta = EncEncuesta(
        tenant_id=tenant.id,
        slug="summary-survey",
        titulo="Sondeo Summary",
        estado="publicada",
        permitir_comentarios=True,
    )
    db.session.add(encuesta)
    db.session.flush()
    db.session.add(
        EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=tenant.id,
            huella_unica="summary-fp",
            lat=-34.61,
            lng=-58.41,
            submitted_at=now - timedelta(hours=3),
        )
    )
    db.session.add(
        EncComentario(
            encuesta_id=encuesta.id,
            texto="Revisar comentario",
            estado="revision",
        )
    )
    db.session.commit()

    response = client.get(
        "/api/app/backoffice/summary",
        query_string={"tenant_slug": tenant.slug, "window": "7d"},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.summary.v1"
    assert payload["title"] == "Resumen de los ultimos 7d"
    cards = {item["id"]: item for item in payload["cards"]}
    assert cards["pending_cases"]["value"] == 1
    assert cards["resolved_cases"]["value"] == 1
    assert cards["active_surveys"]["value"] == 1
    assert cards["live_votes"]["value"] == 1
    assert payload["surveys_overview"]["comments_pending_review"] == 1
    assert payload["surveys_overview"]["heatmap_available"] is True
    assert payload["ai_summary_available"] is True
    assert any(item["id"] == "top_pending_category" for item in payload["priorities"])


def test_backoffice_navigation_disables_unavailable_enterprise_modules(client):
    owner = User(
        email="backoffice-free@test.com",
        name="Backoffice Free",
        rol="admin",
        tipo_chat="pyme",
        tenant_slug="free-pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="free-pyme",
        nombre="Free Pyme",
        tipo="pyme",
        pyme_id=owner.id,
        plan="free",
        capabilities_json={"advanced_analytics": False, "surveys": False, "maps": False, "exports": False},
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    modules = {item["id"]: item for item in response.get_json()["modules"]}
    assert modules["surveys"]["enabled"] is False
    assert modules["maps"]["enabled"] is False
    assert modules["advanced_analytics"]["enabled"] is False
    assert response.get_json()["actions"] == []


def test_backoffice_navigation_supports_school_scope_without_frontend_hardcoding(client):
    owner = User(
        email="backoffice-school@test.com",
        name="Backoffice School",
        rol="admin",
        tipo_chat="pyme",
        tenant_slug="school-backoffice",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="school-backoffice",
        nombre="School Backoffice",
        tipo="colegio",
        pyme_id=owner.id,
        plan="pro",
        capabilities_json={"advanced_analytics": True, "surveys": True, "maps": True, "people": True, "exports": True},
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["tenant"]["scope"] == "colegio"
    modules = {item["id"]: item for item in payload["modules"]}
    assert modules["operations"]["enabled"] is True
    assert modules["surveys"]["enabled"] is True
    assert modules["people"]["enabled"] is True
    assert modules["maps"]["enabled"] is True
    assert modules["advanced_analytics"]["enabled"] is True
    assert {item["id"] for item in payload["actions"]} == {"export_backoffice", "executive_summary"}


def test_backoffice_v2_inbox_summary_prioritizes_real_ticket_work(client):
    owner = User(email="ops-junin@test.com", name="Ops Junin", rol="admin", tipo_chat="municipio")
    owner.set_password("pw")
    employee = User(email="ops-agent@test.com", name="Agente Obras", rol="empleado", tipo_chat="municipio")
    employee.set_password("pw")
    db.session.add_all([owner, employee])
    db.session.flush()

    tenant = TenantProfile(
        slug="ops-junin",
        nombre="Ops Junin",
        tipo="municipio",
        municipio_id=owner.id,
        plan="enterprise",
    )
    employee.empresa_id = owner.id
    employee.tenant_id = tenant.id
    db.session.add(tenant)
    db.session.flush()

    old = datetime.utcnow() - timedelta(days=3)
    assigned = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Luminaria rota",
        categoria="alumbrado",
        estado="nuevo",
        fecha=old,
        asignado_a_id=employee.id,
        canal_ingreso="whatsapp",
        nombre_vecino="Vecina Uno",
        telefono_vecino="+549261111111",
    )
    unassigned = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Bache peligroso",
        categoria="baches",
        estado="pendiente",
        fecha=old,
        canal_ingreso="web",
    )
    resolved = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Caso cerrado",
        categoria="limpieza",
        estado="resuelto",
        fecha=datetime.utcnow() - timedelta(days=1),
    )
    db.session.add_all([assigned, unassigned, resolved])
    db.session.flush()
    db.session.add(TicketComentario(municipio_ticket_id=unassigned.id, comentario="Sigue igual", es_admin=False))
    db.session.commit()

    response = client.get(
        "/api/v2/backoffice/operations/inbox-summary",
        query_string={"tenant_slug": tenant.slug, "scope": "municipio"},
        headers={**_auth_headers(owner), "X-Request-Id": "req-ops-inbox"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.inbox_summary.v1"
    assert payload["request_id"] == "req-ops-inbox"
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["open"] == 2
    assert payload["summary"]["resolved"] == 1
    assert payload["summary"]["sla_risk"] == 2
    assert payload["summary"]["unassigned"] == 1
    assert payload["summary"]["unread"] == 1
    assert any(view["id"] == "sla_risk" for view in payload["recommended_views"])
    assert any(view["id"] == "unassigned" for view in payload["recommended_views"])
    assert any(area["id"] == "alumbrado" for area in payload["filters"]["areas"])
    assert any(agent["id"] == employee.id for agent in payload["filters"]["agents"])
    first_item = payload["items"][0]
    assert first_item["request_id"] == "req-ops-inbox"
    assert first_item["detail_endpoint"].startswith("/tickets/municipio/")
    assert "allowed_actions" in first_item
    assert first_item["sla_status"] in {"risk", "overdue"}


def test_backoffice_v2_orders_summary_uses_validated_order_amounts(client):
    owner = User(email="orders-pyme@test.com", name="Orders Pyme", rol="admin", tipo_chat="pyme")
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="orders-pyme", nombre="Orders Pyme", tipo="pyme", pyme_id=owner.id, plan="pro")
    db.session.add(tenant)
    db.session.flush()

    paid = PymePedido(
        pyme_id=owner.id,
        tenant_id=tenant.id,
        asunto="Pedido pagado",
        detalles="[]",
        monto_total=1500,
        nombre_cliente="Cliente Uno",
    )
    paid.estado = "pagado"
    pending = PymePedido(
        pyme_id=owner.id,
        tenant_id=tenant.id,
        asunto="Pedido pendiente",
        detalles="[]",
        monto_total=700,
        nombre_cliente="Cliente Dos",
    )
    pending.estado = "pendiente"
    db.session.add_all([paid, pending])
    db.session.commit()

    response = client.get(
        "/api/v2/backoffice/orders/summary",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.orders_summary.v1"
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["active"] == 2
    assert payload["summary"]["confirmed_revenue"] == 1500
    assert payload["summary"]["pending_revenue"] == 700
    assert payload["summary"]["unassigned"] is None
    assert "confirm_payment" in payload["actions_by_status"]["pendiente"]
    assert payload["data_quality_notes"]


def test_backoffice_v2_contacts_summary_publishes_segments_without_frontend_rules(client):
    owner = User(
        email="contacts-pyme@test.com",
        name="Contacts Pyme",
        rol="admin",
        tipo_chat="pyme",
        telefono="+549261000000",
        acepta_marketing=True,
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="contacts-pyme", nombre="Contacts Pyme", tipo="pyme", pyme_id=owner.id, plan="pro")
    db.session.add(tenant)
    db.session.flush()
    db.session.add(
        PymeTicket(
            tenant_id=tenant.id,
            pregunta="Consulta",
            asunto="Consulta",
            categoria="ventas",
            estado="nuevo",
            nro_ticket=881001,
            telefono="+549261222222",
            email="cliente@test.com",
        )
    )
    db.session.add(
        PymePedido(
            pyme_id=owner.id,
            tenant_id=tenant.id,
            asunto="Pedido cliente",
            detalles="[]",
            monto_total=100,
            nombre_cliente="Cliente",
            email_cliente="cliente@test.com",
            telefono_cliente="+549261222222",
        )
    )
    db.session.commit()

    response = client.get(
        "/api/v2/backoffice/contacts/summary",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.contacts_summary.v1"
    assert payload["summary"]["total"] >= 2
    assert payload["summary"]["with_phone"] >= 2
    assert payload["summary"]["with_email"] >= 2
    assert payload["summary"]["opt_in_marketing"] == 1
    assert payload["summary"]["possible_duplicates"] >= 1
    assert payload["segments"]["channels"]
    assert payload["segments"]["sources"]


def test_backoffice_v2_team_coverage_summary_flags_uncovered_categories(client):
    owner = User(email="team-junin@test.com", name="Team Junin", rol="admin", tipo_chat="municipio")
    owner.set_password("pw")
    employee = User(
        email="team-agent@test.com",
        name="Agente Alumbrado",
        rol="empleado",
        tipo_chat="municipio",
        ticket_categorias="alumbrado",
    )
    employee.set_password("pw")
    db.session.add_all([owner, employee])
    db.session.flush()
    tenant = TenantProfile(slug="team-junin", nombre="Team Junin", tipo="municipio", municipio_id=owner.id, plan="enterprise")
    db.session.add(tenant)
    db.session.flush()
    employee.empresa_id = owner.id
    employee.tenant_id = tenant.id
    db.session.add_all(
        [
            MunicipioTicket(
                municipio_id=owner.id,
                tenant_id=tenant.id,
                pregunta="Luminaria",
                categoria="alumbrado",
                estado="nuevo",
                asignado_a_id=employee.id,
            ),
            MunicipioTicket(
                municipio_id=owner.id,
                tenant_id=tenant.id,
                pregunta="Bache",
                categoria="baches",
                estado="nuevo",
            ),
        ]
    )
    db.session.commit()

    response = client.get(
        "/api/v2/backoffice/team/coverage-summary",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.team_coverage_summary.v1"
    assert payload["summary"]["active_employees"] >= 2
    assert any(category["label"] == "alumbrado" for category in payload["categories_covered"])
    assert any(category["label"] == "baches" for category in payload["categories_without_owner"])
    assert any(item["id"].startswith("assign_category_") for item in payload["assignment_recommendations"])


def test_backoffice_v2_export_and_executive_summary_are_traceable(client):
    owner = User(email="export-junin@test.com", name="Export Junin", rol="admin", tipo_chat="municipio")
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="export-junin", nombre="Export Junin", tipo="municipio", municipio_id=owner.id, plan="enterprise")
    db.session.add(tenant)
    db.session.add(
        MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            pregunta="Exportar caso",
            categoria="general",
            estado="nuevo",
            fecha=datetime.utcnow() - timedelta(hours=3),
        )
    )
    db.session.commit()

    export_response = client.post(
        "/api/v2/backoffice/export",
        json={"tenant_slug": tenant.slug, "resource": "tickets", "format": "csv", "filters": {}, "include_ai_summary": True},
        headers={**_auth_headers(owner), "X-Request-Id": "req-export-contract"},
    )
    assert export_response.status_code == 200
    export_payload = export_response.get_json()
    assert export_payload["ok"] is True
    assert export_payload["request_id"] == "req-export-contract"
    assert export_payload["download_url"].endswith(".csv")
    assert export_payload["expires_at"]

    summary_response = client.post(
        "/api/v2/backoffice/executive-summary",
        json={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )
    assert summary_response.status_code == 200
    summary = summary_response.get_json()
    assert summary["contract_version"] == "backoffice.executive_summary.v1"
    assert summary["headline"]
    assert summary["confidence"] in {"low", "medium", "high"}
    assert "/api/v2/backoffice/operations/inbox-summary" in summary["source_endpoints"]
