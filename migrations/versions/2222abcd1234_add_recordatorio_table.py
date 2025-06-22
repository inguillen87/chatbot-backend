"""add recordatorio table

Revision ID: 2222abcd1234
Revises: 1111aabbccdd
Create Date: 2025-07-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '2222abcd1234'
down_revision = '1111aabbccdd'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'recordatorio',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('empresa_id', sa.Integer(), nullable=False),
        sa.Column('cliente_id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(length=20), nullable=False),
        sa.Column('descripcion', sa.String(length=255), nullable=True),
        sa.Column('fecha_vencimiento', sa.DateTime(), nullable=False),
        sa.Column('enviado', sa.Boolean(), nullable=True, server_default='0'),
        sa.ForeignKeyConstraint(['empresa_id'], ['user.id']),
        sa.ForeignKeyConstraint(['cliente_id'], ['user.id']),
    )


def downgrade():
    op.drop_table('recordatorio')
