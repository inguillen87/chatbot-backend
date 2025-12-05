from __future__ import annotations

from typing import Optional
from flask import current_app
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from extensions import db
from models import User

_ES_EMPLEADO_COLUMN_EXISTS: Optional[bool] = None
_TENANT_ID_COLUMN_EXISTS: Optional[bool] = None


def _user_table_has_es_empleado_column() -> bool:
    """Return True if the ``user.es_empleado`` column exists in the database."""
    global _ES_EMPLEADO_COLUMN_EXISTS

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
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        current_app.logger.warning(
            "[auth] Could not inspect user.es_empleado column; assuming present.",
            exc_info=exc,
        )
    except Exception as e:
        # In case the engine is not yet available, keep default behavior.
        current_app.logger.warning(
            f"[auth] Generic error inspecting user.es_empleado: {e}",
            exc_info=True
        )
        pass

    return True


def _user_table_has_tenant_id_column() -> bool:
    """Return True if the ``user.tenant_id`` column exists in the database."""
    global _TENANT_ID_COLUMN_EXISTS

    if _TENANT_ID_COLUMN_EXISTS is not None:
        return _TENANT_ID_COLUMN_EXISTS

    try:
        inspector = inspect(db.engine)
        if not inspector.has_table("user"):
            return False

        columns = {c["name"] for c in inspector.get_columns("user")}
        result = "tenant_id" in columns
        _TENANT_ID_COLUMN_EXISTS = result
        return result
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        current_app.logger.warning(
            "[auth] Could not inspect user.tenant_id column; assuming MISSING.",
            exc_info=exc,
        )
    except Exception as e:
        # In case the engine is not yet available, assume the column is absent.
        current_app.logger.warning(
            f"[auth] Generic error inspecting user.tenant_id: {e}; assuming MISSING.",
            exc_info=True
        )

    return False


def _safe_user_query():
    """Return a ``User`` query that avoids missing optional columns when needed."""

    query = User.query
    if not _user_table_has_es_empleado_column():
        query = query.options(defer(User.es_empleado))
    if not _user_table_has_tenant_id_column():
        # Only log once per request/context if possible, but for now we rely on the cached check
        # to avoid spamming the logs if the function above caches it.
        # However, the warning below is explicit for the query construction.
        # We can downgrade it to debug if it's too noisy, but it's important.
        current_app.logger.debug(
            "[auth] user.tenant_id column missing in DB; deferring tenant_id to avoid schema mismatch.",
        )
        query = query.options(defer(User.tenant_id))
    return query


safe_user_query = _safe_user_query
user_table_has_es_empleado_column = _user_table_has_es_empleado_column
user_table_has_tenant_id_column = _user_table_has_tenant_id_column
