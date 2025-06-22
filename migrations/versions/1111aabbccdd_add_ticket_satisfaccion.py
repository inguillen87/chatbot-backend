"""add ticket_satisfaccion table

Revision ID: 1111aabbccdd
Revises: f1d2e3a4b5c6
Create Date: 2025-06-30 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '1111aabbccdd'
down_revision = 'f1d2e3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ticket_satisfaccion',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(length=10), nullable=False),
        sa.Column('puntuacion', sa.Integer(), nullable=False),
        sa.Column('comentario', sa.Text(), nullable=True),
        sa.Column('fecha', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('ticket_satisfaccion')
