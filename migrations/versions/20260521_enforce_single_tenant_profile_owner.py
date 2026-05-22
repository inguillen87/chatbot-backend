"""enforce single tenant profile per owner

Revision ID: 20260521_single_tenant_owner
Revises: 20260521_provider_platform
Create Date: 2026-05-21 20:00:00.000000

"""
from alembic import op


revision = "20260521_single_tenant_owner"
down_revision = "20260521_provider_platform"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_profile_municipio_owner
        ON tenant_profile (municipio_id)
        WHERE municipio_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_profile_pyme_owner
        ON tenant_profile (pyme_id)
        WHERE pyme_id IS NOT NULL
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_tenant_profile_pyme_owner")
    op.execute("DROP INDEX IF EXISTS uq_tenant_profile_municipio_owner")
