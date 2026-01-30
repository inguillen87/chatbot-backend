"""add catalog processing tables

Revision ID: 20300113
Revises: 8e8cabaf31c8
Create Date: 2026-01-30 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

# revision identifiers, used by Alembic.
revision = '20300113'
down_revision = '8e8cabaf31c8'
branch_labels = None
depends_on = None

def upgrade():
    # Helper to check if tables exist to avoid errors in dev environments with partial migrations
    conn = op.get_bind()
    from sqlalchemy.engine.reflection import Inspector
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if 'catalog_upload' not in tables:
        op.create_table('catalog_upload',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('filename', sa.String(length=255), nullable=False),
            sa.Column('mime_type', sa.String(length=100), nullable=True),
            sa.Column('processor_slug', sa.String(length=50), nullable=False, server_default='generic'),
            sa.Column('processor_version', sa.String(length=20), nullable=True, server_default='1.0'),
            sa.Column('status', sa.String(length=20), nullable=True),
            # Use JSON type compatible with the dialect (alembic generic or dialect specific)
            # Simplification: Use sa.JSON() which compiles to JSON/JSONB depending on backend in recent SA versions
            sa.Column('stats', sa.JSON(), nullable=True),
            sa.Column('raw_text_snippet', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_catalog_upload_tenant_id'), 'catalog_upload', ['tenant_id'], unique=False)

    if 'tenant_catalog_mapping' not in tables:
        op.create_table('tenant_catalog_mapping',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('rubro_slug', sa.String(length=50), nullable=False),
            sa.Column('mapping_json', sa.JSON(), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenant_profile.id'], ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('tenant_id', 'rubro_slug', name='uq_tenant_mapping_rubro')
        )
        op.create_index(op.f('ix_tenant_catalog_mapping_tenant_id'), 'tenant_catalog_mapping', ['tenant_id'], unique=False)

    # Add catalog_upload_id to catalogo_item
    # Check if column exists
    columns_item = [col['name'] for col in inspector.get_columns('catalogo_item')]
    if 'catalog_upload_id' not in columns_item:
        op.add_column('catalogo_item', sa.Column('catalog_upload_id', sa.Integer(), nullable=True))
        op.create_foreign_key('fk_catalogo_item_catalog_upload', 'catalogo_item', 'catalog_upload', ['catalog_upload_id'], ['id'])

def downgrade():
    op.drop_constraint('fk_catalogo_item_catalog_upload', 'catalogo_item', type_='foreignkey')
    op.drop_column('catalogo_item', 'catalog_upload_id')
    op.drop_index(op.f('ix_tenant_catalog_mapping_tenant_id'), table_name='tenant_catalog_mapping')
    op.drop_table('tenant_catalog_mapping')
    op.drop_index(op.f('ix_catalog_upload_tenant_id'), table_name='catalog_upload')
    op.drop_table('catalog_upload')
