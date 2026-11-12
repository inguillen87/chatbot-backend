"""Run actual CRM template HTTP regressions on a fresh loopback PostgreSQL 18.

Reuses the existing disposable-service refusal checks. Never reads a developer
or customer's DATABASE_URL, .env or credentials. No provider/production acceptance.
"""
import json
import os
import secrets
import unittest
from uuid import uuid4

from tests.run_auth_session_lifecycle_postgres import (
    FixtureRefused,
    fixture_configuration,
    verify_empty_fixture,
)


def main(argv=None):
    configuration = fixture_configuration(argv)
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    os.environ["CORS_ALLOWED_ORIGINS"] = "https://panel.example.invalid"
    os.environ["CLERK_SUPERADMIN_EMAILS"] = "guillen.marce@gmail.com"
    import psycopg
    with psycopg.connect(**configuration, connect_timeout=3) as connection:
        verify_empty_fixture(connection)

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import URL
    from sqlalchemy.schema import CreateSchema, DropSchema
    from tests.crm_template_integrity_http import CrmTemplateIntegrityHttpTests
    from app import create_app
    from config import TestingConfig
    from models import Role, TenantProfile, User, UserRole, db

    database_url = URL.create("postgresql+psycopg", username=configuration["user"],
        password=configuration["password"], host=configuration["host"],
        port=configuration["port"], database=configuration["dbname"])

    class CrmTemplateIntegrityPostgresTests(CrmTemplateIntegrityHttpTests):
        @classmethod
        def setUpClass(cls):
            cls.root = create_engine(database_url)
            cls.schema = "crm_templates_" + uuid4().hex
            cls.app = None
            with cls.root.begin() as connection:
                connection.execute(CreateSchema(cls.schema))

            class CrmPostgresConfig(TestingConfig):
                SQLALCHEMY_DATABASE_URI = database_url
                SQLALCHEMY_ENGINE_OPTIONS = {"execution_options": {"schema_translate_map": {None: cls.schema}}}
                SECRET_KEY = secrets.token_hex(32)
                SESSION_TYPE = "null"
                SESSION_COOKIE_SAMESITE = "Lax"
                CUTOVER_WRITER_FENCE_ENABLED = False
                RATELIMIT_STORAGE_URI = "memory://"
                OUTBOUND_NOTIFICATIONS_ENABLED = False

            try:
                cls.app = create_app(CrmPostgresConfig)
                cls.password = secrets.token_urlsafe(24)
                cls.accounts = {}
                with cls.app.app_context():
                    column_type = db.session.execute(text("SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema=:schema AND table_name='tenant_profile' AND column_name='configuracion'"),
                        {"schema": cls.schema}).scalar_one()
                    if column_type != "jsonb":
                        raise FixtureRefused("crm_configuration_must_be_real_jsonb")
                    for slug in ("acceptance-a", "acceptance-b"):
                        owner = User(name="Institution " + slug, email=slug + "@example.invalid",
                            rol="admin", tipo_chat="municipio", nombre_empresa=slug)
                        owner.set_password(cls.password)
                        db.session.add(owner)
                        db.session.flush()
                        tenant = TenantProfile(slug=slug, nombre=slug, tipo="municipio",
                            municipio_id=owner.id, vertical="government", configuracion={})
                        db.session.add(tenant)
                        db.session.flush()
                        owner.tenant_id = tenant.id
                        owner.tenant_slug = slug
                        owner.municipio_id = owner.id
                        cls.accounts[slug] = {"id": owner.id, "email": owner.email, "tenant_id": tenant.id}
                        if slug != "acceptance-a":
                            continue
                        for label, role in (("second", "admin"), ("viewer", "empleado"),
                                            ("delegated", "empleado"), ("legacy-manager", "manager"),
                                            ("superadmin", "super_admin")):
                            user = User(name=label,
                                email=("guillen.marce@gmail.com" if role == "super_admin" else label + "@example.invalid"),
                                rol=role, tenant_id=(None if role == "super_admin" else tenant.id),
                                tenant_slug=(None if role == "super_admin" else slug),
                                municipio_id=(None if role == "super_admin" else owner.id), tipo_chat="municipio")
                            user.set_password(cls.password)
                            db.session.add(user)
                            db.session.flush()
                            cls.accounts[label] = {"id": user.id, "email": user.email, "tenant_id": user.tenant_id}
                            if label == "delegated":
                                assignment_role = Role(name="tenant_admin")
                                db.session.add(assignment_role)
                                db.session.flush()
                                db.session.add(UserRole(user_id=user.id, role_id=assignment_role.id, tenant_id=tenant.id))
                    db.session.commit()
            except BaseException:
                cls.tearDownClass()
                raise

        @classmethod
        def tearDownClass(cls):
            if cls.app is not None:
                with cls.app.app_context():
                    db.session.remove()
                    db.engine.dispose()
            try:
                with cls.root.begin() as connection:
                    connection.execute(DropSchema(cls.schema, cascade=True))
            finally:
                cls.root.dispose()

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CrmTemplateIntegrityPostgresTests)
    expected_count = suite.countTestCases()
    if expected_count != 9:
        raise FixtureRefused("crm_http_regression_count_changed")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # Assert cleanup as well as test results. This service can then host another
    # isolated suite; no application schema, table or extra role may survive.
    with psycopg.connect(**configuration, connect_timeout=3) as connection:
        verify_empty_fixture(connection)
    summary = {"tests_run": result.testsRun, "expected_tests": expected_count,
        "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
        "successful": result.wasSuccessful() and not result.skipped and result.testsRun == expected_count,
        "target": "explicit_fresh_loopback_postgres18", "configuration_column": "jsonb",
        "fixture_cleanup_verified": True, "remote_database_access": False}
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["successful"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FixtureRefused as error:
        print(json.dumps({"successful": False, "reason_code": str(error)}, sort_keys=True))
        raise SystemExit(2)
    except Exception as error:
        print(json.dumps({"successful": False, "reason_code": "disposable_crm_fixture_failed",
            "error_type": type(error).__name__}, sort_keys=True))
        raise SystemExit(1)
