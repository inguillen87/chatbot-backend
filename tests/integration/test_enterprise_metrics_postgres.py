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
            "enterprise_metrics.business_candidate": "tenant-closure-v1",
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
    token = issue_token({"user_id": owner.id, "rol": owner.rol, "tipo_chat": owner.tipo_chat,
                         "exp": datetime.now(timezone.utc) + timedelta(hours=1)})
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


def test_postgres_exact_profile_quarantines_shared_owner_and_legacy_rows(pg_metrics_app):
    from models import PymeTicket, TenantProfile, UserRole
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
        # Real persisted ticket markers distinguish A (3) from B (7).
        markers = (
            (selected, "synthetic-shared-owner-a", 3, -34.601, -58.401),
            (other, "synthetic-shared-owner-b", 7, -34.602, -58.402),
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
