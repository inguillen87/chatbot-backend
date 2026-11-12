import importlib.util
from pathlib import Path

import sqlalchemy as sa

from extensions import db
from models import CatalogoItem, TenantProfile, User
from services.catalog_seed import ensure_seed_catalog, provision_demo_catalog, seed_items_for


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260711_scope_legacy_junin_catalog.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("scope_legacy_junin_catalog", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_real_municipality_is_not_eligible_for_generic_demo_seed(app):
    with app.app_context():
        db.drop_all()
        db.create_all()
        owner = User(
            name="Municipio compartido",
            email="catalog-scope@example.com",
            password_hash="test-hash",
            tipo_chat="municipio",
        )
        db.session.add(owner)
        db.session.flush()
        old_tenant = TenantProfile(
            slug="municipio-demo-scope",
            nombre="Municipio demo",
            tipo="municipio",
            municipio_id=owner.id,
        )
        junin = TenantProfile(
            slug="junin",
            nombre="Municipalidad de Junin",
            tipo="municipio",
            municipio_id=owner.id,
        )
        db.session.add_all([old_tenant, junin])
        db.session.flush()
        db.session.add(
            CatalogoItem(
                user_id=owner.id,
                tenant_id=old_tenant.id,
                nombre="Articulo de otro tenant",
                sku="other-tenant-item",
            )
        )
        db.session.commit()

        app.config["ENABLE_DEMO_MODE"] = True
        assert ensure_seed_catalog(owner, junin) is False
        assert provision_demo_catalog(owner, junin) is False
        assert CatalogoItem.query.filter_by(user_id=owner.id, tenant_id=junin.id).count() == 0
        assert CatalogoItem.query.filter_by(user_id=owner.id, tenant_id=old_tenant.id).count() == 1


def test_explicit_demo_seed_is_scoped_idempotent_and_disclosed(app):
    with app.app_context():
        db.drop_all()
        db.create_all()
        app.config["ENABLE_DEMO_MODE"] = True
        owner = User(
            name="Municipio demo",
            email="catalog-demo-scope@example.com",
            password_hash="test-hash",
            tipo_chat="municipio",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="municipio-demo-scope",
            nombre="Municipio demo",
            tipo="municipio",
            municipio_id=owner.id,
            configuracion={"demo_mode": True, "demo_catalog_seed": True},
        )
        db.session.add(tenant)
        db.session.commit()

        assert provision_demo_catalog(owner, tenant) is True
        assert provision_demo_catalog(owner, tenant) is False
        items = CatalogoItem.query.filter_by(user_id=owner.id, tenant_id=tenant.id).all()
        assert len(items) == len(seed_items_for(owner, tenant))
        assert items
        for item in items:
            assert item.extra_metadata["data_origin"] == "synthetic_demo"
            assert item.extra_metadata["synthetic_demo"] is True
            assert item.extra_metadata["seed_contract_version"] == "catalog.demo_seed.v1"


def test_migration_adopts_only_legacy_junin_seed_items(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    tenants = sa.Table(
        "tenant_profile",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String),
        sa.Column("municipio_id", sa.Integer),
        sa.Column("pyme_id", sa.Integer),
    )
    catalog = sa.Table(
        "catalogo_item",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("tenant_id", sa.Integer),
        sa.Column("sku", sa.String),
        sa.Column("modalidad", sa.String),
        sa.Column("precio_puntos", sa.Integer),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        connection.execute(
            tenants.insert(),
            [
                {"id": 22, "slug": "junin", "municipio_id": 4},
                {"id": 6, "slug": "municipio", "municipio_id": 142},
            ],
        )
        connection.execute(
            catalog.insert(),
            [
                {"id": 1, "user_id": 4, "tenant_id": None, "sku": "junin-kit-escolar", "modalidad": "venta"},
                {"id": 2, "user_id": 4, "tenant_id": None, "sku": "junin-canje-ewaste", "modalidad": "venta"},
                {"id": 3, "user_id": 4, "tenant_id": 6, "sku": "vino-ajeno", "modalidad": "venta"},
                {"id": 4, "user_id": 9, "tenant_id": None, "sku": "junin-arbol-nativo", "modalidad": "venta"},
            ],
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration.upgrade()
        rows = {
            row.id: row
            for row in connection.execute(sa.select(catalog).order_by(catalog.c.id)).mappings()
        }

    assert rows[1]["tenant_id"] == 22
    assert rows[1]["modalidad"] == "canje"
    assert rows[1]["precio_puntos"] == 1500
    assert rows[2]["tenant_id"] == 22
    assert rows[2]["modalidad"] == "canje"
    assert rows[2]["precio_puntos"] == 800
    assert rows[3]["tenant_id"] == 6
    assert rows[4]["tenant_id"] is None
