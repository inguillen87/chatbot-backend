import jwt
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import event

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
from utils.roles import (
    PERM_MANAGE_CATALOG,
    PERM_VIEW_STATS,
    ROLE_ANALYTICS_VIEWER,
    ROLE_CATALOG_MANAGER,
    canonical_role,
    has_permission,
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


def test_backoffice_navigation_limits_analytics_viewer_to_analytics_modules(
    client,
    monkeypatch,
):
    viewer = User(
        email="backoffice-analytics-viewer@test.com",
        name="Analytics Viewer",
        rol="analytics_viewer",
        tipo_chat="municipio",
        tenant_slug="analytics-viewer-tenant",
    )
    viewer.set_password("pw")
    db.session.add(viewer)
    db.session.flush()

    tenant = TenantProfile(
        slug="analytics-viewer-tenant",
        nombre="Analytics Viewer Tenant",
        tipo="municipio",
        municipio_id=viewer.id,
        plan="enterprise",
        capabilities_json={"statistics": True, "advanced_analytics": True},
    )
    db.session.add(tenant)
    db.session.commit()

    def unexpected_operational_query(*_args, **_kwargs):
        raise AssertionError("navigation-only roles must not query operational data")

    monkeypatch.setattr("routes.backoffice._operations_counts", unexpected_operational_query)
    monkeypatch.setattr("routes.backoffice._surveys_overview", unexpected_operational_query)

    response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(viewer),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert canonical_role(viewer.rol) == ROLE_ANALYTICS_VIEWER
    assert has_permission(viewer.rol, PERM_VIEW_STATS) is True
    assert payload["role"] == ROLE_ANALYTICS_VIEWER
    modules = {item["id"]: item for item in payload["modules"]}
    assert set(modules) == {"reports", "advanced_analytics"}
    assert modules["reports"]["enabled"] is True
    assert modules["advanced_analytics"]["enabled"] is True
    assert payload["actions"] == []
    assert "surveys_overview" not in payload

    summary = client.get(
        "/api/app/backoffice/summary",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(viewer),
    )
    assert summary.status_code == 403
    assert summary.get_json()["reason_code"] == "backoffice_operator_required"


def test_backoffice_navigation_limits_catalog_manager_to_catalog_without_operations(
    client,
    monkeypatch,
):
    manager = User(
        email="backoffice-catalog-manager@test.com",
        name="Catalog Manager",
        rol="catalog_manager",
        tipo_chat="pyme",
        tenant_slug="catalog-manager-tenant",
    )
    manager.set_password("pw")
    db.session.add(manager)
    db.session.flush()

    tenant = TenantProfile(
        slug="catalog-manager-tenant",
        nombre="Catalog Manager Tenant",
        tipo="pyme",
        pyme_id=manager.id,
        plan="pro",
        capabilities_json={"catalog": True},
    )
    db.session.add(tenant)
    db.session.commit()

    def unexpected_operational_query(*_args, **_kwargs):
        raise AssertionError("catalog navigation must not query operational data")

    monkeypatch.setattr("routes.backoffice._operations_counts", unexpected_operational_query)
    monkeypatch.setattr("routes.backoffice._surveys_overview", unexpected_operational_query)

    response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(manager),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert canonical_role(manager.rol) == ROLE_CATALOG_MANAGER
    assert has_permission(manager.rol, PERM_MANAGE_CATALOG) is True
    assert has_permission(manager.rol, PERM_VIEW_STATS) is False
    assert payload["role"] == ROLE_CATALOG_MANAGER
    assert payload["actions"] == []
    assert "analytics_modes" not in payload
    assert "surveys_overview" not in payload
    assert [item["id"] for item in payload["modules"]] == ["catalog"]
    catalog = payload["modules"][0]
    assert catalog["enabled"] is True
    assert catalog["route"] == "/perfil?tab=catalogo"


def test_backoffice_navigation_special_roles_respect_tenant_capabilities(client):
    analytics_viewer = User(
        email="backoffice-analytics-locked@test.com",
        name="Analytics Locked",
        rol="analytics_viewer",
        tipo_chat="municipio",
        tenant_slug="special-roles-locked",
    )
    analytics_viewer.set_password("pw")
    catalog_manager = User(
        email="backoffice-catalog-locked@test.com",
        name="Catalog Locked",
        rol="catalog_manager",
        tipo_chat="municipio",
        tenant_slug="special-roles-locked",
    )
    catalog_manager.set_password("pw")
    db.session.add_all([analytics_viewer, catalog_manager])
    db.session.flush()

    tenant = TenantProfile(
        slug="special-roles-locked",
        nombre="Special Roles Locked",
        tipo="municipio",
        municipio_id=analytics_viewer.id,
        plan="free",
        capabilities_json={
            "statistics": False,
            "advanced_analytics": False,
            "catalog": False,
        },
    )
    db.session.add(tenant)
    db.session.flush()
    analytics_viewer.tenant_id = tenant.id
    catalog_manager.tenant_id = tenant.id
    db.session.commit()

    analytics_response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(analytics_viewer),
    )
    assert analytics_response.status_code == 200
    analytics_modules = {
        item["id"]: item for item in analytics_response.get_json()["modules"]
    }
    assert set(analytics_modules) == {"reports", "advanced_analytics"}
    assert all(module["enabled"] is False for module in analytics_modules.values())

    catalog_response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(catalog_manager),
    )
    assert catalog_response.status_code == 200
    assert catalog_response.get_json()["modules"][0]["id"] == "catalog"
    assert catalog_response.get_json()["modules"][0]["enabled"] is False


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

    now = datetime.now(timezone.utc)
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
        EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=tenant.id,
            response_origin="legacy_unverified",
            huella_unica="summary-legacy-unverified",
            lat=-34.63,
            lng=-58.43,
            submitted_at=now - timedelta(hours=1),
        )
    )
    db.session.add(
        EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=tenant.id,
            response_origin="synthetic_demo",
            huella_unica="summary-trusted-synthetic",
            lat=-34.62,
            lng=-58.42,
            submitted_at=now - timedelta(hours=2),
            metadata_payload={
                "is_demo_seed": True,
                "demo_seed_contract_version": "surveys.demo_seeding.v1",
                "demo_batch_id": f"seed-{encuesta.id}-1720000000",
            },
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
    assert (
        payload["surveys_overview"]["response_provenance"]
        ["synthetic_responses_excluded"]
        == 1
    )
    assert (
        payload["surveys_overview"]["response_provenance"]
        ["unverified_responses_excluded"]
        == 1
    )
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


def test_backoffice_employee_requires_explicit_operational_scope(client):
    owner = User(
        email="backoffice-capability-owner@test.com",
        name="Capability Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("pw")
    employee_without_scope = User(
        email="backoffice-no-capability@test.com",
        name="Employee Without Scope",
        rol="empleado",
        tipo_chat="municipio",
        accesibilidad={
            "employee_scope": {
                "capabilities": {"analytics.operations.read": "false"},
            }
        },
    )
    employee_without_scope.set_password("pw")
    scoped_employee = User(
        email="backoffice-with-capability@test.com",
        name="Scoped Employee",
        rol="empleado",
        tipo_chat="municipio",
        accesibilidad={
            "employee_scope": {
                "capabilities": ["analytics.operations.read"],
            }
        },
    )
    scoped_employee.set_password("pw")
    db.session.add_all([owner, employee_without_scope, scoped_employee])
    db.session.flush()

    tenant = TenantProfile(
        slug="backoffice-capability",
        nombre="Backoffice Capability",
        tipo="municipio",
        municipio_id=owner.id,
        plan="enterprise",
    )
    db.session.add(tenant)
    db.session.flush()
    employee_without_scope.tenant_id = tenant.id
    scoped_employee.tenant_id = tenant.id
    db.session.commit()

    for endpoint in (
        "/api/app/backoffice/navigation",
        "/api/app/backoffice/summary",
        "/api/v2/backoffice/operations/inbox-summary",
    ):
        denied = client.get(
            endpoint,
            query_string={"tenant_slug": tenant.slug},
            headers=_auth_headers(employee_without_scope),
        )
        assert denied.status_code == 403
        denied_payload = denied.get_json()
        assert (
            denied_payload["reason_code"]
            == "backoffice_operational_capability_required"
        )
        assert "analytics.operations.read" in denied_payload["required_capabilities"]

        allowed = client.get(
            endpoint,
            query_string={"tenant_slug": tenant.slug},
            headers=_auth_headers(scoped_employee),
        )
        assert allowed.status_code == 200


def test_backoffice_survey_overview_uses_one_bounded_aggregate_and_excludes_nonreal_geo(client):
    owner = User(
        email="backoffice-scale-owner@test.com",
        name="Scale Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="backoffice-survey-scale",
        nombre="Backoffice Survey Scale",
        tipo="municipio",
        municipio_id=owner.id,
        plan="enterprise",
    )
    db.session.add(tenant)
    db.session.flush()
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug="backoffice-survey-scale",
        titulo="Backoffice survey scale",
        estado="publicada",
    )
    db.session.add(survey)
    db.session.flush()

    now = datetime.now(timezone.utc)
    db.session.add_all(
        [
            EncRespuesta(
                encuesta_id=survey.id,
                tenant_id=tenant.id,
                response_origin="real",
                huella_unica=f"backoffice-scale-real-{index}",
                submitted_at=now - timedelta(hours=1),
            )
            for index in range(64)
        ]
    )
    db.session.add_all(
        [
            EncRespuesta(
                encuesta_id=survey.id,
                tenant_id=tenant.id,
                response_origin="synthetic_demo",
                huella_unica=f"backoffice-scale-synthetic-{index}",
                phone=f"+5492619990{index}",
                barrio="Barrio reservado",
                lat=-34.61,
                lng=-58.41,
                submitted_at=now - timedelta(minutes=30),
            )
            for index in range(3)
        ]
    )
    db.session.add_all(
        [
            EncRespuesta(
                encuesta_id=survey.id,
                tenant_id=tenant.id,
                response_origin="legacy_unverified",
                huella_unica=f"backoffice-scale-unverified-{index}",
                ip=f"192.0.2.{index + 10}",
                barrio="Zona en cuarentena",
                lat=-34.62,
                lng=-58.42,
                submitted_at=now - timedelta(minutes=20),
            )
            for index in range(2)
        ]
    )
    db.session.commit()

    response_queries: list[str] = []
    loaded_response_ids: list[int] = []

    def capture_response_query(_conn, _cursor, statement, _parameters, _context, _many):
        if "enc_respuesta" in statement.lower():
            response_queries.append(statement)

    def capture_response_load(target, _context):
        loaded_response_ids.append(target.id)

    event.listen(db.engine, "before_cursor_execute", capture_response_query)
    event.listen(EncRespuesta, "load", capture_response_load)
    try:
        db.session.expire_all()
        response = client.get(
            "/api/app/backoffice/summary",
            query_string={"tenant_slug": tenant.slug, "window": "999999d"},
            headers=_auth_headers(owner),
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_response_query)
        event.remove(EncRespuesta, "load", capture_response_load)

    assert response.status_code == 200
    payload = response.get_json()
    overview = payload["surveys_overview"]
    provenance = overview["response_provenance"]
    assert payload["window"] == "90d"
    assert overview["live_votes"] == 64
    assert overview["heatmap_available"] is False
    assert provenance["real_responses_included"] == 64
    assert provenance["synthetic_responses_excluded"] == 3
    assert provenance["unverified_responses_excluded"] == 2
    assert loaded_response_ids == []
    assert len(response_queries) == 1
    assert "sum(case" in " ".join(response_queries[0].lower().split())
    serialized = response.get_data(as_text=True)
    assert "+5492619990" not in serialized
    assert "Barrio reservado" not in serialized
    assert "Zona en cuarentena" not in serialized


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


def test_backoffice_v2_export_and_executive_summary_are_traceable(
    client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("routes.backoffice._export_dir", lambda: tmp_path)

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


def test_backoffice_v2_rejects_end_user_before_pii_or_export_side_effects(client, monkeypatch):
    owner = User(email="operator-gate-owner@test.com", name="Operator Gate", rol="admin", tipo_chat="municipio")
    owner.set_password("pw")
    end_user = User(
        email="operator-gate-citizen@test.com",
        name="Citizen",
        rol="usuario",
        tipo_chat="municipio",
        tenant_slug="operator-gate",
    )
    end_user.set_password("pw")
    client_alias = User(
        email="operator-gate-client@test.com",
        name="Client Alias",
        rol="cliente",
        tipo_chat="municipio",
        tenant_slug="operator-gate",
    )
    client_alias.set_password("pw")
    db.session.add_all([owner, end_user, client_alias])
    db.session.flush()
    tenant = TenantProfile(
        slug="operator-gate",
        nombre="Operator Gate",
        tipo="municipio",
        municipio_id=owner.id,
        plan="enterprise",
    )
    db.session.add(tenant)
    db.session.flush()
    end_user.tenant_id = tenant.id
    client_alias.tenant_id = tenant.id
    db.session.commit()

    export_dir_calls = []
    write_calls = []
    monkeypatch.setattr("routes.backoffice._export_dir", lambda: export_dir_calls.append(True))
    monkeypatch.setattr("routes.backoffice._write_csv_export", lambda path, rows: write_calls.append(rows))

    for blocked_user in (end_user, client_alias):
        headers = _auth_headers(blocked_user)
        for endpoint in (
            "/api/app/backoffice/navigation",
            "/api/app/backoffice/summary",
            "/api/v2/backoffice/operations/inbox-summary",
            "/api/v2/backoffice/orders/summary",
            "/api/v2/backoffice/contacts/summary",
            "/api/v2/backoffice/team/coverage-summary",
        ):
            response = client.get(endpoint, query_string={"tenant_slug": tenant.slug}, headers=headers)
            assert response.status_code == 403
            assert response.get_json()["reason_code"] == "backoffice_operator_required"

    headers = _auth_headers(end_user)
    export_response = client.post(
        "/api/v2/backoffice/export",
        json={"tenant_slug": tenant.slug, "resource": "contacts", "format": "csv"},
        headers=headers,
    )
    executive_response = client.post(
        "/api/v2/backoffice/executive-summary",
        json={"tenant_slug": tenant.slug},
        headers=headers,
    )

    assert export_response.status_code == 403
    assert executive_response.status_code == 403
    assert export_dir_calls == []
    assert write_calls == []


def test_backoffice_v2_employee_sees_and_exports_only_allowed_ticket_categories(client, monkeypatch, tmp_path):
    owner = User(email="category-owner@test.com", name="Category Owner", rol="admin", tipo_chat="municipio")
    owner.set_password("pw")
    employee = User(
        email="category-employee@test.com",
        name="Category Employee",
        rol="empleado",
        tipo_chat="municipio",
        ticket_categorias="alumbrado",
    )
    employee.set_password("pw")
    db.session.add_all([owner, employee])
    db.session.flush()
    tenant = TenantProfile(
        slug="category-scoped-backoffice",
        nombre="Category Scoped Backoffice",
        tipo="municipio",
        municipio_id=owner.id,
        plan="enterprise",
    )
    db.session.add(tenant)
    db.session.flush()
    employee.tenant_id = tenant.id
    employee.empresa_id = owner.id

    allowed = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Luminaria permitida",
        categoria="alumbrado",
        estado="nuevo",
        nombre_vecino="Allowed Citizen",
        email_vecino="allowed-citizen@test.com",
        telefono_vecino="111111",
    )
    restricted = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Bache restringido",
        categoria="baches",
        estado="nuevo",
        nombre_vecino="Restricted Citizen",
        email_vecino="restricted-citizen@test.com",
        telefono_vecino="222222",
    )
    db.session.add_all([allowed, restricted])
    db.session.commit()

    headers = _auth_headers(employee)
    inbox_response = client.get(
        "/api/v2/backoffice/operations/inbox-summary",
        query_string={"tenant_slug": tenant.slug, "scope": "municipio"},
        headers=headers,
    )
    assert inbox_response.status_code == 200
    item_ids = {item["id"] for item in inbox_response.get_json()["items"]}
    assert allowed.id in item_ids
    assert restricted.id not in item_ids

    written_rows = []
    monkeypatch.setattr("routes.backoffice._export_dir", lambda: tmp_path)
    monkeypatch.setattr("routes.backoffice._write_csv_export", lambda path, rows: written_rows.extend(rows))
    export_response = client.post(
        "/api/v2/backoffice/export",
        json={"tenant_slug": tenant.slug, "resource": "contacts", "format": "csv"},
        headers=headers,
    )

    assert export_response.status_code == 200
    exported_emails = {row.get("email") for row in written_rows}
    assert "allowed-citizen@test.com" in exported_emails
    assert "restricted-citizen@test.com" not in exported_emails
