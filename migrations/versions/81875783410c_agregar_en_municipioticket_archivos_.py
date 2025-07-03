"""agregar en municipioticket archivos adjuntos

Revision ID: 81875783410c
Revises: a9ffe38fb399
Create Date: 2025-07-02 22:37:03.921847

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '81875783410c'
down_revision = 'a9ffe38fb399'
branch_labels = None
depends_on = None

def upgrade():
    # Por compatibilidad con SQLite y para NO romper nada, NO se borra la columna aquí.
    # Simplemente marcamos la migración como aplicada.
    pass

def downgrade():
    # No hacemos nada en el downgrade.
    pass
