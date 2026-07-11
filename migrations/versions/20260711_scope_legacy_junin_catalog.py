"""scope legacy Junin catalog items to the production tenant

Revision ID: 20260711_scope_junin_catalog
Revises: 20260711_revoke_legacy_superadmins
Create Date: 2026-07-11 00:00:01.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260711_scope_junin_catalog"
down_revision = "20260711_revoke_legacy_superadmins"
branch_labels = None
depends_on = None


JUNIN_SEED_ITEMS = {
    "junin-kit-escolar": ("canje", 1500),
    "junin-arbol-nativo": ("donacion", 0),
    "junin-bono-hospital": ("donacion", 2500),
    "junin-bolson-saludable": ("compra", None),
    "junin-canje-ewaste": ("canje", 800),
}


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _junin_scope(bind):
    tenant = sa.table(
        "tenant_profile",
        sa.column("id", sa.Integer()),
        sa.column("slug", sa.String()),
        sa.column("municipio_id", sa.Integer()),
        sa.column("pyme_id", sa.Integer()),
    )
    row = bind.execute(
        sa.select(
            tenant.c.id,
            tenant.c.municipio_id,
            tenant.c.pyme_id,
        ).where(sa.func.lower(tenant.c.slug) == "junin")
    ).mappings().first()
    if not row:
        return None, None
    return row["id"], row["municipio_id"] or row["pyme_id"]


def upgrade() -> None:
    tenant_columns = _columns("tenant_profile")
    item_columns = _columns("catalogo_item")
    if not {"id", "slug", "municipio_id", "pyme_id"}.issubset(tenant_columns):
        return
    if not {"user_id", "tenant_id", "sku"}.issubset(item_columns):
        return

    bind = op.get_bind()
    tenant_id, owner_id = _junin_scope(bind)
    if not tenant_id or not owner_id:
        return

    catalog = sa.table(
        "catalogo_item",
        sa.column("user_id", sa.Integer()),
        sa.column("tenant_id", sa.Integer()),
        sa.column("sku", sa.String()),
        sa.column("modalidad", sa.String()),
        sa.column("precio_puntos", sa.Integer()),
    )
    for sku, (mode, points) in JUNIN_SEED_ITEMS.items():
        values = {"tenant_id": tenant_id}
        if "modalidad" in item_columns:
            values["modalidad"] = mode
        if "precio_puntos" in item_columns:
            values["precio_puntos"] = points
        bind.execute(
            catalog.update()
            .where(
                catalog.c.user_id == owner_id,
                catalog.c.tenant_id.is_(None),
                sa.func.lower(catalog.c.sku) == sku,
            )
            .values(**values)
        )


def downgrade() -> None:
    tenant_columns = _columns("tenant_profile")
    item_columns = _columns("catalogo_item")
    if not {"id", "slug", "municipio_id", "pyme_id"}.issubset(tenant_columns):
        return
    if not {"user_id", "tenant_id", "sku"}.issubset(item_columns):
        return

    bind = op.get_bind()
    tenant_id, owner_id = _junin_scope(bind)
    if not tenant_id or not owner_id:
        return

    catalog = sa.table(
        "catalogo_item",
        sa.column("user_id", sa.Integer()),
        sa.column("tenant_id", sa.Integer()),
        sa.column("sku", sa.String()),
    )
    bind.execute(
        catalog.update()
        .where(
            catalog.c.user_id == owner_id,
            catalog.c.tenant_id == tenant_id,
            sa.func.lower(catalog.c.sku).in_(tuple(JUNIN_SEED_ITEMS)),
        )
        .values(tenant_id=None)
    )
