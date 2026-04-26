"""Add tenant profile entities for multi-tenant PWA support."""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260115_add_tenant_profiles"
down_revision = "20251209_expand_ver_len"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("nombre", sa.String(length=255), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("municipio_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("pyme_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("encuestas_tenant_id", sa.Integer(), nullable=True),
        sa.Column("dominio", sa.String(length=255), nullable=True),
        sa.Column("logo_url", sa.String(length=512), nullable=True),
        sa.Column("tema", sa.JSON(), nullable=True),
        sa.Column("configuracion", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(municipio_id IS NOT NULL) OR (pyme_id IS NOT NULL)",
            name="ck_tenant_profile_owner_present",
        ),
        sa.CheckConstraint(
            "NOT (municipio_id IS NOT NULL AND pyme_id IS NOT NULL)",
            name="ck_tenant_profile_single_owner",
        ),
        sa.UniqueConstraint("slug", name="uq_tenant_profile_slug"),
    )
    op.create_index(
        "ix_tenant_profile_slug",
        "tenant_profile",
        ["slug"],
        unique=False,
    )

    op.create_table(
        "tenant_follower",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column(
            "notifications_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id",
            "tenant_id",
            name="uq_tenant_follower_user_tenant",
        ),
    )
    op.create_index(
        "ix_tenant_follower_user",
        "tenant_follower",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_follower_tenant",
        "tenant_follower",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "tenant_ticket",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("categoria", sa.String(length=80), nullable=True),
        sa.Column("descripcion", sa.Text(), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False, server_default="nuevo"),
        sa.Column("origen", sa.String(length=20), nullable=False, server_default="pwa"),
        sa.Column("latitud", sa.Float(), nullable=True),
        sa.Column("longitud", sa.Float(), nullable=True),
        sa.Column("datos_extra", sa.JSON(), nullable=True),
        sa.Column("fingerprint", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_tenant_ticket_tenant",
        "tenant_ticket",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_ticket_user",
        "tenant_ticket",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_ticket_fingerprint",
        "tenant_ticket",
        ["fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_ticket_tenant_estado",
        "tenant_ticket",
        ["tenant_id", "estado"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_ticket_tenant_estado", table_name="tenant_ticket")
    op.drop_index("ix_tenant_ticket_fingerprint", table_name="tenant_ticket")
    op.drop_index("ix_tenant_ticket_user", table_name="tenant_ticket")
    op.drop_index("ix_tenant_ticket_tenant", table_name="tenant_ticket")
    op.drop_table("tenant_ticket")

    op.drop_index("ix_tenant_follower_tenant", table_name="tenant_follower")
    op.drop_index("ix_tenant_follower_user", table_name="tenant_follower")
    op.drop_table("tenant_follower")

    op.drop_index("ix_tenant_profile_slug", table_name="tenant_profile")
    op.drop_table("tenant_profile")
