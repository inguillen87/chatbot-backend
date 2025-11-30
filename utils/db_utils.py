import logging
import time

from sqlalchemy import inspect, text
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

def safe_flag_modified(obj, attr):
    if not obj or not hasattr(obj, attr):
        return
    try:
        flag_modified(obj, attr)
    except InvalidRequestError:
        logger.warning(f"No se pudo marcar como modificado {attr} en {obj}.")


def commit_with_retry(session, retries: int = 3, delay: float = 0.1) -> bool:
    """Attempt to commit the session, retrying on SQLite locked errors.

    Parameters
    ----------
    session: SQLAlchemy session
        The session to commit.
    retries: int
        Number of attempts before giving up.
    delay: float
        Seconds to wait between retries.

    Returns
    -------
    bool
        True if the commit succeeded, False otherwise.
    """
    for attempt in range(1, retries + 1):
        try:
            session.commit()
            return True
        except OperationalError as exc:
            if "database is locked" in str(exc).lower() and attempt < retries:
                session.rollback()
                time.sleep(delay)
                continue
            session.rollback()
            raise
    return False


def ensure_chat_session_context_schema(session) -> None:
    """Guarantee ``tenant_id`` exists on ``chat_session_context`` to avoid runtime errors.

    This is a safety net for environments where migrations may not have run yet.
    It is idempotent and cheap (inspects metadata before altering).
    """

    try:
        bind = session.get_bind()
        inspector = inspect(bind)
        columns = {col["name"] for col in inspector.get_columns("chat_session_context")}
        if "tenant_id" in columns:
            return

        logger.warning("tenant_id missing in chat_session_context; attempting auto-add")

        ddl_conn = bind.execution_options(isolation_level="AUTOCOMMIT")
        ddl_conn.execute(
            text(
                "ALTER TABLE chat_session_context "
                "ADD COLUMN IF NOT EXISTS tenant_id INTEGER"
            )
        )

        # Re-validate after attempting the DDL
        inspector = inspect(bind)
        columns = {col["name"] for col in inspector.get_columns("chat_session_context")}
        if "tenant_id" not in columns:
            logger.error(
                "tenant_id creation attempt did not persist; manual migration required"
            )
        else:
            logger.info("tenant_id column ensured on chat_session_context via runtime safeguard")
    except Exception as exc:  # pragma: no cover - best-effort safeguard
        logger.warning(
            "No se pudo asegurar la columna tenant_id en chat_session_context", exc_info=exc
        )
