"""Add a dedicated append-only-by-application store for study declarations.

Revision ID: 20260922_methodology_v1
Revises: 20260906_flask_sessions_v1
No customer data is migrated or inferred. Enable the feature only after upgrade.
"""
from alembic import op
import sqlalchemy as sa

revision = '20260922_methodology_v1'
down_revision = '20260906_flask_sessions_v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('survey_methodology_revision',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('survey_id', sa.Integer(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('instrument_revision', sa.Integer(), nullable=False),
        sa.Column('fields', sa.JSON(), nullable=False),
        sa.Column('change_reason', sa.String(500), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('previous_digest', sa.String(64), nullable=True),
        sa.Column('operation_digest', sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id','survey_id'],['enc_encuesta.tenant_id','enc_encuesta.id'],
            name='fk_methodology_survey',ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['actor_user_id'],['user.id'],ondelete='RESTRICT'),
        sa.UniqueConstraint('tenant_id','survey_id','revision',name='uq_methodology_revision'),
        sa.CheckConstraint('revision > 0 AND instrument_revision > 0',name='ck_methodology_revisions'))
    op.create_index('ix_methodology_scope_revision','survey_methodology_revision',['tenant_id','survey_id','revision'])


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM survey_methodology_revision LIMIT 1')).first():
        raise RuntimeError('Methodology history exists; refusing destructive downgrade')
    op.drop_index('ix_methodology_scope_revision',table_name='survey_methodology_revision')
    op.drop_table('survey_methodology_revision')
