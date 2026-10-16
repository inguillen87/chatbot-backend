"""Durable actor/session retirement authority; no changes to existing grants."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '20261001_auth_session_v1'
down_revision = '20260906_flask_sessions_v1'
branch_labels = None
depends_on = None


def upgrade():
    # These new typed tables contain no legacy backfill or inferred lineage.
    op.create_table('auth_provider_session',
        sa.Column('provider', sa.String(16), nullable=False),
        sa.Column('provider_session_id', sa.String(255), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('reason', sa.String(120)),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('remote_status', sa.String(24), nullable=False),
        sa.PrimaryKeyConstraint('provider', 'provider_session_id'))
    op.create_table('auth_session',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('actor_version', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(16), nullable=False),
        sa.Column('audience', sa.String(80), nullable=False),
        sa.Column('provider_session_id', sa.String(255)),
        sa.Column('flask_sid_hash', sa.String(64)),
        sa.Column('retirement_nonce', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['provider', 'provider_session_id'],
            ['auth_provider_session.provider', 'auth_provider_session.provider_session_id'],
            name='fk_auth_session_provider_sid'),
        sa.CheckConstraint("provider in ('native','clerk')", name='ck_auth_session_provider'),
        sa.CheckConstraint('revision >= 1', name='ck_auth_session_revision'))
    op.create_index('ix_auth_session_actor_id', 'auth_session', ['actor_id'])
    op.create_index('ix_auth_session_provider_session_id', 'auth_session', ['provider_session_id'])
    op.create_table('auth_session_audit',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('lineage_id', sa.String(64), sa.ForeignKey('auth_session.id'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('event_type', sa.String(32), nullable=False),
        sa.Column('request_id', sa.String(128)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_auth_session_audit_lineage_id', 'auth_session_audit', ['lineage_id'])
    op.create_table('auth_session_retirement',
        sa.Column('request_id', sa.String(128), primary_key=True),
        sa.Column('lineage_id', sa.String(64), sa.ForeignKey('auth_session.id'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('receipt', sa.JSON().with_variant(JSONB(), 'postgresql'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    # A rollback must preserve tombstones and receipts, never resurrect A.
    raise RuntimeError('Auth-session authority cannot be removed by an unsafe rollback')
