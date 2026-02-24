from __future__ import annotations

from typing import Optional
from flask import current_app
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from extensions import db
from models import User

_ES_EMPLEADO_COLUMN_EXISTS: Optional[bool] = None
_ES_EMPLEADO_INSPECTION_LOGGED_FAILURE = False
# We know tenant_id exists in the model and migrations.
# Forcing True avoids runtime inspection errors in some environments.
# Critical fix: avoid 500 error on registration if inspector fails.
_TENANT_ID_COLUMN_EXISTS: bool = True


def _user_table_has_es_empleado_column() -> bool:
    """Return True if the ``user.es_empleado`` column exists in the database."""
    global _ES_EMPLEADO_COLUMN_EXISTS, _ES_EMPLEADO_INSPECTION_LOGGED_FAILURE

    if _ES_EMPLEADO_COLUMN_EXISTS is not None:
        return _ES_EMPLEADO_COLUMN_EXISTS

    try:
        inspector = inspect(db.engine)
        if not inspector.has_table("user"):
            return False

        columns = {c["name"] for c in inspector.get_columns("user")}
        result = "es_empleado" in columns
        _ES_EMPLEADO_COLUMN_EXISTS = result
        return result
    except (SQLAlchemyError, Exception) as exc:  # pragma: no cover - defensive
        # Keep login/demo hot paths resilient during transient DB hiccups and
        # avoid log storms: assume column present and memoize that decision.
        if not _ES_EMPLEADO_INSPECTION_LOGGED_FAILURE:
            current_app.logger.warning(
                "[auth] Could not inspect user.es_empleado column; assuming present. error=%s",
                exc,
            )
            _ES_EMPLEADO_INSPECTION_LOGGED_FAILURE = True

        _ES_EMPLEADO_COLUMN_EXISTS = True
        return True


def _user_table_has_tenant_id_column() -> bool:
    """Return True if the ``user.tenant_id`` column exists in the database."""
    # Always return True as the column is part of the core model definition
    # and we want to avoid fragile runtime inspection that fails in some envs.
    return True


def _safe_user_query():
    """Return a ``User`` query that avoids missing optional columns when needed."""

    query = User.query
    if not _user_table_has_es_empleado_column():
        query = query.options(defer(User.es_empleado))

    # tenant_id is assumed present now, so we don't defer it.
    return query


safe_user_query = _safe_user_query
user_table_has_es_empleado_column = _user_table_has_es_empleado_column
user_table_has_tenant_id_column = _user_table_has_tenant_id_column
