"""scope PymePedido idempotency by tenant and preserve source currency

Revision ID: 20260712_pyme_pedido_idem
Revises: 20260711_scope_junin_catalog
Create Date: 2026-07-12 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_pyme_pedido_idem"
down_revision = "20260711_scope_junin_catalog"
branch_labels = None
depends_on = None


TABLE_NAME = "pyme_pedido"
IDEMPOTENCY_INDEX = "ix_pyme_pedido_idempotency_key"
TENANT_IDEMPOTENCY_CONSTRAINT = "uq_pyme_pedido_tenant_idempotency"


def _inspector():
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    inspector = _inspector()
    if TABLE_NAME not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE_NAME)}


def _index_state():
    inspector = _inspector()
    indexes = inspector.get_indexes(TABLE_NAME)
    constraints = inspector.get_unique_constraints(TABLE_NAME)
    return indexes, constraints


def _backfill_unambiguous_tenant_ids() -> None:
    inspector = _inspector()
    if "tenant_profile" not in set(inspector.get_table_names()):
        return
    tenant_columns = {column["name"] for column in inspector.get_columns("tenant_profile")}
    if not {"id", "pyme_id"}.issubset(tenant_columns):
        return

    bind = op.get_bind()
    tenant_profile = sa.table(
        "tenant_profile",
        sa.column("id", sa.Integer()),
        sa.column("pyme_id", sa.Integer()),
    )
    pyme_pedido = sa.table(
        TABLE_NAME,
        sa.column("tenant_id", sa.Integer()),
        sa.column("pyme_id", sa.Integer()),
    )
    mappings = bind.execute(
        sa.select(
            tenant_profile.c.pyme_id,
            sa.func.min(tenant_profile.c.id).label("tenant_id"),
        )
        .where(tenant_profile.c.pyme_id.is_not(None))
        .group_by(tenant_profile.c.pyme_id)
        .having(sa.func.count(tenant_profile.c.id) == 1)
    ).mappings()
    for mapping in mappings:
        bind.execute(
            pyme_pedido.update()
            .where(
                pyme_pedido.c.tenant_id.is_(None),
                pyme_pedido.c.pyme_id == mapping["pyme_id"],
            )
            .values(tenant_id=mapping["tenant_id"])
        )


def upgrade() -> None:
    columns = _columns()
    if not {"tenant_id", "idempotency_key"}.issubset(columns):
        return

    _backfill_unambiguous_tenant_ids()
    indexes, constraints = _index_state()
    global_constraint_names = {
        constraint["name"]
        for constraint in constraints
        if list(constraint.get("column_names") or []) == ["idempotency_key"] and constraint.get("name")
    }
    idempotency_indexes = [
        index
        for index in indexes
        if list(index.get("column_names") or []) == ["idempotency_key"] and index.get("name")
        and index.get("duplicates_constraint") not in global_constraint_names
    ]
    global_constraints = [
        constraint
        for constraint in constraints
        if list(constraint.get("column_names") or []) == ["idempotency_key"] and constraint.get("name")
    ]
    has_tenant_constraint = any(
        list(constraint.get("column_names") or []) == ["tenant_id", "idempotency_key"]
        for constraint in constraints
    )

    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        for constraint in global_constraints:
            batch_op.drop_constraint(constraint["name"], type_="unique")
        for index in idempotency_indexes:
            batch_op.drop_index(index["name"])
        if "moneda" not in columns:
            batch_op.add_column(sa.Column("moneda", sa.String(length=10), nullable=True))
        batch_op.create_index(IDEMPOTENCY_INDEX, ["idempotency_key"], unique=False)
        if not has_tenant_constraint:
            batch_op.create_unique_constraint(
                TENANT_IDEMPOTENCY_CONSTRAINT,
                ["tenant_id", "idempotency_key"],
            )


def downgrade() -> None:
    columns = _columns()
    if not {"tenant_id", "idempotency_key"}.issubset(columns):
        return

    bind = op.get_bind()
    pyme_pedido = sa.table(
        TABLE_NAME,
        sa.column("idempotency_key", sa.String(length=128)),
    )
    duplicate = bind.execute(
        sa.select(pyme_pedido.c.idempotency_key)
        .where(pyme_pedido.c.idempotency_key.is_not(None))
        .group_by(pyme_pedido.c.idempotency_key)
        .having(sa.func.count() > 1)
        .limit(1)
    ).scalar()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot restore global PymePedido idempotency uniqueness while tenant-scoped duplicate keys exist"
        )

    indexes, constraints = _index_state()
    scoped_constraints = [
        constraint
        for constraint in constraints
        if list(constraint.get("column_names") or []) == ["tenant_id", "idempotency_key"]
        and constraint.get("name")
    ]
    idempotency_indexes = [
        index
        for index in indexes
        if list(index.get("column_names") or []) == ["idempotency_key"] and index.get("name")
    ]

    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        for constraint in scoped_constraints:
            batch_op.drop_constraint(constraint["name"], type_="unique")
        for index in idempotency_indexes:
            batch_op.drop_index(index["name"])
        if "moneda" in columns:
            batch_op.drop_column("moneda")
        batch_op.create_index(IDEMPOTENCY_INDEX, ["idempotency_key"], unique=True)
