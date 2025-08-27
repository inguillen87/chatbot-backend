import logging
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import InvalidRequestError

logger = logging.getLogger(__name__)

def safe_flag_modified(obj, attr):
    if not obj or not hasattr(obj, attr):
        return
    try:
        flag_modified(obj, attr)
    except InvalidRequestError:
        logger.warning(f"No se pudo marcar como modificado {attr} en {obj}.")
