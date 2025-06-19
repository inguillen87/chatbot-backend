"""add anon_id to ticket_comentario

Revision ID: f1d2e3a4b5c6
Revises: e1e8d4a4968d
Create Date: 2025-06-20 00:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f1d2e3a4b5c6'
down_revision = 'e1e8d4a4968d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ticket_comentario', schema=None) as batch_op:
        batch_op.add_column(sa.Column('anon_id', sa.String(length=80), nullable=True))
        batch_op.create_index(batch_op.f('ix_ticket_comentario_anon_id'), ['anon_id'], unique=False)


def downgrade():
    with op.batch_alter_table('ticket_comentario', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ticket_comentario_anon_id'))
        batch_op.drop_column('anon_id')
