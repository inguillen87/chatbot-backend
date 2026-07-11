import importlib.util
from pathlib import Path

import sqlalchemy as sa
from werkzeug.security import check_password_hash, generate_password_hash


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260711_revoke_unauthorized_superadmins.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "revoke_legacy_superadmins_migration",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_upgrade_revokes_only_credentials_that_are_known_to_be_compromised(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String),
        sa.Column("rol", sa.String),
        sa.Column("password_hash", sa.String),
        sa.Column("token", sa.String),
        sa.Column("entity_token", sa.String),
        sa.Column("tenant_id", sa.Integer),
        sa.Column("tenant_slug", sa.String),
        sa.Column("municipio_id", sa.Integer),
        sa.Column("pyme_id", sa.Integer),
        sa.Column("empresa_id", sa.Integer),
        sa.Column("password_reset_selector", sa.String),
        sa.Column("password_reset_verifier_hash", sa.String),
        sa.Column("password_reset_sent_at", sa.DateTime(timezone=True)),
    )
    metadata.create_all(engine)

    original_password = generate_password_hash("known-password")
    fixtures = [
        {
            "id": 1,
            "email": "guillen.marce@gmail.com",
            "rol": "super_admin",
            "password_hash": original_password,
            "token": "owner-api-token",
            "entity_token": "owner-entity-token",
        },
        {
            "id": 2,
            "email": "legacy-admin@example.com",
            "rol": "superadmin",
            "password_hash": original_password,
            "token": "legacy-api-token",
            "entity_token": "legacy-entity-token",
            "tenant_id": 10,
        },
        {
            "id": 3,
            "email": "mauricio@junin.com",
            "rol": "admin",
            "password_hash": original_password,
            "token": "junin-api-token",
            "entity_token": "junin-entity-token",
            "tenant_id": 20,
        },
        {
            "id": 4,
            "email": "franco@cuatrofincas.com",
            "rol": "admin",
            "password_hash": original_password,
            "token": "exposed-franco-token",
            "entity_token": "franco-entity-token",
            "tenant_id": 30,
        },
        {
            "id": 5,
            "email": "info@servill.ar",
            "rol": "admin_pyme",
            "password_hash": original_password,
            "token": "servill-api-token",
            "entity_token": "servill-entity-token",
            "tenant_id": 40,
        },
    ]

    migration = _load_migration()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "guillen.marce@gmail.com")

    with engine.begin() as connection:
        for fixture in fixtures:
            connection.execute(users.insert(), fixture)
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        rows = {
            row.email: row
            for row in connection.execute(sa.select(users)).mappings().all()
        }

    owner = rows["guillen.marce@gmail.com"]
    assert owner["rol"] == "super_admin"
    assert owner["token"] != "owner-api-token"
    assert owner["entity_token"] is None
    assert not check_password_hash(owner["password_hash"], "known-password")

    unauthorized = rows["legacy-admin@example.com"]
    assert unauthorized["rol"] == "admin"
    assert unauthorized["token"] != "legacy-api-token"
    assert unauthorized["entity_token"] is None
    assert not check_password_hash(unauthorized["password_hash"], "known-password")

    mauricio = rows["mauricio@junin.com"]
    assert mauricio["token"] == "junin-api-token"
    assert mauricio["entity_token"] == "junin-entity-token"
    assert not check_password_hash(mauricio["password_hash"], "known-password")

    franco = rows["franco@cuatrofincas.com"]
    assert franco["token"] != "exposed-franco-token"
    assert franco["entity_token"] == "franco-entity-token"
    assert not check_password_hash(franco["password_hash"], "known-password")

    servill = rows["info@servill.ar"]
    assert servill["token"] == "servill-api-token"
    assert servill["entity_token"] == "servill-entity-token"
    assert not check_password_hash(servill["password_hash"], "known-password")
