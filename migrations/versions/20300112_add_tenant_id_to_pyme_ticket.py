"""Add tenant_id to pyme_ticket.

Revision ID: 20300112_add_tenant_id_to_pyme_ticket
Revises: 20300111_merge_tenant_profile_heads
Create Date: 2030-01-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


# revision identifiers, used by Alembic.
revision = '20300112_add_tenant_id_to_pyme_ticket'
down_revision = '20300111_merge_tenant_profile_heads'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns_pyme = [col['name'] for col in inspector.get_columns('pyme_ticket')]

    if 'tenant_id' not in columns_pyme:
        op.add_column('pyme_ticket', sa.Column('tenant_id', sa.Integer(), nullable=True))
        # Add foreign key constraint if it doesn't exist (names should be unique)
        # op.create_foreign_key('fk_pyme_ticket_tenant_id', 'pyme_ticket', 'tenant_profile', ['tenant_id'], ['id'])
        # NOTE: We skip FK creation to avoid issues with inconsistent data in dev,
        # but in prod it should be added. For now, just the column to stop 500 errors.

    columns_muni = [col['name'] for col in inspector.get_columns('municipio_ticket')]
    if 'tenant_id' not in columns_muni:
        op.add_column('municipio_ticket', sa.Column('tenant_id', sa.Integer(), nullable=True))


def downgrade():
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns_pyme = [col['name'] for col in inspector.get_columns('pyme_ticket')]

    if 'tenant_id' in columns_pyme:
        op.drop_column('pyme_ticket', 'tenant_id')

    columns_muni = [col['name'] for col in inspector.get_columns('municipio_ticket')]
    if 'tenant_id' in columns_muni:
        op.drop_column('municipio_ticket', 'tenant_id')
