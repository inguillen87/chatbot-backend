"""extend ticket effect receipts to TenantTicket replies

Revision ID: 20260815_tenant_reply_receipt_v1
Revises: 20260814_model_schema_drift_v1
Create Date: 2026-08-15

TenantTicket operator replies use the same tenant-scoped idempotency receipt
as MunicipioTicket/PymeTicket comments.  This forward-only constraint update
does not rewrite or delete existing receipts.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260815_tenant_reply_receipt_v1"
down_revision = "20260814_model_schema_drift_v1"
branch_labels = None
depends_on = None


_TABLE = "ticket_domain_effect_receipt"
_EFFECT_CONSTRAINT = "ck_ticket_domain_effect_kind"
_RESOURCE_CONSTRAINT = "ck_ticket_domain_effect_resource_type"

_EFFECT_SQL = (
    "effect_kind IN ('ticket.create.municipio', 'ticket.create.pyme', "
    "'ticket.comment.municipio', 'ticket.comment.pyme', "
    "'ticket.comment.tenant')"
)
_RESOURCE_SQL = (
    "resource_type IN ('municipio_ticket', 'pyme_ticket', "
    "'ticket_comentario', 'tenant_ticket')"
)


def _normalized_sql(value: object) -> str:
    return "".join(str(value or "").lower().split())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        raise RuntimeError("ticket_domain_effect_receipt table is required")

    constraints = {
        str(item.get("name") or ""): _normalized_sql(item.get("sqltext"))
        for item in inspector.get_check_constraints(_TABLE)
    }
    effect_sql = constraints.get(_EFFECT_CONSTRAINT)
    resource_sql = constraints.get(_RESOURCE_CONSTRAINT)
    effect_current = effect_sql is not None and "ticket.comment.tenant" in effect_sql
    resource_current = resource_sql is not None and "tenant_ticket" in resource_sql
    if effect_current and resource_current:
        return
    if effect_current != resource_current:
        raise RuntimeError("tenant reply receipt constraints are only partially updated")
    if effect_sql is None or resource_sql is None:
        raise RuntimeError("ticket reply receipt check constraints are missing")

    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_EFFECT_CONSTRAINT, type_="check")
        batch_op.drop_constraint(_RESOURCE_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_EFFECT_CONSTRAINT, _EFFECT_SQL)
        batch_op.create_check_constraint(_RESOURCE_CONSTRAINT, _RESOURCE_SQL)


def downgrade() -> None:
    # Forward-only: narrowing these checks could invalidate durable receipts
    # created after this release.  A rollback must preserve that evidence.
    pass
