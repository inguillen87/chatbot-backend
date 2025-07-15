"""Agrego campos de vecino a municipio_ticket

Revision ID: 4b9953f012b9
Revises: db48e3a4c174
Create Date: 2025-07-09 13:28:18.669144

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '4b9953f012b9'
down_revision = 'db48e3a4c174'
branch_labels = None
depends_on = None

def upgrade():
    # Todas las columnas ya existen. No hacemos nada para evitar errores.
    pass

def downgrade():
    # NO hagas downgrade. Dejalo vacío para no intentar borrar columnas con datos.
    pass
