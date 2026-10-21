import json
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import event

from extensions import db
from services.auth_session_lifecycle import issue_token
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
    token = issue_token(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)},
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
    assert {"operations", "reports", "surveys", "people", "maps", "advanced_analytics", "implementation"}.issubset(set(module_ids))
    implementation = next(item for item in payload["modules"] if item["id"] == "implementation")
    assert implementation["route"] == "/implementacion"
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

    operational_queries: list[str] = []

    def capture_operational_query(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lower()
        if "municipio_ticket" in normalized or any(
            table in normalized
            for table in ("enc_encuesta", "enc_respuesta", "enc_comentario")
        ):
            operational_queries.append(normalized)

    event.listen(db.engine, "before_cursor_execute", capture_operational_query)
    try:
        response = client.get(
            "/api/app/backoffice/summary",
            query_string={"tenant_slug": tenant.slug, "window": "7d"},
            headers=_auth_headers(owner),
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", capture_operational_query)

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
    survey_queries = [
        statement
        for statement in operational_queries
        if any(table in statement for table in ("enc_encuesta", "enc_respuesta", "enc_comentario"))
    ]
    ticket_queries = [statement for statement in operational_queries if "municipio_ticket" in statement]
    # One response/comment aggregate plus one tenant-bound instrument read.
    # Persisted publication alone cannot establish current reception authority.
    assert len(survey_queries) == 2
    assert all(table in survey_queries[0] for table in ("enc_encuesta", "enc_respuesta", "enc_comentario"))
    # One aggregate powers all counters; a second query obtains the top pending
    # category. The old implementation performed four counter queries here.
    assert len(ticket_queries) == 2


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
        if endpoint.endswith("/navigation"):
            assert "implementation" not in {
                item["id"] for item in allowed.get_json()["modules"]
            }


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
        datos_extra={"sla": {"status": "at_risk"}},
    )
    unassigned = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Bache peligroso",
        categoria="baches",
        estado="pendiente",
        fecha=old,
        canal_ingreso="web",
        detalles=json.dumps({"sla": {"due_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}}),
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


def _sla_contract_tenant(kind, suffix):
    owner = User(email=f"sla-{kind}-{suffix}@example.invalid", name="SLA contract fixture", rol="admin", tipo_chat=kind)
    owner.set_password("local-test-fixture")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug=f"sla-{kind}-{suffix}", nombre="SLA fixture", tipo=kind,
        municipio_id=owner.id if kind == "municipio" else None,
        pyme_id=owner.id if kind == "pyme" else None, plan="enterprise")
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
    return owner, tenant


def _sla_contract_ticket(kind, owner, tenant, number, *, created, metadata, state="nuevo", details=None):
    values = dict(tenant_id=tenant.id, pregunta=f"SLA fixture {number}", categoria="general",
        estado=state, fecha=created, datos_extra=metadata)
    if kind == "municipio":
        return MunicipioTicket(**values, municipio_id=owner.id, ultima_actividad=created,
            detalles=json.dumps(details) if details is not None else None)
    return PymeTicket(**values, nro_ticket=number)


@pytest.mark.parametrize("kind", ["municipio", "pyme"])
def test_home_sla_matches_reports_evidence_not_ticket_age(client, kind):
    """Local HTTP/ORM fixtures exercise both real handlers, not Preview acceptance."""
    owner, tenant = _sla_contract_tenant(kind, "mixed")
    foreign_owner, foreign_tenant = _sla_contract_tenant(kind, "foreign")
    now = datetime.now(timezone.utc)
    recent, old = now - timedelta(minutes=10), now - timedelta(days=30)
    cases = [
        (old, {}, "nuevo", None),  # Old age cannot establish any SLA.
        (recent, {"sla": {"due_at": (now - timedelta(hours=2)).isoformat()}}, "nuevo", None),
        (old, {"sla": {"due_at": (now + timedelta(days=2)).isoformat()}}, "nuevo",
            {"sla": {"due_at": (now - timedelta(days=1)).isoformat()}}),  # datos_extra wins.
        (recent, {"sla": {"due_at": (now + timedelta(hours=2)).isoformat()}}, "nuevo", None),
        (old, {"sla": {"due_at": "invalid-deadline"}}, "nuevo", None),
        (old, {"sla": {"state": "paused", "due_at": (now - timedelta(days=1)).isoformat()}}, "nuevo", None),
        (old, {"sla": {"due_at": (now - timedelta(days=1)).isoformat()}}, "cerrado", None),
    ]
    tickets = [_sla_contract_ticket(kind, owner, tenant, 700 + index,
        created=created, metadata=metadata, state=state, details=details)
        for index, (created, metadata, state, details) in enumerate(cases)]
    foreign = _sla_contract_ticket(kind, foreign_owner, foreign_tenant, 900,
        created=old, metadata={"sla": {"state": "breached"}})
    db.session.add_all([*tickets, foreign])
    db.session.commit()
    headers = {**_auth_headers(owner), "X-Tenant": tenant.slug}
    home = client.get("/api/v2/backoffice/operations/inbox-summary",
        query_string={"tenant_slug": tenant.slug, "scope": kind}, headers=headers)
    reports = client.get("/api/v2/analytics/operations/dashboard",
        query_string={"tenant_slug": tenant.slug, "days": 7}, headers=headers)
    assert home.status_code == reports.status_code == 200
    payload = home.get_json()
    summary = payload["summary"]
    assert summary["total"] == 7
    assert summary["open"] == 6
    assert summary["sla_risk"] == 2
    assert summary["sla_breached"] == summary["sla_at_risk"] == 1
    assert summary["sla_known"] == 3
    assert summary["sla_unknown"] == 2
    assert summary["sla_eligible"] == 5
    report_sla = reports.get_json()["queue_truth"]["queue_snapshot"]["sla"]
    for home_field, report_field in (("sla_breached", "breached"), ("sla_at_risk", "at_risk"),
            ("sla_known", "known"), ("sla_unknown", "unknown"), ("sla_eligible", "eligible")):
        assert summary[home_field] == report_sla[report_field]
    items = {item["id"]: item for item in payload["items"]}
    assert items[tickets[0].id]["sla_status"] == "unknown"
    assert items[tickets[1].id]["sla_status"] == "overdue"
    assert items[tickets[2].id]["sla_status"] == "ok"
    assert items[tickets[3].id]["sla_status"] == "risk"
    assert items[tickets[4].id]["sla_status"] == "unknown"
    assert items[tickets[5].id]["sla_status"] == "not_eligible"
    assert len(items) == 6  # Closed and foreign cases are excluded from the active list.


@pytest.mark.parametrize("kind", ["municipio", "pyme"])
def test_home_unknown_sla_backlog_never_becomes_overdue_recommendation(client, kind):
    owner, tenant = _sla_contract_tenant(kind, "unknown")
    old = datetime.now(timezone.utc) - timedelta(days=60)
    db.session.add_all([_sla_contract_ticket(kind, owner, tenant, 1000 + number,
        created=old, metadata={}) for number in range(55)])
    db.session.commit()
    response = client.get("/api/v2/backoffice/operations/inbox-summary",
        query_string={"tenant_slug": tenant.slug, "scope": kind}, headers=_auth_headers(owner))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["open"] == 55
    assert payload["summary"]["sla_risk"] == payload["summary"]["sla_breached"] == payload["summary"]["sla_at_risk"] == 0
    assert payload["summary"]["sla_known"] == 0
    assert payload["summary"]["sla_unknown"] == payload["summary"]["sla_eligible"] == 55
    assert not any(view["id"] == "sla_risk" for view in payload["recommended_views"])
    assert all(item["sla_status"] == "unknown" for item in payload["items"])
    assert {item["id"]: item["count"] for item in payload["filters"]["sla_statuses"]} == {"unknown": 55}
    assert payload["data_quality_notes"]
    executive = client.post("/api/v2/backoffice/executive-summary",
        json={"tenant_slug": tenant.slug}, headers=_auth_headers(owner))
    assert executive.status_code == 200
    summary = executive.get_json()
    assert "55 sin SLA verificable" in summary["headline"]
    assert not any(risk["id"] == "sla_risk" for risk in summary["risks"])
    assert any("sin evidencia SLA verificable" in note for note in summary["data_quality_notes"])


@pytest.mark.parametrize("kind", ["municipio", "pyme"])
def test_legacy_sla_filter_matches_evidence_scope_facets_and_pagination(client, kind):
    owner, tenant = _sla_contract_tenant(kind, "ticket-filter")
    foreign_owner, foreign_tenant = _sla_contract_tenant(kind, "ticket-filter-foreign")
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    specs = [
        (old, {}, "a", "nuevo", None),
        (now - timedelta(minutes=1), {"sla": {"due_at": (now - timedelta(hours=2)).isoformat()}}, "a", "nuevo", None),
        (now - timedelta(minutes=2), {"sla": {"due_at": (now + timedelta(hours=2)).isoformat()}}, "a", "nuevo", None),
        (old, {"sla": {"due_at": "invalid"}}, "a", "nuevo", "not-json"),
        (old, {"sla": {"state": "breached"}}, "a", "cerrado", None),
        (old, {"sla": {"state": "breached"}}, "b", "nuevo", None),
    ]
    tickets = []
    for number, (created, metadata, category, state, malformed_details) in enumerate(specs):
        ticket = _sla_contract_ticket(kind, owner, tenant, 2000 + number,
            created=created, metadata=metadata, state=state)
        ticket.categoria = category
        if kind == "municipio" and malformed_details:
            ticket.detalles = malformed_details
        tickets.append(ticket)
    foreign = _sla_contract_ticket(kind, foreign_owner, foreign_tenant, 2900,
        created=now, metadata={"sla": {"state": "breached"}})
    foreign.categoria = "a"
    employee = User(email=f"sla-{kind}-employee@example.invalid", name="Category-limited fixture",
        rol="empleado", tipo_chat=kind, es_empleado=True, tenant_id=tenant.id, tenant_slug=tenant.slug,
        municipio_id=owner.id if kind == "municipio" else None,
        pyme_id=owner.id if kind == "pyme" else None,
        accesibilidad={"employee_scope": {"categorias": ["a"]}})
    employee.set_password("local-test-fixture")
    db.session.add_all([*tickets, foreign, employee])
    db.session.commit()
    headers = {**_auth_headers(owner), "X-Tenant": tenant.slug}
    query = {"sla": "risk", "categoria": "a", "per_page": 1, "include": "compact"}
    first = client.get("/api/tickets", query_string=query, headers=headers)
    second = client.get("/api/tickets", query_string={**query, "page": 2}, headers=headers)
    assert first.status_code == second.status_code == 200
    one, two = first.get_json(), second.get_json()
    assert one["pagination"]["total_items"] == two["pagination"]["total_items"] == 2
    assert one["tickets"][0]["id"] == tickets[1].id
    assert one["tickets"][0]["sla_status"] == "vencido"
    assert two["tickets"][0]["id"] == tickets[2].id
    assert two["tickets"][0]["sla_status"] == "por_vencer"
    assert one["facets"]["sla"] == one["facets"]["slaStatuses"]
    sla_facets = {item["value"]: item["count"] for item in one["facets"]["sla"]}
    assert sla_facets["risk"] == sla_facets["unknown"] == 2
    assert sla_facets["vencido"] == sla_facets["por_vencer"] == sla_facets["resuelto"] == 1
    unknown = client.get("/api/tickets", query_string={**query, "sla": "unknown", "per_page": 0}, headers=headers)
    assert unknown.status_code == 200
    assert {item["id"] for item in unknown.get_json()["tickets"]} == {tickets[0].id, tickets[3].id}
    assert all(item["sla_status"] == "unknown" for item in unknown.get_json()["tickets"])
    employee_headers = {**_auth_headers(employee), "X-Tenant": tenant.slug}
    allowed = client.get("/api/tickets", query_string={"sla": "risk", "include": "compact"}, headers=employee_headers)
    denied_category = client.get("/api/tickets", query_string={**query, "categoria": "b"}, headers=employee_headers)
    assert allowed.status_code == denied_category.status_code == 200
    assert allowed.get_json()["pagination"]["total_items"] == 2
    assert denied_category.get_json()["pagination"]["total_items"] == 0
    assert denied_category.get_json()["tickets"] == []
    foreign_scope = client.get("/api/tickets", query_string=query,
        headers={**_auth_headers(owner), "X-Tenant": foreign_tenant.slug})
    assert foreign_scope.status_code == 403
    implicit = client.get("/api/tickets", query_string=query, headers=_auth_headers(owner))
    by_id = client.get("/api/tickets", query_string={**query, "tenant_id": tenant.id}, headers=_auth_headers(owner))
    assert implicit.status_code == by_id.status_code == 200
    assert implicit.get_json()["pagination"]["total_items"] == by_id.get_json()["pagination"]["total_items"] == 2
    unknown_scope = client.get("/api/tickets", query_string=query,
        headers={**_auth_headers(owner), "X-Tenant": "unknown-organization"})
    contradictory = client.get("/api/tickets", query_string={**query, "tenant_slug": foreign_tenant.slug}, headers=headers)
    duplicate = client.get("/api/tickets", query_string=[*query.items(),
        ("tenant_slug", tenant.slug), ("tenant_slug", foreign_tenant.slug)], headers=_auth_headers(owner))
    assert unknown_scope.status_code == contradictory.status_code == duplicate.status_code == 400
    assert unknown_scope.get_json()["reason_code"] == "invalid_tenant_selector"
    employee.tenant_id, employee.tenant_slug = None, None
    db.session.commit()
    legacy_employee = client.get("/api/tickets", query_string={"sla": "risk", "include": "compact"}, headers=_auth_headers(employee))
    assert legacy_employee.status_code == 200
    assert legacy_employee.get_json()["pagination"]["total_items"] == 2
    owner.tenant_slug = foreign_tenant.slug
    db.session.commit()
    contradictory_membership = client.get("/api/tickets", query_string=query, headers=headers)
    assert contradictory_membership.status_code == 403


def test_legacy_sla_filter_large_id_scope_is_single_projection_and_not_page_truncated(client):
    owner, tenant = _sla_contract_tenant("municipio", "bulk-filter")
    foreign_owner, foreign_tenant = _sla_contract_tenant("municipio", "bulk-filter-foreign")
    now = datetime.now(timezone.utc)
    tickets = [_sla_contract_ticket("municipio", owner, tenant, 3000 + number,
        created=now - timedelta(minutes=number + 1),
        metadata={"sla": {"due_at": (now - timedelta(hours=2)).isoformat()}}) for number in range(1005)]
    for ticket in tickets:
        ticket.categoria = "a"
    unknown = _sla_contract_ticket("municipio", owner, tenant, 5001,
        created=now - timedelta(days=60), metadata={})
    unknown.categoria = "a"
    other_category = _sla_contract_ticket("municipio", owner, tenant, 5002,
        created=now, metadata={"sla": {"state": "breached"}})
    other_category.categoria = "b"
    foreign = _sla_contract_ticket("municipio", foreign_owner, foreign_tenant, 5003,
        created=now, metadata={"sla": {"state": "breached"}})
    foreign.categoria = "a"
    db.session.add_all([*tickets, unknown, other_category, foreign])
    db.session.commit()
    projection_columns, bind_counts = [], []
    def inspect_sql(_connection, _cursor, statement, parameters, _context, _many):
        sql = statement.strip().lower()
        if sql.startswith("select municipio_ticket.id as municipio_ticket_id, municipio_ticket.estado"):
            projection_columns.append(sql.split("\nfrom", 1)[0])
        bind_counts.append(len(parameters))
    event.listen(db.engine, "before_cursor_execute", inspect_sql)
    try:
        response = client.get("/api/tickets", query_string={"sla": "risk", "categoria": "a",
            "page": 1001, "per_page": 1, "include": "compact"},
            headers={**_auth_headers(owner), "X-Tenant": tenant.slug})
    finally:
        event.remove(db.engine, "before_cursor_execute", inspect_sql)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["pagination"]["total_items"] == payload["pagination"]["total_pages"] == 1005
    assert payload["tickets"][0]["id"] == tickets[1000].id
    assert len(projection_columns) == 1
    assert "municipio_ticket.datos_extra" in projection_columns[0]
    assert "municipio_ticket.detalles" in projection_columns[0]
    assert "nombre_vecino" not in projection_columns[0] and "pregunta" not in projection_columns[0]
    assert max(bind_counts) < 100  # IDs use validated integer literal chunks, not >1,000 binds.


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


def test_backoffice_v2_orders_summary_does_not_mix_tenants_for_shared_owner(client):
    from routes.backoffice import _orders_for_tenant

    owner = User(email="shared-orders-owner@test.com", name="Shared owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="shared-orders-a",
        nombre="Shared orders A",
        tipo="pyme",
        pyme_id=owner.id,
        plan="pro",
    )
    other_tenant = TenantProfile(
        slug="shared-orders-b",
        nombre="Shared orders B",
        tipo="pyme",
        pyme_id=owner.id,
        plan="pro",
    )
    db.session.add_all([tenant, other_tenant])
    db.session.flush()
    owner.tenant_id = tenant.id
    administrator = User(
        email="shared-orders-admin@example.invalid", name="Scoped administrator",
        rol="admin", tipo_chat="pyme", tenant_id=tenant.id, tenant_slug=tenant.slug,
    )
    administrator.set_password("pw")
    db.session.add(administrator)
    local_order = PymePedido(
        pyme_id=owner.id,
        tenant_id=tenant.id,
        asunto="Pedido tenant A",
        detalles="[]",
        monto_total=Decimal("10.25"),
    )
    foreign_order = PymePedido(
        pyme_id=owner.id,
        tenant_id=other_tenant.id,
        asunto="Pedido tenant B",
        detalles="[]",
        monto_total=Decimal("999.99"),
    )
    legacy_order = PymePedido(
        pyme_id=owner.id,
        tenant_id=None,
        asunto="Pedido legacy sin tenant",
        detalles="[]",
        monto_total=Decimal("5.00"),
    )
    db.session.add_all([local_order, foreign_order, legacy_order])
    db.session.commit()

    response = client.get(
        "/api/v2/backoffice/orders/summary",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(administrator),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["total"] == 1
    assert {item["number"] for item in payload["active_orders"]} == {local_order.nro_pedido}
    assert {order.id for order in _orders_for_tenant(other_tenant)} == {foreign_order.id}
    assert legacy_order.id not in {
        order.id
        for profile in (tenant, other_tenant)
        for order in _orders_for_tenant(profile)
    }
    ambiguous_owner_response = client.get(
        "/api/v2/backoffice/orders/summary",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )
    assert ambiguous_owner_response.status_code == 403
    assert ambiguous_owner_response.get_json()["reason_code"] == "tenant_forbidden"


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
        es_empleado=True,
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
    assert payload["summary"]["active_employees"] == 1
    assert {item["id"] for item in payload["employees"]} == {employee.id}
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


def _backoffice_test_user(**fields):
    user = User(**fields)
    user.set_password("pw")
    return user


def _backoffice_membership_fixture(kind="municipio"):
    owner = _backoffice_test_user(email="team-owner@example.invalid", name="Owner", rol="admin", tipo_chat=kind)
    foreign_owner = _backoffice_test_user(email="other-team-owner@example.invalid", name="Foreign owner", rol="admin", tipo_chat=kind)
    db.session.add_all([owner, foreign_owner])
    db.session.flush()
    owner_field = "municipio_id" if kind == "municipio" else "pyme_id"
    tenant = TenantProfile(
        slug="team-membership-own", nombre="Own", tipo=kind, plan="enterprise",
        **{owner_field: owner.id},
    )
    other = TenantProfile(
        slug="team-membership-foreign", nombre="Foreign", tipo=kind, plan="enterprise",
        **{owner_field: foreign_owner.id},
    )
    db.session.add_all([tenant, other])
    db.session.flush()
    owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
    foreign_owner.tenant_id, foreign_owner.tenant_slug = other.id, other.slug
    return owner, tenant, foreign_owner, other


@pytest.mark.parametrize("kind", ["municipio", "pyme"])
def test_backoffice_team_counts_only_consistent_enabled_staff_and_preserves_customer_contacts(client, kind):
    from routes.backoffice import _collect_contacts

    owner, tenant, foreign_owner, other = _backoffice_membership_fixture(kind)

    def add_user(label, **fields):
        user = _backoffice_test_user(email=f"{label}@example.invalid", name=label, tipo_chat=kind, **fields)
        db.session.add(user)
        return user

    direct_employee = add_user("direct-employee", rol="empleado", es_empleado=True, tenant_id=tenant.id)
    role_employee = add_user("role-employee", rol="agent", tenant_id=tenant.id)
    flagged_employee = add_user("flagged-employee", rol="usuario", es_empleado=True, tenant_id=tenant.id)
    legacy_employee = add_user("legacy-employee", rol="empleado", empresa_id=owner.id)
    customer = add_user("own-customer", rol="usuario", tenant_id=tenant.id)
    administrator = add_user("own-administrator", rol="admin_municipio", tenant_id=tenant.id)
    legacy_customer = add_user("legacy-customer", rol="usuario", **{
        "municipio_id" if kind == "municipio" else "pyme_id": owner.id,
    })
    disabled = add_user("disabled-employee", rol="empleado", es_empleado=True, tenant_id=tenant.id,
                        accesibilidad={"auth": {"disabled": True}})
    clerk_disabled = add_user("clerk-disabled-employee", rol="empleado", es_empleado=True, tenant_id=tenant.id,
                              accesibilidad={"auth": {"clerk": {"disabled": True}}})
    foreign_employee = add_user("foreign-employee", rol="empleado", es_empleado=True,
                                tenant_id=other.id, empresa_id=owner.id)
    foreign_customer = add_user("foreign-customer", rol="usuario", tenant_id=other.id,
                                **{"municipio_id" if kind == "municipio" else "pyme_id": owner.id})
    conflicting_slug = add_user("conflicting-slug", rol="empleado", tenant_id=tenant.id, tenant_slug=other.slug)
    conflicting_owner = add_user("conflicting-owner", rol="empleado", tenant_id=tenant.id, empresa_id=foreign_owner.id)
    db.session.commit()

    headers = _auth_headers(owner)
    response = client.get("/api/v2/backoffice/team/coverage-summary",
                          query_string={"tenant_slug": tenant.slug}, headers=headers)
    assert response.status_code == 200
    payload = response.get_json()
    expected_staff = {direct_employee.id, role_employee.id, flagged_employee.id, legacy_employee.id}
    assert payload["summary"]["active_employees"] == len(expected_staff)
    assert {item["id"] for item in payload["employees"]} == expected_staff
    assert {item["id"] for item in payload["workload_by_agent"]} == expected_staff

    contacts_response = client.get("/api/v2/backoffice/contacts/summary",
                                   query_string={"tenant_slug": tenant.slug}, headers=headers)
    assert contacts_response.status_code == 200
    expected_contacts = [owner, direct_employee, role_employee, flagged_employee, legacy_employee,
                         customer, administrator, legacy_customer, disabled, clerk_disabled]
    assert contacts_response.get_json()["summary"]["total"] == len(expected_contacts)
    contacts = _collect_contacts(tenant, owner)
    assert {item["email"] for item in contacts} == {user.email for user in expected_contacts}
    assert all(item["sources"] == ["user"] and item["records"] == 1 for item in contacts)
    assert {foreign_employee.email, foreign_customer.email, conflicting_slug.email, conflicting_owner.email}.isdisjoint(
        {item["email"] for item in contacts}
    )


def test_backoffice_team_metric_matches_actual_admin_employee_endpoint_for_native_staff(client):
    owner, tenant, _, _ = _backoffice_membership_fixture()
    employee = _backoffice_test_user(email="actual-staff@example.invalid", name="Staff", rol="empleado", es_empleado=True,
                    tipo_chat="municipio", tenant_id=tenant.id)
    db.session.add(employee)
    db.session.add_all([
        _backoffice_test_user(email=f"actual-customer-{index}@example.invalid", name="Customer", rol="usuario",
             tipo_chat="municipio", tenant_id=tenant.id)
        for index in range(50)
    ])
    db.session.commit()
    headers = {**_auth_headers(owner), "X-Tenant-Slug": tenant.slug}
    actual = client.get("/api/admin/employees", headers=headers)
    summary = client.get("/api/v2/backoffice/team/coverage-summary", headers=headers)
    assert actual.status_code == summary.status_code == 200
    assert {item["id"] for item in actual.get_json()} == {employee.id}
    assert summary.get_json()["summary"]["active_employees"] == 1
    assert {item["id"] for item in summary.get_json()["employees"]} == {employee.id}


def test_backoffice_team_does_not_claim_coverage_from_disabled_or_foreign_assignees(client):
    owner, tenant, _, other = _backoffice_membership_fixture()
    disabled = _backoffice_test_user(email="disabled-assignee@example.invalid", name="Disabled", rol="empleado", es_empleado=True,
                    tipo_chat="municipio", tenant_id=tenant.id, accesibilidad={"auth": {"disabled": True}})
    foreign = _backoffice_test_user(email="foreign-assignee@example.invalid", name="Foreign", rol="empleado", es_empleado=True,
                   tipo_chat="municipio", tenant_id=other.id, empresa_id=owner.id)
    db.session.add_all([disabled, foreign])
    db.session.flush()
    db.session.add_all([
        MunicipioTicket(tenant_id=tenant.id, municipio_id=owner.id, pregunta="Own case", estado="nuevo",
                        categoria=category, asignado_a_id=user.id)
        for category, user in (("disabled-category", disabled), ("foreign-category", foreign))
    ])
    db.session.commit()
    response = client.get("/api/v2/backoffice/team/coverage-summary",
                          query_string={"tenant_slug": tenant.slug}, headers=_auth_headers(owner))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"]["active_employees"] == 0
    assert payload["summary"]["covered_categories"] == payload["summary"]["covered_channels"] == 0
    assert {item["label"] for item in payload["categories_without_owner"]} == {"disabled-category", "foreign-category"}


def test_backoffice_ambiguous_legacy_staff_and_contacts_remain_quarantined(client):
    owner, tenant, _, _ = _backoffice_membership_fixture()
    duplicate = TenantProfile(slug="same-owner-second-tenant", nombre="Duplicate owner", tipo="municipio",
                              municipio_id=owner.id, plan="enterprise")
    actor = _backoffice_test_user(email="explicit-team-admin@example.invalid", name="Admin", rol="admin", tipo_chat="municipio",
                 tenant_id=tenant.id, tenant_slug=tenant.slug)
    direct = _backoffice_test_user(email="explicit-team-staff@example.invalid", name="Direct", rol="empleado", es_empleado=True,
                  tipo_chat="municipio", tenant_id=tenant.id)
    legacy = _backoffice_test_user(email="ambiguous-team-staff@example.invalid", name="Legacy", rol="empleado", es_empleado=True,
                  tipo_chat="municipio", empresa_id=owner.id)
    db.session.add_all([duplicate, actor, direct, legacy])
    db.session.commit()
    headers = _auth_headers(actor)
    team = client.get("/api/v2/backoffice/team/coverage-summary", query_string={"tenant_slug": tenant.slug}, headers=headers)
    contacts = client.get("/api/v2/backoffice/contacts/summary", query_string={"tenant_slug": tenant.slug}, headers=headers)
    assert team.status_code == contacts.status_code == 200
    assert {item["id"] for item in team.get_json()["employees"]} == {direct.id}
    assert contacts.get_json()["summary"]["total"] == 2  # Explicit admin and staff only.


@pytest.mark.parametrize("selection,expected_status", [
    ("implicit", 200), ("own", 200), ("own_id", 200), ("foreign", 403),
    ("unknown", 404), ("conflicting", 404), ("blank", 404), ("repeated", 404), ("unknown_id", 404),
    ("own_header", 200), ("foreign_header", 403), ("unknown_header", 404), ("conflicting_header", 404),
])
def test_backoffice_selectors_never_fall_back_after_explicit_denial(client, selection, expected_status):
    owner, tenant, _, other = _backoffice_membership_fixture("pyme")
    db.session.commit()
    query = {
        "implicit": {}, "own": {"tenant_slug": tenant.slug}, "own_id": {"tenant_id": tenant.id},
        "foreign": {"tenant_slug": other.slug}, "unknown": {"tenant_slug": "missing-team-tenant"},
        "conflicting": {"tenant_slug": tenant.slug, "tenant": other.slug}, "blank": {"tenant_slug": " "},
        "repeated": [("tenant_slug", tenant.slug), ("tenant_slug", other.slug)],
        "unknown_id": {"tenant_id": 987654321},
        "own_header": {}, "foreign_header": {}, "unknown_header": {},
        "conflicting_header": {"tenant_slug": tenant.slug},
    }[selection]
    headers = _auth_headers(owner)
    if selection.endswith("_header"):
        headers["X-Tenant-Slug"] = {
            "own_header": tenant.slug, "foreign_header": other.slug,
            "unknown_header": "missing-team-tenant", "conflicting_header": other.slug,
        }[selection]
    for endpoint in (
        "/api/app/backoffice/navigation", "/api/app/backoffice/summary",
        "/api/v2/backoffice/orders/summary", "/api/v2/backoffice/contacts/summary",
        "/api/v2/backoffice/team/coverage-summary", "/api/v2/backoffice/operations/inbox-summary",
    ):
        response = client.get(endpoint, query_string=query, headers=headers)
        assert response.status_code == expected_status, (endpoint, response.get_json())
        if expected_status == 200:
            assert response.get_json()["tenant_slug"] == tenant.slug
        else:
            assert "summary" not in response.get_json()


@pytest.mark.parametrize("conflict", ["slug", "owner", "legacy_owner"])
def test_backoffice_membership_conflict_cannot_be_overridden_by_selected_tenant(client, conflict):
    _, tenant, foreign_owner, other = _backoffice_membership_fixture()
    actor = _backoffice_test_user(email="conflicting-admin@example.invalid", name="Conflict", rol="admin", tipo_chat="municipio",
                 tenant_id=tenant.id, tenant_slug=tenant.slug)
    if conflict == "slug":
        actor.tenant_slug = other.slug
    elif conflict == "owner":
        actor.empresa_id = foreign_owner.id
    else:
        actor.municipio_id = foreign_owner.id
    db.session.add(actor)
    db.session.commit()
    for endpoint in ("/api/app/backoffice/navigation", "/api/v2/backoffice/team/coverage-summary",
                     "/api/v2/backoffice/contacts/summary"):
        response = client.get(endpoint, query_string={"tenant_slug": tenant.slug}, headers=_auth_headers(actor))
        assert response.status_code == 403
        assert "summary" not in response.get_json()


@pytest.mark.parametrize("body_selector,expected_status", [
    ("unknown", 404), ("foreign", 403), ("contradictory", 404), ("blank", 404), ("null", 404), ("list", 404),
])
def test_backoffice_mutation_selector_denial_precedes_export_or_summary_effects(client, monkeypatch, body_selector, expected_status):
    owner, tenant, _, other = _backoffice_membership_fixture()
    db.session.commit()
    calls = []
    monkeypatch.setattr("routes.backoffice._export_dir", lambda: calls.append("directory"))
    monkeypatch.setattr("routes.backoffice._executive_summary_payload", lambda *args: calls.append("summary"))
    slug = {
        "unknown": "missing-team-tenant", "foreign": other.slug, "contradictory": other.slug,
        "blank": " ", "null": None, "list": [tenant.slug],
    }[body_selector]
    query = {"tenant_slug": tenant.slug} if body_selector == "contradictory" else {}
    for endpoint, body in (
        ("/api/v2/backoffice/export", {"tenant_slug": slug, "resource": "contacts", "format": "csv"}),
        ("/api/v2/backoffice/executive-summary", {"tenant_slug": slug}),
    ):
        response = client.post(endpoint, query_string=query, json=body, headers=_auth_headers(owner))
        assert response.status_code == expected_status
    assert calls == []


@pytest.mark.parametrize("association,visible", [
    ("foreign_explicit", False), ("foreign_slug", False), ("conflicting_legacy", False),
    ("ambiguous_legacy", False), ("own_legacy", True), ("own_staff", True), ("own_inactive_admin", True),
])
def test_backoffice_inbox_assignee_badge_requires_consistent_membership_without_erasing_own_history(
    client, association, visible,
):
    owner, tenant, foreign_owner, other = _backoffice_membership_fixture()
    actor = _backoffice_test_user(email="inbox-scoped-admin@example.invalid", name="Scoped admin", rol="admin",
                                 tipo_chat="municipio", tenant_id=tenant.id, tenant_slug=tenant.slug)
    fields = {"rol": "empleado", "es_empleado": True, "tenant_id": tenant.id}
    if association == "foreign_explicit":
        fields.update(tenant_id=other.id, empresa_id=owner.id)
    elif association == "foreign_slug":
        fields.update(tenant_slug=other.slug)
    elif association == "conflicting_legacy":
        fields.update(tenant_id=None, empresa_id=owner.id, municipio_id=foreign_owner.id)
    elif association == "ambiguous_legacy":
        fields.update(tenant_id=None, empresa_id=owner.id)
        db.session.add(TenantProfile(slug="inbox-duplicate-owner", nombre="Second organization", tipo="municipio",
                                     municipio_id=owner.id, plan="enterprise"))
    elif association == "own_legacy":
        fields.update(tenant_id=None, empresa_id=owner.id)
    elif association == "own_inactive_admin":
        fields.update(rol="admin_municipio", es_empleado=False, accesibilidad={"auth": {"disabled": True}})
    assignee = _backoffice_test_user(email="assignee-private@example.invalid", name="Private assignee name",
                                    tipo_chat="municipio", **fields)
    db.session.add_all([actor, assignee])
    db.session.flush()
    ticket = MunicipioTicket(tenant_id=tenant.id, municipio_id=owner.id, pregunta="Own case", categoria="atencion",
                             estado="nuevo", asignado_a_id=assignee.id)
    db.session.add(ticket)
    db.session.commit()
    response = client.get("/api/v2/backoffice/operations/inbox-summary",
                          query_string={"tenant_slug": tenant.slug}, headers=_auth_headers(actor))
    assert response.status_code == 200
    payload = response.get_json()
    assert {item["id"] for item in payload["items"]} == {ticket.id}
    badge = payload["items"][0]["assigned_agent"]
    if visible:
        assert badge["id"] == assignee.id
        assert badge["email"] == assignee.email
        assert badge["label"] == assignee.name
    else:
        assert badge is None
        assert assignee.id not in {item["id"] for item in payload["filters"]["agents"]}
        assert assignee.email not in response.get_data(as_text=True)
        assert assignee.name not in response.get_data(as_text=True)
    assert db.session.get(MunicipioTicket, ticket.id).asignado_a_id == assignee.id
