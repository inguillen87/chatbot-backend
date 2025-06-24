"""Add tipo_chat column to user

Revision ID: ee3694e2e48a
Revises: 2051cf9e6752
Create Date: 2025-07-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'ee3694e2e48a'
down_revision = '2051cf9e6752'
branch_labels = None
depends_on = None


PUBLICOS = ('municipio','municipios','ong','gobierno','hospital_publico','entidad_publica')

def upgrade():
    op.add_column('user', sa.Column('tipo_chat', sa.String(length=20), nullable=True))
    op.execute(
        """
        UPDATE user
        SET tipo_chat = CASE
            WHEN rubro_id IN (
                SELECT id FROM rubro WHERE lower(nombre) IN %s OR lower(clave) IN %s
            ) THEN 'municipio'
            ELSE 'pyme'
        END
        """ % (PUBLICOS, PUBLICOS)
    )


def downgrade():
    op.drop_column('user', 'tipo_chat')
