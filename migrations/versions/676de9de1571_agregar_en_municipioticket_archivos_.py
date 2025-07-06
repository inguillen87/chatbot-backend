"""agregar en municipioticket archivos adjuntos

Revision ID: 676de9de1571
Revises: 81875783410c
Create Date: 2025-07-06 12:46:39.052869

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '676de9de1571'
down_revision = '81875783410c'
branch_labels = None
depends_on = None


def upgrade():
    # No hacer nada, la estructura ya es compatible
    pass

def downgrade():
    pass

