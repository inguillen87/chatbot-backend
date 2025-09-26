"""Assign dedicated WhatsApp sender to Cuatro Fincas pyme admin

Revision ID: 9d5f3c0d4f4b
Revises: 3f3b6a9b7d6e
Create Date: 2025-09-26 18:30:00.000000
"""

from datetime import datetime
from typing import Optional

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session


# revision identifiers, used by Alembic.
revision = "9d5f3c0d4f4b"
down_revision = "3f3b6a9b7d6e"
branch_labels = None
depends_on = None


FRANCO_EMAIL = "franco@cuatrofincas.com"
CUATRO_FINCAS_WHATSAPP = "+14795924727"


def _fetch_user_id(session: Session, email: str) -> Optional[int]:
    result = session.execute(
        sa.text("SELECT id FROM \"user\" WHERE email = :email"),
        {"email": email},
    ).scalar()
    return result if result is not None else None


def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    try:
        user_id = _fetch_user_id(session, FRANCO_EMAIL)
        if not user_id:
            return

        existing_mapping = session.execute(
            sa.text(
                "SELECT id, user_id, is_active FROM whatsapp_numero WHERE numero_whatsapp = :number"
            ),
            {"number": CUATRO_FINCAS_WHATSAPP},
        ).mappings().first()

        now = datetime.utcnow()

        if existing_mapping:
            session.execute(
                sa.text(
                    """
                    UPDATE whatsapp_numero
                    SET user_id = :user_id,
                        is_active = :is_active,
                        updated_at = :updated_at
                    WHERE id = :mapping_id
                    """
                ),
                {
                    "user_id": user_id,
                    "is_active": True,
                    "updated_at": now,
                    "mapping_id": existing_mapping["id"],
                },
            )
        else:
            session.execute(
                sa.text(
                    """
                    INSERT INTO whatsapp_numero (numero_whatsapp, user_id, is_active, created_at, updated_at)
                    VALUES (:number, :user_id, :is_active, :created_at, :updated_at)
                    """
                ),
                {
                    "number": CUATRO_FINCAS_WHATSAPP,
                    "user_id": user_id,
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                },
            )

        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    try:
        user_id = _fetch_user_id(session, FRANCO_EMAIL)
        if not user_id:
            return

        session.execute(
            sa.text(
                "DELETE FROM whatsapp_numero WHERE numero_whatsapp = :number AND user_id = :user_id"
            ),
            {"number": CUATRO_FINCAS_WHATSAPP, "user_id": user_id},
        )
        session.commit()
    finally:
        session.close()
