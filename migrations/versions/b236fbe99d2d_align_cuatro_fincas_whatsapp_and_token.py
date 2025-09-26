"""Ensure Cuatro Fincas uses its dedicated WhatsApp sender and token."""

from datetime import datetime
from typing import Optional

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

# revision identifiers, used by Alembic.
revision = "b236fbe99d2d"
down_revision = "9d5f3c0d4f4b"
branch_labels = None
depends_on = None

FRANCO_EMAIL = "franco@cuatrofincas.com"
FRANCO_NAME = "Franco Cuatro Fincas"
FRANCO_EMPRESA = "Bodega Cuatro Fincas"
FRANCO_PASSWORD = "123456"
FRANCO_TOKEN = "cuatrofincas-live-token"
FRANCO_PLAN = "premium"
FRANCO_LIMIT = 250
FRANCO_RUBRO_CLAVE = "bodega"

CUATRO_FINCAS_WHATSAPP = "+18564858589"
MAURICIO_EMAIL = "mauricio@junin.com"
MAURICIO_WHATSAPP = "+17432643718"


def _fetch_user(session: Session, email: str) -> Optional[dict]:
    return (
        session.execute(
            sa.text(
                """
                SELECT id, token, rol, tipo_chat, password_hash
                FROM \"user\"
                WHERE email = :email
                """
            ),
            {"email": email},
        )
        .mappings()
        .first()
    )


def _get_or_create_rubro(session: Session, clave: str) -> Optional[int]:
    rubro = (
        session.execute(
            sa.text("SELECT id FROM rubro WHERE clave = :clave"),
            {"clave": clave},
        )
        .mappings()
        .first()
    )
    if rubro:
        return rubro["id"]

    result = session.execute(
        sa.text(
            """
            INSERT INTO rubro (clave, nombre, es_publico, descripcion)
            VALUES (:clave, :nombre, :es_publico, :descripcion)
            RETURNING id
            """
        ),
        {
            "clave": clave,
            "nombre": "Bodega",
            "es_publico": False,
            "descripcion": "Catálogo boutique de vinos Cuatro Fincas",
        },
    )
    return result.scalar() if result else None


def _ensure_franco_user(session: Session) -> Optional[int]:
    rubro_id = _get_or_create_rubro(session, FRANCO_RUBRO_CLAVE)
    if not rubro_id:
        return None

    existing = _fetch_user(session, FRANCO_EMAIL)
    if existing:
        session.execute(
            sa.text(
                """
                UPDATE "user"
                SET name = :name,
                    nombre_empresa = :nombre_empresa,
                    rubro_id = :rubro_id,
                    tipo_chat = :tipo_chat,
                    rol = :rol,
                    plan = :plan,
                    limite_preguntas = :limite_preguntas,
                    token = :token,
                    empresa_id = NULL,
                    pyme_id = NULL,
                    municipio_id = NULL
                WHERE email = :email
                """
            ),
            {
                "name": FRANCO_NAME,
                "nombre_empresa": FRANCO_EMPRESA,
                "rubro_id": rubro_id,
                "tipo_chat": "pyme",
                "rol": "admin",
                "plan": FRANCO_PLAN,
                "limite_preguntas": FRANCO_LIMIT,
                "token": FRANCO_TOKEN,
                "email": FRANCO_EMAIL,
            },
        )

        if not existing.get("password_hash"):
            password_hash = generate_password_hash(FRANCO_PASSWORD)
            session.execute(
                sa.text(
                    """
                    UPDATE "user"
                    SET password_hash = :password_hash
                    WHERE id = :user_id
                    """
                ),
                {
                    "password_hash": password_hash,
                    "user_id": existing["id"],
                },
            )
        return existing["id"]

    password_hash = generate_password_hash(FRANCO_PASSWORD)
    result = session.execute(
        sa.text(
            """
            INSERT INTO "user"
                (name, email, nombre_empresa, password_hash, token, rol, tipo_chat,
                 plan, limite_preguntas, rubro_id, empresa_id, pyme_id, municipio_id)
            VALUES
                (:name, :email, :nombre_empresa, :password_hash, :token, :rol, :tipo_chat,
                 :plan, :limite_preguntas, :rubro_id, NULL, NULL, NULL)
            RETURNING id
            """
        ),
        {
            "name": FRANCO_NAME,
            "email": FRANCO_EMAIL,
            "nombre_empresa": FRANCO_EMPRESA,
            "password_hash": password_hash,
            "token": FRANCO_TOKEN,
            "rol": "admin",
            "tipo_chat": "pyme",
            "plan": FRANCO_PLAN,
            "limite_preguntas": FRANCO_LIMIT,
            "rubro_id": rubro_id,
        },
    )
    return result.scalar() if result else None


def _assign_number(session: Session, number: str, user_id: int) -> None:
    now = datetime.utcnow()
    mapping = (
        session.execute(
            sa.text(
                "SELECT id FROM whatsapp_numero WHERE numero_whatsapp = :number"
            ),
            {"number": number},
        )
        .mappings()
        .first()
    )
    if mapping:
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
                "mapping_id": mapping["id"],
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
                "number": number,
                "user_id": user_id,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    try:
        franco_id = _ensure_franco_user(session)
        mauricio = _fetch_user(session, MAURICIO_EMAIL)

        if franco_id:
            _assign_number(session, CUATRO_FINCAS_WHATSAPP, franco_id)

        if mauricio:
            _assign_number(session, MAURICIO_WHATSAPP, mauricio["id"])

        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    try:
        franco = _fetch_user(session, FRANCO_EMAIL)
        mauricio = _fetch_user(session, MAURICIO_EMAIL)

        if franco:
            mapping = (
                session.execute(
                    sa.text(
                        "SELECT id FROM whatsapp_numero WHERE numero_whatsapp = :number"
                    ),
                    {"number": CUATRO_FINCAS_WHATSAPP},
                )
                .mappings()
                .first()
            )
            if mapping:
                session.execute(
                    sa.text("DELETE FROM whatsapp_numero WHERE id = :mapping_id"),
                    {"mapping_id": mapping["id"]},
                )

            _assign_number(session, MAURICIO_WHATSAPP, franco["id"])
        elif mauricio:
            _assign_number(session, MAURICIO_WHATSAPP, mauricio["id"])

        session.commit()
    finally:
        session.close()
