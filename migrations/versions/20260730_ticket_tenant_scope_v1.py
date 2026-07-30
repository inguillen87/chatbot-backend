"""backfill deterministic tenant scope for legacy municipal tickets

Revision ID: 20260730_ticket_tenant_scope_v1
Revises: 20260730_voice_consent_v1
Create Date: 2026-07-30 23:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_ticket_tenant_scope_v1"
down_revision = "20260730_voice_consent_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill only when the legacy owner has one and only one candidate across
    # both tenant owner columns.  Ambiguous and orphaned rows deliberately stay
    # null so application reads fail closed instead of guessing a tenant.
    op.execute(
        sa.text(
            """
            UPDATE municipio_ticket
            SET tenant_id = (
                SELECT MIN(tp.id)
                FROM tenant_profile AS tp
                WHERE tp.municipio_id = municipio_ticket.municipio_id
                   OR tp.pyme_id = municipio_ticket.municipio_id
            )
            WHERE tenant_id IS NULL
              AND municipio_id IS NOT NULL
              AND 1 = (
                  SELECT COUNT(DISTINCT tp.id)
                  FROM tenant_profile AS tp
                  WHERE tp.municipio_id = municipio_ticket.municipio_id
                     OR tp.pyme_id = municipio_ticket.municipio_id
              )
            """
        )
    )
    op.create_index(
        "ix_municipio_ticket_owner_tenant",
        "municipio_ticket",
        ["municipio_id", "tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_municipio_ticket_owner_tenant",
        table_name="municipio_ticket",
    )
    # The deterministic assignments are valid domain data.  Downgrading the
    # supporting index must not erase them or recreate ambiguous null rows.
