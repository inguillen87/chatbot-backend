import logging
import time
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import InvalidRequestError, OperationalError

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
