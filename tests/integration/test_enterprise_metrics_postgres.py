"""Real PostgreSQL contracts, collected only by the dedicated owned-CI runner.

All people, organizations, amounts and identities below are synthetic. The
global SQLite fixtures are deliberately excluded by the runner's --noconftest.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import secrets
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.run_auth_session_lifecycle_postgres import (
    FIXTURE_DATABASE, FIXTURE_USER, FixtureRefused, verify_empty_fixture,
)
from tests.run_enterprise_metrics_postgres import OPT_IN, business_source_hashes


pytestmark = pytest.mark.skipif(os.environ.get(OPT_IN) != "1",
                                reason="Dedicated owned PostgreSQL CI runner required")


def _drop_owned_schema(engine, schema, created_oid, created_owner):
    from sqlalchemy import text
    from sqlalchemy.schema import DropSchema
    if not re.fullmatch(r"enterprise_metrics_[a-f0-9]{32}", schema):
        raise FixtureRefused("metrics_fixture_schema_name_invalid")
    with engine.begin() as connection:
        actual = connection.execute(text(
            "SELECT oid, nspowner FROM pg_catalog.pg_namespace WHERE nspname=:schema"
        ), {"schema": schema}).one_or_none()
        if actual is None or tuple(actual) != (created_oid, created_owner):
            raise FixtureRefused("metrics_fixture_schema_ownership_lost")
        connection.execute(DropSchema(schema, cascade=True))


@pytest.fixture(scope="module")
def pg_metrics_app(enterprise_metrics_configuration, record_testsuite_property):
    """Use one invocation-owned schema; never the global app/client fixtures."""
    configuration = dict(enterprise_metrics_configuration)
    if (configuration.get("host") != "127.0.0.1" or configuration.get("port") != 5432
            or configuration.get("dbname") != FIXTURE_DATABASE
            or configuration.get("user") != FIXTURE_USER):
        raise FixtureRefused("metrics_fixture_configuration_mismatch")
    import psycopg
    with psycopg.connect(**configuration, connect_timeout=3) as connection:
        verify_empty_fixture(connection)
        observed_server_version = int(connection.execute("SHOW server_version_num").fetchone()[0])
        if not 180000 <= observed_server_version < 190000:
            raise FixtureRefused("fixture_postgres18_required")

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL
    from sqlalchemy.schema import CreateSchema
    from app import create_app
    from config import TestingConfig
    from models import db

    url = URL.create("postgresql+psycopg", username=configuration["user"],
                     password=configuration["password"], host=configuration["host"],
                     port=configuration["port"], database=configuration["dbname"])
    engine_options = {
        "connect_args": {"connect_timeout": 3,
                         "options": "-c statement_timeout=5000 -c lock_timeout=1500"},
        "hide_parameters": True,
    }
    root = create_engine(url, **engine_options)
    schema = "enterprise_metrics_" + uuid4().hex
    created_oid = created_owner = None
    app = None
    try:
        assert root.dialect.name == "postgresql" and root.dialect.driver == "psycopg"
        with root.begin() as connection:
            if connection.execute(text("SELECT to_regnamespace(:schema)"), {"schema": schema}).scalar() is not None:
                raise FixtureRefused("metrics_fixture_schema_already_exists")
            connection.execute(CreateSchema(schema))
            created_oid, created_owner = connection.execute(text(
                "SELECT oid, nspowner FROM pg_catalog.pg_namespace WHERE nspname=:schema"
            ), {"schema": schema}).one()

        class MetricsPostgresConfig(TestingConfig):
            SQLALCHEMY_DATABASE_URI = url
            # Replace the complete dictionary: TestingConfig has SQLite-only
            # connect_args.timeout, which must never reach psycopg.
            SQLALCHEMY_ENGINE_OPTIONS = {
                **engine_options, "execution_options": {"schema_translate_map": {None: schema}},
            }
            SECRET_KEY = secrets.token_hex(32)
            SESSION_TYPE = "null"
            SESSION_COOKIE_SAMESITE = "Lax"
            CUTOVER_WRITER_FENCE_ENABLED = False
            RATELIMIT_STORAGE_URI = "memory://"
            OUTBOUND_NOTIFICATIONS_ENABLED = False

        # create_app creates testing tables: all DDL is inside our owned schema.
        app = create_app(MetricsPostgresConfig)
        with app.app_context():
            assert db.engine.dialect.name == "postgresql"
            assert db.engine.dialect.driver == "psycopg"
            database, actor = db.session.execute(text("SELECT current_database(), current_user")).one()
            if (database, actor) != (FIXTURE_DATABASE, FIXTURE_USER):
                raise FixtureRefused("metrics_fixture_identity_changed")
            db.session.remove()
        for key, value in {
            "enterprise_metrics.database_dialect": "postgresql",
            "enterprise_metrics.fixture_database": FIXTURE_DATABASE,
            "enterprise_metrics.server_major": str(observed_server_version // 10000),
            "enterprise_metrics.server_version_num": str(observed_server_version),
            "enterprise_metrics.business_candidate": "municipal-tenant-closure-v3",
            "enterprise_metrics.business_source_hashes": json.dumps(business_source_hashes(), sort_keys=True),
            "enterprise_metrics.owned_schema": "true",
        }.items():
            record_testsuite_property(key, value)
        yield SimpleNamespace(app=app, db=db, root=root, schema=schema)
    finally:
        try:
            if app is not None:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()
            if created_oid is not None:
                _drop_owned_schema(root, schema, created_oid, created_owner)
        finally:
            root.dispose()
        with psycopg.connect(**configuration, connect_timeout=3) as connection:
            verify_empty_fixture(connection)
        record_testsuite_property("enterprise_metrics.fixture_cleanup_verified", "true")


def _owner_and_tenant(db, label):
    from models import TenantProfile, User
    suffix = label + "-" + uuid4().hex
    owner = User(name="Synthetic metrics account", email=suffix + "@example.invalid",
                 rol="admin", tipo_chat="pyme")
    owner.set_password(secrets.token_urlsafe(24))
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug=suffix, nombre="Synthetic metrics organization",
                           tipo="pyme", pyme_id=owner.id, plan="enterprise", is_active=True)
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug, owner.pyme_id = tenant.id, tenant.slug, owner.id
    return owner, tenant


_DEFAULT_CUSTOMER = object()


def _order(db, owner, tenant_id, amount, currency="ARS", *, customer=_DEFAULT_CUSTOMER):
    from models import PymePedido
    row = PymePedido(pyme_id=owner.id, tenant_id=tenant_id, asunto="Synthetic metrics fixture",
                     detalles="[]", monto_total=amount, moneda=currency,
                     user_id=owner.id if customer is _DEFAULT_CUSTOMER else customer)
    db.session.add(row)
    return row


def _headers(owner, tenant):
    from services.auth_session_lifecycle import issue_token
    claims = {"user_id": owner.id, "rol": owner.rol, "tipo_chat": owner.tipo_chat,
              "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    if owner.rol == "super_admin":
        from utils.auth_helpers import auth_session_version
        claims.update(auth_provider="clerk", session_kind="clerk",
                      clerk_sid=f"synthetic-metrics-session-{owner.id}",
                      sv=auth_session_version(owner))
    token = issue_token(claims)
    return {"Authorization": "Bearer " + token, "X-Tenant-Slug": tenant.slug}


def _get(runtime, owner, tenant, path="/api/metrics/summary", *, query=None, headers=None):
    with runtime.app.test_client() as client:
        response = client.get(path, query_string=query, headers=headers or _headers(owner, tenant))
    return response


def _grant(db, actor, tenant):
    from models import Role, UserRole
    role = Role.query.filter_by(name="admin").first()
    if role is None:
        role = Role(name="admin")
        db.session.add(role)
        db.session.flush()
    db.session.add(UserRole(user_id=actor.id, role_id=role.id, tenant_id=tenant.id))


def test_postgres_orm_api_counts_distinct_and_null_identity(pg_metrics_app):
    from services.metricas_service import MetricasService
    runtime = pg_metrics_app
    with runtime.app.app_context():
        owner, tenant = _owner_and_tenant(runtime.db, "counts")
        _order(runtime.db, owner, tenant.id, 100)
        _order(runtime.db, owner, tenant.id, 50)
        _order(runtime.db, owner, tenant.id, 25, customer=None)
        runtime.db.session.commit()
        direct = MetricasService(owner.id, tenant_id=tenant.id).get_summary()
        response = _get(runtime, owner, tenant)
        assert response.status_code == 200
        payload = response.get_json()
        for data in (direct, payload):
            assert data["total_orders"] == 3
            assert data["unique_customers"] == 1
            assert data["total_sales"] == 175
            assert data["avg_order_value"] == pytest.approx(175 / 3)
            assert data["availability"]["unique_customers"]["definition"] == "distinct_order_user_ids"
            assert data["scope"]["tenant_id"] == tenant.id


def test_postgres_currency_normalization_and_mixed_unknown_disclosure(pg_metrics_app):
    runtime = pg_metrics_app
    with runtime.app.app_context():
        owner, tenant = _owner_and_tenant(runtime.db, "currency")
        _order(runtime.db, owner, tenant.id, 100)
        changing = _order(runtime.db, owner, tenant.id, 50, "ars")
        runtime.db.session.commit()
        for currency in ("ars", " ARS ", "USD", None):
            changing.moneda = currency
            runtime.db.session.commit()
            response = _get(runtime, owner, tenant)
            assert response.status_code == 200
            data = response.get_json()
            assert data["total_orders"] == 2
            if currency in ("ars", " ARS "):
                assert data["total_sales"] == 150 and data["currency"] == "ARS"
            else:
                assert data["total_sales"] is None and data["avg_order_value"] is None
                assert data["availability"]["total_sales"]["reason_code"] == "mixed_or_unknown_currency"


        # Three SQL-distinct values must never collapse after bounded LIMIT 2.
        padded_owner, padded_tenant = _owner_and_tenant(runtime.db, "currency-tab-third")
        for amount, currency in ((100, "ARS"), (50, "ARS\t"), (1000, "USD")):
            _order(runtime.db, padded_owner, padded_tenant.id, amount, currency)
        unknown_owner, unknown_tenant = _owner_and_tenant(runtime.db, "currency-unknown")
        _order(runtime.db, unknown_owner, unknown_tenant.id, 100, "ZZZ")
        runtime.db.session.commit()
        for actor, selected, count in ((padded_owner, padded_tenant, 3), (unknown_owner, unknown_tenant, 1)):
            response = _get(runtime, actor, selected)
            assert response.status_code == 200
            data = response.get_json()
            assert data["total_orders"] == count
            assert data["total_sales"] is None and data["avg_order_value"] is None
            assert data["currency"] is None
            assert data["availability"]["total_sales"]["reason_code"] == "mixed_or_unknown_currency"


def test_postgres_missing_nonfinite_and_unsafe_amounts_are_unavailable(pg_metrics_app):
    runtime = pg_metrics_app
    with runtime.app.app_context():
        owner, tenant = _owner_and_tenant(runtime.db, "amounts")
        changing = _order(runtime.db, owner, tenant.id, None)
        runtime.db.session.commit()
        for amount, reason in ((None, "missing_order_amount"),
                               (float("nan"), "invalid_order_amount"),
                               (float("inf"), "invalid_order_amount"),
                               (9007199254740992, "unsafe_order_amount")):
            changing.monto_total = amount
            runtime.db.session.commit()
            response = _get(runtime, owner, tenant, "/api/metrics/kpis")
            assert response.status_code == 200
            data = response.get_json()
            assert data["total_orders"] == 1 and data["unique_customers"] == 1
            assert data["total_sales"] is None and data["avg_order_value"] is None
            assert data["availability"]["total_sales"]["reason_code"] == reason


def _exercise_municipal_statistics_scope(runtime):
    """Exercise selected-scope statistics and legacy attribution on real PG18."""
    from contextlib import contextmanager
    from sqlalchemy import event
    from models import (
        MunicipioTicket, Role, SugerenciaCiudadano, TenantProfile,
        TicketSatisfaccion, User, UserRole,
    )
    from utils.auth_helpers import admin_surface_access_allowed
    from utils.tenant_admin_access import resolve_consistent_user_tenant

    database = runtime.db
    suffix = uuid4().hex
    allowed_category = "synthetic-municipal-allowed-" + suffix
    excluded_category = "synthetic-municipal-excluded-" + suffix
    legacy_category = "synthetic-municipal-quarantined-" + suffix
    paths = (
        "/estadisticas/dashboard",
        "/estadisticas/tickets",
        "/estadisticas/mapa_calor/datos",
    )
    request_count = 0
    expected_districts = {}
    expected_satisfaction = {}

    def user(label, role="admin", **values):
        actor = User(name="Synthetic municipal " + label,
                     email=label + "-" + uuid4().hex + "@example.invalid",
                     rol=role, tipo_chat="municipio", **values)
        actor.set_password(secrets.token_urlsafe(24))
        database.session.add(actor)
        database.session.flush()
        return actor

    def tenant(owner, label):
        selected = TenantProfile(slug=label + "-" + uuid4().hex,
                                 nombre="Synthetic municipal organization",
                                 tipo="municipio", municipio_id=owner.id,
                                 plan="enterprise", is_active=True)
        database.session.add(selected)
        database.session.flush()
        return selected

    def role(name):
        assigned = Role.query.filter_by(name=name).first()
        if assigned is None:
            assigned = Role(name=name)
            database.session.add(assigned)
            database.session.flush()
        return assigned

    def assign(actor, selected, name):
        assignment = UserRole(user_id=actor.id, tenant_id=selected.id,
                              role_id=role(name).id)
        database.session.add(assignment)
        database.session.flush()
        return assignment

    def tickets(owner, selected, category, count, latitude, longitude, *, district):
        created = []
        for _ in range(count):
            row = MunicipioTicket(
                tenant_id=selected.id if selected is not None else None,
                municipio_id=owner.id, pregunta="Synthetic municipal scope fixture",
                categoria=category, estado="nuevo", latitud=latitude,
                longitud=longitude, distrito=district,
            )
            database.session.add(row)
            created.append(row)
        return created

    def get(actor, selected, path):
        nonlocal request_count
        suggestion_selects = []
        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            lowered = statement.lower()
            if "sugerencia_ciudadano" in lowered and "select" in lowered:
                suggestion_selects.append(statement)
        event.listen(database.engine, "before_cursor_execute", observe)
        try:
            request_count += 1
            response = _get(runtime, actor, selected, path, query={"tipo": "municipio"})
            response.synthetic_suggestion_select_count = len(suggestion_selects)
            return response
        finally:
            event.remove(database.engine, "before_cursor_execute", observe)

    @contextmanager
    def no_municipal_row_read():
        reached = []
        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            lowered = statement.lower()
            if ("select" in lowered and
                    ("municipio_ticket" in lowered or "sugerencia_ciudadano" in lowered)):
                reached.append(statement)
        event.listen(database.engine, "before_cursor_execute", observe)
        try:
            yield
        finally:
            event.remove(database.engine, "before_cursor_execute", observe)
            assert not reached, "Municipal rows must not be read before a scope403"

    def denied(actor, selected, path):
        with no_municipal_row_read():
            response = get(actor, selected, path)
        assert response.status_code == 403
        assert response.get_json() == {
            "error": "tenant_forbidden",
            "reason_code": "metrics_tenant_scope_unavailable",
        }
        assert response.headers["Cache-Control"] == "no-store"

    def points(response, selected, expected, *, employee=False,
               unique_summary=False, suggestion_count=0):
        assert response.status_code == 200
        payload = response.get_json()
        observed = sorted(
            (point["categoria"], point["location"]["lat"],
             point["location"]["lng"], point["weight"])
            for point in payload["heatmap"]
        )
        assert observed == sorted(expected)
        assert legacy_category not in {point[0] for point in observed}
        total = sum(point[3] for point in expected)
        assert payload["summary"]["total"] == total
        assert payload["stats"]["resumen"]["total"] == total
        assert sorted((row["categoria"], row["total"])
                      for row in payload["stats"]["por_categoria"]) == sorted(
                          (point[0], point[3]) for point in expected
                      )
        assert [(row["distrito"], row["total"])
                for row in payload["stats"]["por_distrito"]] == [
                    (expected_districts[selected.id], total)
                ]
        responses, average = expected_satisfaction[(selected.id, employee)]
        assert payload["stats"]["satisfaccion"]["respuestas"] == responses
        assert payload["stats"]["satisfaccion"]["promedio"] == pytest.approx(average)
        assert payload["summary"]["respuestas_satisfaccion"] == responses
        assert payload["summary"]["promedio_satisfaccion"] == pytest.approx(average)
        if employee:
            assert payload["privacy"]["mode"] == "employee_aggregated"
            assert payload["privacy"]["k_min"] == 5
        if response.request.path == "/estadisticas/dashboard":
            assert payload["metadata"]["tenant_id"] == selected.id
        suggestions = payload["stats"]["sugerencias"]
        if unique_summary:
            assert suggestions["available"] is True
            assert suggestions["reason_code"] is None
            assert suggestions["total"] == suggestion_count
            assert response.synthetic_suggestion_select_count > 0
            if suggestion_count:
                assert sum(row["total"] for row in suggestions["por_estado"]) == suggestion_count
                assert sum(row["total"] for row in suggestions["por_categoria"]) == suggestion_count
                assert sum(row["total"] for row in suggestions["tendencia_mensual"]) == suggestion_count
            else:
                assert suggestions["por_estado"] == suggestions["por_categoria"] == suggestions["tendencia_mensual"] == []
        else:
            assert suggestions == {
                "total": None, "por_estado": [], "por_categoria": [], "tendencia_mensual": [],
                "available": False, "reason_code": "legacy_owner_scope_unattributable",
            }
            assert response.synthetic_suggestion_select_count == 0
        return payload

    owner = user("shared-owner")
    selected = tenant(owner, "municipal-a")
    other = tenant(owner, "municipal-b")
    owner.tenant_id, owner.tenant_slug, owner.municipio_id = (
        selected.id, selected.slug, owner.id,
    )
    admin = user("admin-a", tenant_id=selected.id, tenant_slug=selected.slug,
                 municipio_id=owner.id)
    employee = user(
        "employee-a", role="empleado", tenant_id=selected.id,
        tenant_slug=selected.slug, municipio_id=owner.id, empresa_id=None,
        es_empleado=True, ticket_categorias=allowed_category,
        accesibilidad={"employee_scope": {"categorias": [allowed_category]}},
    )
    assign(admin, selected, "admin")
    assign(employee, selected, "empleado")
    district_a, district_b = "synthetic-district-a-" + suffix, "synthetic-district-b-" + suffix
    a_allowed = tickets(owner, selected, allowed_category, 7, -33.0, -60.0, district=district_a)
    a_excluded = tickets(owner, selected, excluded_category, 5, -33.2, -60.2, district=district_a)
    b_allowed = tickets(owner, other, allowed_category, 11, -34.0, -61.0, district=district_b)
    b_excluded = tickets(owner, other, excluded_category, 6, -34.2, -61.2, district=district_b)
    tickets(owner, None, legacy_category, 13, -35.0, -62.0, district="synthetic-legacy-district")
    database.session.flush()
    for row, score in ((a_allowed[0], 1), (a_excluded[0], 1),
                       (b_allowed[0], 5), (b_excluded[0], 5)):
        database.session.add(TicketSatisfaccion(
            ticket_id=row.id, tipo="municipio", puntuacion=score,
        ))
    for _ in range(3):
        database.session.add(SugerenciaCiudadano(
            municipio_id=owner.id, texto_sugerencia="Synthetic unassigned shared-owner suggestion",
            categoria=legacy_category, estado="nueva",
        ))
    database.session.commit()
    expected_districts.update({selected.id: district_a, other.id: district_b})
    expected_satisfaction.update({
        (selected.id, False): (2, 1.0), (selected.id, True): (1, 1.0),
        (other.id, False): (2, 5.0),
    })
    assert MunicipioTicket.query.filter_by(
        municipio_id=owner.id, tenant_id=None, categoria=legacy_category,
    ).count() == 13
    assert SugerenciaCiudadano.query.filter_by(municipio_id=owner.id).count() == 3
    for actor in (admin, employee):
        assert actor.id != owner.id
        assert resolve_consistent_user_tenant(actor).id == selected.id
        assert UserRole.query.filter_by(user_id=actor.id, tenant_id=other.id).count() == 0

    expected_a = [
        (allowed_category, -33.0, -60.0, 7),
        (excluded_category, -33.2, -60.2, 5),
    ]
    expected_b = [
        (allowed_category, -34.0, -61.0, 11),
        (excluded_category, -34.2, -61.2, 6),
    ]
    for path in paths:
        denied(admin, other, path)
        denied(employee, other, path)
        points(get(admin, selected, path), selected, expected_a)
        points(get(employee, selected, path), selected, expected_a[:1], employee=True)
        # The actual owner explicitly selects B; its ticket statistics must
        # agree with B's heatmap while unbound legacy suggestions stay unknown.
        points(get(owner, other, path), other, expected_b)

    for name in ("admin", "analytics_viewer"):
        assignment = assign(admin, other, name)
        database.session.commit()
        for path in paths:
            points(get(admin, other, path), other, expected_b)
        database.session.delete(assignment)
        database.session.commit()
        for path in paths:
            denied(admin, other, path)

        assignment = assign(employee, other, name)
        database.session.commit()
        for path in paths:
            denied(employee, other, path)
            points(get(employee, selected, path), selected, expected_a[:1], employee=True)
        database.session.delete(assignment)
        database.session.commit()

    unrelated_owner = user("unrelated-owner")
    unrelated = tenant(unrelated_owner, "municipal-unrelated")
    unrelated_owner.tenant_id = unrelated.id
    unrelated_owner.tenant_slug = unrelated.slug
    foreign_assignment = assign(admin, unrelated, "admin")
    database.session.commit()
    for path in paths:
        denied(admin, unrelated, path)
    database.session.delete(foreign_assignment)
    database.session.commit()

    # This sole owner's legacy rows are attributable. Real scalar date and
    # aggregate queries run on stock PG18; no geometry stub or extension exists.
    unique_owner = user("unique-owner")
    unique = tenant(unique_owner, "municipal-legacy-unique")
    unique_owner.tenant_id, unique_owner.tenant_slug, unique_owner.municipio_id = (
        unique.id, unique.slug, unique_owner.id,
    )
    legacy = user("unique-legacy-delegate", municipio_id=unique_owner.id)
    unique_category = "synthetic-municipal-unique-" + suffix
    unique_district = "synthetic-district-unique-" + suffix
    tickets(unique_owner, unique, unique_category, 2, -36.0, -63.0, district=unique_district)
    tickets(unique_owner, None, unique_category, 3, -36.0, -63.0, district=unique_district)
    database.session.commit()
    expected_districts[unique.id] = unique_district
    expected_satisfaction[(unique.id, False)] = (0, 0.0)
    assert legacy.tenant_id is None and legacy.tenant_slug is None
    assert resolve_consistent_user_tenant(legacy).id == unique.id
    for path in paths:
        points(get(legacy, unique, path), unique,
               [(unique_category, -36.0, -63.0, 5)], unique_summary=True)

    for _ in range(2):
        database.session.add(SugerenciaCiudadano(
            municipio_id=unique_owner.id, texto_sugerencia="Synthetic attributable suggestion",
            categoria=unique_category, estado="nueva",
        ))
    database.session.commit()
    for path in paths:
        points(get(legacy, unique, path), unique,
               [(unique_category, -36.0, -63.0, 5)],
               unique_summary=True, suggestion_count=2)

    ambiguous = user("ambiguous-legacy-delegate", municipio_id=owner.id)
    database.session.commit()
    assert resolve_consistent_user_tenant(ambiguous) is None
    for path in paths:
        denied(ambiguous, other, path)

    # Legal profiles can have only a PyME owner reference. An explicit role
    # cannot let this municipal request fall back to its actor's foreign owner,
    # whose unique organization has five tickets and two actual suggestions.
    pyme_owner = user("owner-reference-p")
    pyme_owner.tipo_chat = "pyme"
    missing_owner = TenantProfile(
        slug="municipal-missing-owner-" + uuid4().hex,
        nombre="Synthetic missing municipal owner", tipo="municipio",
        municipio_id=None, pyme_id=pyme_owner.id,
        plan="enterprise", is_active=True,
    )
    database.session.add(missing_owner)
    database.session.flush()
    pyme_owner.tenant_id, pyme_owner.tenant_slug, pyme_owner.pyme_id = (
        missing_owner.id, missing_owner.slug, pyme_owner.id,
    )
    for reference, role_name in (("municipio_id", "admin"), ("empresa_id", "analytics_viewer")):
        delegate = user(
            "missing-owner-" + reference, tenant_id=missing_owner.id,
            tenant_slug=missing_owner.slug, **{reference: unique_owner.id},
        )
        assign(delegate, missing_owner, role_name)
        database.session.commit()
        assert missing_owner.municipio_id is None and missing_owner.is_active is True
        assert resolve_consistent_user_tenant(delegate) is None
        for path in paths:
            with runtime.app.test_request_context(
                path, query_string={"tipo": "municipio"}, headers=_headers(delegate, missing_owner)
            ):
                assert admin_surface_access_allowed(delegate, allow_employee=True) is True
            denied(delegate, missing_owner, path)

    # A legal single-owner profile can still belong to the wrong domain.
    # Neither actual ownership nor an authenticated, allowlisted platform actor
    # can turn this explicit PyME profile into municipal statistics authority.
    from services.municipal_stats import build_stats_for_municipio
    from services.tenant_ticket_scope import TicketTenantScopeError
    from utils.roles import is_authorized_superadmin_user

    wrong_domain = TenantProfile(
        slug="municipal-wrong-domain-" + uuid4().hex,
        nombre="Synthetic non-municipal profile", tipo="pyme",
        municipio_id=owner.id, pyme_id=None, plan="enterprise", is_active=True,
    )
    platform = user("structural-platform", role="super_admin")
    database.session.add(wrong_domain)
    database.session.commit()
    assert wrong_domain.is_active is True and wrong_domain.tipo == "pyme"
    assert wrong_domain.municipio_id == owner.id and wrong_domain.pyme_id is None
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("CLERK_SUPERADMIN_EMAILS", platform.email)
        assert is_authorized_superadmin_user(platform) is True
        for actor in (owner, platform):
            for path in paths:
                denied(actor, wrong_domain, path)

    with no_municipal_row_read():
        with pytest.raises(TicketTenantScopeError) as rejected:
            build_stats_for_municipio(owner.id, tenant_id=wrong_domain.id, actor=owner)
    assert rejected.value.code == "ticket_tenant_scope_unavailable"

    assert request_count == 63
    return request_count



def test_postgres_exact_profile_quarantines_shared_owner_and_legacy_rows(pg_metrics_app):
    from models import PymeTicket, TenantProfile, User, UserRole
    runtime = pg_metrics_app
    with runtime.app.app_context():
        owner, selected = _owner_and_tenant(runtime.db, "shared-owner")
        other = TenantProfile(slug="shared-other-" + uuid4().hex, nombre="Synthetic other",
                              tipo="pyme", pyme_id=owner.id, is_active=True)
        runtime.db.session.add(other)
        runtime.db.session.flush()
        _order(runtime.db, owner, selected.id, 100)
        _order(runtime.db, owner, other.id, 900)
        _order(runtime.db, owner, None, 500)
        runtime.db.session.commit()
        response = _get(runtime, owner, selected)
        assert response.status_code == 200
        assert response.get_json()["total_sales"] == 100
        assert response.get_json()["total_orders"] == 1
        dashboard = _get(runtime, owner, selected, "/estadisticas/dashboard?tipo=pyme")
        assert dashboard.status_code == 200
        assert dashboard.get_json()["summary"]["total_ventas"] == 100

        # Same legacy owner does not grant access to the other exact profile.
        # Real persisted ticket markers distinguish A (3), B (7) and C (11).
        # Third profile is active and shares the same legacy owner, but its
        # persisted domain must never be accepted for requested PyME statistics.
        domain_tenant = TenantProfile(
            slug="shared-domain-" + uuid4().hex, nombre="Synthetic other domain",
            tipo="colegio", pyme_id=owner.id, plan="enterprise", is_active=True,
        )
        runtime.db.session.add(domain_tenant)
        runtime.db.session.flush()
        employee = User(
            name="Synthetic exact-profile employee", email=uuid4().hex + "@example.invalid",
            rol="empleado", tipo_chat="pyme", es_empleado=True,
            tenant_id=selected.id, tenant_slug=selected.slug,
            pyme_id=owner.id, empresa_id=None,
            ticket_categorias="synthetic-shared-owner-a,synthetic-shared-owner-c",
            accesibilidad={"employee_scope": {"categorias": [
                "synthetic-shared-owner-a", "synthetic-shared-owner-c",
            ]}},
        )
        employee.set_password(secrets.token_urlsafe(24))
        runtime.db.session.add(employee)
        markers = (
            (selected, "synthetic-shared-owner-a", 3, -34.601, -58.401),
            (other, "synthetic-shared-owner-b", 7, -34.602, -58.402),
            (domain_tenant, "synthetic-shared-owner-c", 11, -34.603, -58.403),
        )
        ticket_number = 610000
        for tenant, marker, count, latitude, longitude in markers:
            for _ in range(count):
                ticket_number += 1
                runtime.db.session.add(PymeTicket(
                    tenant_id=tenant.id, user_id=owner.id, nro_ticket=ticket_number,
                    pregunta="Synthetic exact-profile heatmap fixture", categoria=marker,
                    estado="nuevo", latitud=latitude, longitud=longitude,
                ))
        runtime.db.session.commit()
        assert owner.tenant_id == selected.id and owner.tenant_slug == selected.slug
        assert UserRole.query.filter_by(user_id=owner.id, tenant_id=other.id).count() == 0
        paths = ("/estadisticas/mapa_calor/datos?tipo=pyme", "/estadisticas/tickets?tipo=pyme")
        assert employee.tenant_id == selected.id and employee.tenant_slug == selected.slug
        assert employee.empresa_id is None
        for actor in (owner, employee):
            assert UserRole.query.filter_by(user_id=actor.id, tenant_id=domain_tenant.id).count() == 0
        for domain_type in ("colegio", "unknown"):
            domain_tenant.tipo = domain_type
            runtime.db.session.flush()
            runtime.db.session.commit()
            assert domain_tenant.is_active is True
            for actor in (owner, employee):
                for path in paths:
                    denied = _get(runtime, actor, domain_tenant, path)
                    assert denied.status_code == 403

        # The employee is admitted to A; its three records remain below k=5.
        for path in paths:
            allowed = _get(runtime, employee, selected, path)
            assert allowed.status_code == 200
            payload = allowed.get_json()
            assert payload["privacy"]["mode"] == "employee_aggregated"
            assert payload["heatmap"] == []

        for path in paths:
            denied = _get(runtime, owner, other, path)
            assert denied.status_code == 403
            allowed = _get(runtime, owner, selected, path)
            assert allowed.status_code == 200
            points = allowed.get_json()["heatmap"]
            assert len(points) == 1
            assert points[0]["categoria"] == "synthetic-shared-owner-a"
            assert points[0]["weight"] == 3
            assert points[0]["location"] == {"lat": -34.601, "lng": -58.401}

        _grant(runtime.db, owner, other)
        runtime.db.session.commit()
        grant = UserRole.query.filter_by(user_id=owner.id, tenant_id=other.id).one()
        assert grant.role.name == "admin"
        for path in paths:
            allowed = _get(runtime, owner, other, path)
            assert allowed.status_code == 200
            points = allowed.get_json()["heatmap"]
            assert len(points) == 1
            assert points[0]["categoria"] == "synthetic-shared-owner-b"
            assert points[0]["weight"] == 7
            assert points[0]["location"] == {"lat": -34.602, "lng": -58.402}

        UserRole.query.filter_by(user_id=owner.id, tenant_id=other.id).delete()
        runtime.db.session.commit()
        for path in paths:
            revoked = _get(runtime, owner, other, path)
            assert revoked.status_code == 403

        _exercise_municipal_statistics_scope(runtime)


def test_postgres_unique_owner_keeps_only_safe_unbound_legacy_orders(pg_metrics_app):
    runtime = pg_metrics_app
    with runtime.app.app_context():
        owner, selected = _owner_and_tenant(runtime.db, "legacy-unique")
        _, foreign = _owner_and_tenant(runtime.db, "legacy-foreign")
        _order(runtime.db, owner, selected.id, 100)
        _order(runtime.db, owner, None, 50)
        _order(runtime.db, owner, foreign.id, 900)
        runtime.db.session.commit()
        response = _get(runtime, owner, selected)
        assert response.status_code == 200
        assert response.get_json()["total_sales"] == 150
        assert response.get_json()["total_orders"] == 2


def test_postgres_registered_grant_and_selector_conflict_boundaries(pg_metrics_app, monkeypatch):
    runtime = pg_metrics_app
    with runtime.app.app_context():
        actor, primary = _owner_and_tenant(runtime.db, "grant-primary")
        owner, granted = _owner_and_tenant(runtime.db, "grant-target")
        _order(runtime.db, actor, primary.id, 100)
        _order(runtime.db, owner, granted.id, 900)
        runtime.db.session.commit()
        denied = _get(runtime, actor, granted)
        assert denied.status_code == 403
        _grant(runtime.db, actor, granted)
        runtime.db.session.commit()
        allowed = _get(runtime, actor, granted)
        assert allowed.status_code == 200 and allowed.get_json()["total_sales"] == 900
        dashboard = _get(runtime, actor, granted, "/estadisticas/dashboard?tipo=pyme")
        assert dashboard.status_code == 200
        assert dashboard.get_json()["summary"]["total_ventas"] == 900
        conflicting = _get(runtime, actor, primary, "/estadisticas/dashboard",
                           query={"tipo": "pyme", "tenant_slug": granted.slug})
        assert conflicting.status_code == 403


        from models import TenantProfile, User, UserRole
        UserRole.query.filter_by(user_id=actor.id, tenant_id=granted.id).delete()
        runtime.db.session.commit()
        for path in ("/api/metrics/summary", "/estadisticas/dashboard?tipo=pyme"):
            revoked = _get(runtime, actor, granted, path)
            assert revoked.status_code == 403
            assert revoked.get_json()["reason_code"] == "metrics_tenant_scope_unavailable"

        municipal_owner = User(name="Synthetic municipal owner", email=uuid4().hex + "@example.invalid",
                               rol="admin", tipo_chat="municipio")
        municipal_owner.set_password("synthetic-postgres-only")
        runtime.db.session.add(municipal_owner)
        runtime.db.session.flush()
        municipal_tenant = TenantProfile(slug="metrics-municipal-" + uuid4().hex,
                                        nombre="Synthetic municipality", tipo="municipio", plan="enterprise",
                                        is_active=True, municipio_id=municipal_owner.id)
        runtime.db.session.add(municipal_tenant)
        runtime.db.session.flush()
        employee = User(name="Synthetic scoped employee", email=uuid4().hex + "@example.invalid",
                        rol="empleado", tipo_chat="municipio", es_empleado=True,
                        tenant_id=municipal_tenant.id, tenant_slug=municipal_tenant.slug,
                        municipio_id=municipal_owner.id, empresa_id=municipal_owner.id,
                        accesibilidad={"employee_scope": {"categorias": ["synthetic-category"]}})
        employee.set_password("synthetic-postgres-only")
        runtime.db.session.add(employee)
        runtime.db.session.commit()
        def forbidden_municipal_source(*args, **kwargs):
            raise AssertionError("Municipal employee denial must precede aggregate materialization")
        with monkeypatch.context() as guard:
            guard.setattr("routes.metricas.MunicipioMetricasService", forbidden_municipal_source)
            denied = _get(runtime, employee, municipal_tenant, "/api/municipal/metrics/summary")
        assert denied.status_code == 403
        assert denied.get_json()["reason_code"] == "employee_analytics_scope_unsupported"
        assert denied.get_json()["replacement_endpoint"] == "/api/v2/analytics/operations/dashboard"
        assert denied.headers["Cache-Control"] == "no-store"


def test_postgres_empty_scope_has_observed_zero_but_no_average(pg_metrics_app):
    runtime = pg_metrics_app
    with runtime.app.app_context():
        owner, tenant = _owner_and_tenant(runtime.db, "empty")
        runtime.db.session.commit()
        response = _get(runtime, owner, tenant)
        assert response.status_code == 200
        data = response.get_json()
        assert data["total_sales"] == data["total_orders"] == data["unique_customers"] == 0
        assert data["avg_order_value"] is None and data["currency"] is None
        assert data["trend"] is None and data["conversion_rate"] is None
        assert data["availability"]["avg_order_value"]["reason_code"] == "no_orders"


def test_postgres_real_select_failure_never_becomes_zero(pg_metrics_app, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    from services.metricas_service import MetricasService
    runtime = pg_metrics_app
    with runtime.app.app_context():
        owner, tenant = _owner_and_tenant(runtime.db, "sql-error")
        _order(runtime.db, owner, tenant.id, 100)
        runtime.db.session.commit()
        service = MetricasService(owner.id, tenant_id=tenant.id)
        missing = runtime.schema + "_absent"
        with runtime.root.connect() as connection:
            assert connection.execute(text("SELECT to_regnamespace(:schema)"), {"schema": missing}).scalar() is None
        engine = runtime.db.engine
        original_options = dict(engine.get_execution_options())
        runtime.db.session.remove()
        try:
            # No DDL or provider fault: actual PostgreSQL SELECTs refer to an
            # explicitly nonexistent synthetic schema, and must raise.
            engine.update_execution_options(schema_translate_map={None: missing})
            with pytest.raises(SQLAlchemyError) as error:
                service.get_summary()
            assert getattr(error.value.orig, "sqlstate", None) in {"42P01", "3F000"}
        finally:
            runtime.db.session.rollback()
            runtime.db.session.remove()
            engine.update_execution_options(**original_options)
        assert MetricasService(owner.id, tenant_id=tenant.id).get_summary()["total_sales"] == 100


        # Enter the actual HTTP route/auth/scope first, then fault only its ORM
        # source lookup. The original service executes real PostgreSQL SELECTs;
        # no SQL exception or API response is mocked and no DDL is performed.
        original_summary = MetricasService.get_summary
        source_errors = []
        def summary_with_missing_owned_source(self):
            runtime.db.session.remove()
            engine.update_execution_options(schema_translate_map={None: missing})
            try:
                return original_summary(self)
            except SQLAlchemyError as error:
                source_errors.append(getattr(error.orig, "sqlstate", None))
                raise
            finally:
                runtime.db.session.rollback()
                runtime.db.session.remove()
                engine.update_execution_options(**original_options)
        with monkeypatch.context() as fault:
            fault.setattr(MetricasService, "get_summary", summary_with_missing_owned_source)
            fault.setitem(runtime.app.config, "PROPAGATE_EXCEPTIONS", False)
            failed = _get(runtime, owner, tenant)
        assert source_errors and source_errors[-1] in {"42P01", "3F000"}
        assert failed.status_code == 500
        payload = failed.get_json(silent=True)
        if isinstance(payload, dict):
            assert "total_sales" not in payload and "total_orders" not in payload
        assert _get(runtime, owner, tenant).get_json()["total_sales"] == 100
