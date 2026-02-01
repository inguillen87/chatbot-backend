"""Add theme_json to TenantProfile

Revision ID: 20300123_add_theme_json
Revises: 20300122_add_orders_tables
Create Date: 2030-01-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20300123'
down_revision = '20300122'
branch_labels = None
depends_on = None


def upgrade():
    # Helper for JSON type
    json_type = sa.JSON()

    conn = op.get_bind()
    from sqlalchemy.engine.reflection import Inspector
    inspector = Inspector.from_engine(conn)
    columns = [col['name'] for col in inspector.get_columns('tenant_profile')]

    if 'theme_json' not in columns:
        op.add_column('tenant_profile', sa.Column('theme_json', json_type, nullable=True))


def downgrade():
    op.drop_column('tenant_profile', 'theme_json')
