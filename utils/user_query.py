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


def _safe_user_query():
    """Return a ``User`` query that avoids missing optional columns when needed."""

    query = User.query
    if not _user_table_has_es_empleado_column():
        query = query.options(defer(User.es_empleado))
    return query


safe_user_query = _safe_user_query
user_table_has_es_empleado_column = _user_table_has_es_empleado_column
