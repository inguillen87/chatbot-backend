from __future__ import annotations

from flask import current_app
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from extensions import db
from models import User


def _user_table_has_es_empleado_column() -> bool:
    """Return True if the ``user.es_empleado`` column exists in the database."""

    try:
        inspector = inspect(db.engine)
        return inspector.has_table("user") and inspector.has_column("user", "es_empleado")
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        current_app.logger.warning(
            "[auth] Could not inspect user.es_empleado column; assuming present.",
            exc_info=exc,
        )
    except Exception:
        # In case the engine is not yet available, keep default behavior.
        pass

    return True


def _user_table_has_tenant_id_column() -> bool:
    """Return True if the ``user.tenant_id`` column exists in the database."""

    try:
        inspector = inspect(db.engine)
        return inspector.has_table("user") and inspector.has_column("user", "tenant_id")
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        current_app.logger.warning(
            "[auth] Could not inspect user.tenant_id column; assuming MISSING.",
            exc_info=exc,
        )
    except Exception:
        # In case the engine is not yet available, assume the column is absent.
        current_app.logger.warning(
            "[auth] Generic error inspecting user.tenant_id; assuming MISSING.",
        )

    return False


def _safe_user_query():
    """Return a ``User`` query that avoids missing optional columns when needed."""

    query = User.query
    if not _user_table_has_es_empleado_column():
        query = query.options(defer(User.es_empleado))
    if not _user_table_has_tenant_id_column():
        current_app.logger.warning(
            "[auth] user.tenant_id column missing in DB; deferring tenant_id to avoid schema mismatch.",
        )
        query = query.options(defer(User.tenant_id))
    return query


safe_user_query = _safe_user_query
user_table_has_es_empleado_column = _user_table_has_es_empleado_column
user_table_has_tenant_id_column = _user_table_has_tenant_id_column
