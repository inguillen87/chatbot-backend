"""revoke unauthorized legacy superadmin credentials

Revision ID: 20260711_revoke_legacy_superadmins
Revises: 20260705_ticket_ai_metadata
Create Date: 2026-07-11 00:00:00.000000

"""
import os
import secrets
import uuid

from alembic import op
import sqlalchemy as sa
from werkzeug.security import generate_password_hash


revision = "20260711_revoke_legacy_superadmins"
down_revision = "20260705_ticket_ai_metadata"
branch_labels = None
depends_on = None


DEFAULT_ALLOWED_EMAILS = {"guillen.marce@gmail.com"}
PRIVILEGED_ROLES = {"super_admin", "superadmin", "platform_admin"}
KNOWN_COMPROMISED_EMAILS = {
    "franco@cuatrofincas.com",
    "info@servill.ar",
    "mauricio@junin.com",
}
KNOWN_COMPROMISED_API_TOKEN_EMAILS = {
    "franco@cuatrofincas.com",
}


def _allowed_emails() -> set[str]:
    configured = {
        item.strip().lower()
        for item in str(os.getenv("CLERK_SUPERADMIN_EMAILS") or "").split(",")
        if item.strip()
    }
    return configured or DEFAULT_ALLOWED_EMAILS


def _user_columns() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user" not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns("user")}


def upgrade() -> None:
    columns = _user_columns()
    required = {"id", "email", "rol", "password_hash"}
    if not required.issubset(columns):
        return

    bind = op.get_bind()
    user_table = sa.table(
        "user",
        sa.column("id", sa.Integer()),
        sa.column("email", sa.String()),
        sa.column("rol", sa.String()),
        sa.column("password_hash", sa.String()),
        sa.column("token", sa.String()),
        sa.column("entity_token", sa.String()),
        sa.column("tenant_id", sa.Integer()),
        sa.column("tenant_slug", sa.String()),
        sa.column("municipio_id", sa.Integer()),
        sa.column("pyme_id", sa.Integer()),
        sa.column("empresa_id", sa.Integer()),
        sa.column("password_reset_selector", sa.String()),
        sa.column("password_reset_verifier_hash", sa.String()),
        sa.column("password_reset_sent_at", sa.DateTime(timezone=True)),
    )
    selected_names = [
        name
        for name in (
            "id",
            "email",
            "rol",
            "password_hash",
            "token",
            "entity_token",
            "tenant_id",
            "tenant_slug",
            "municipio_id",
            "pyme_id",
            "empresa_id",
            "password_reset_selector",
            "password_reset_verifier_hash",
            "password_reset_sent_at",
        )
        if name in columns
    ]
    rows = bind.execute(
        sa.select(*(user_table.c[name] for name in selected_names)).where(
            sa.func.lower(user_table.c.rol).in_(PRIVILEGED_ROLES)
        )
    ).mappings().all()
    allowed = _allowed_emails()

    for row in rows:
        email = str(row.get("email") or "").strip().lower()
        tenant_bound = any(
            row.get(name)
            for name in ("tenant_id", "tenant_slug", "municipio_id", "pyme_id", "empresa_id")
            if name in columns
        )
        values = {
            "password_hash": generate_password_hash(
                secrets.token_urlsafe(48),
                method="pbkdf2:sha256:600000",
            ),
        }
        if email not in allowed:
            values["rol"] = "admin" if tenant_bound else "usuario"
        if "token" in columns:
            values["token"] = str(uuid.uuid4())
        if "entity_token" in columns:
            values["entity_token"] = None
        for name in (
            "password_reset_selector",
            "password_reset_verifier_hash",
            "password_reset_sent_at",
        ):
            if name in columns:
                values[name] = None
        bind.execute(
            sa.update(user_table).where(user_table.c.id == row["id"]).values(**values)
        )

    compromised_rows = bind.execute(
        sa.select(*(user_table.c[name] for name in selected_names)).where(
            sa.func.lower(user_table.c.email).in_(KNOWN_COMPROMISED_EMAILS)
        )
    ).mappings().all()
    for row in compromised_rows:
        email = str(row.get("email") or "").strip().lower()
        values = {
            "password_hash": generate_password_hash(
                secrets.token_urlsafe(48),
                method="pbkdf2:sha256:600000",
            ),
        }
        if "token" in columns and email in KNOWN_COMPROMISED_API_TOKEN_EMAILS:
            values["token"] = str(uuid.uuid4())
        for name in (
            "password_reset_selector",
            "password_reset_verifier_hash",
            "password_reset_sent_at",
        ):
            if name in columns:
                values[name] = None
        bind.execute(
            sa.update(user_table).where(user_table.c.id == row["id"]).values(**values)
        )


def downgrade() -> None:
    # Credential revocation and privilege removal are intentionally irreversible.
    pass
