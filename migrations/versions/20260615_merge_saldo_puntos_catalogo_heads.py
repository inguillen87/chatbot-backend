"""Merge saldo_puntos and catalogo_item tenant_id heads."""

# revision identifiers, used by Alembic.
revision = "20260615_merge_saldo_puntos_catalogo_heads"
down_revision = (
    "202511210001",
    "20260601_add_tenant_id_to_catalogo_item",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
