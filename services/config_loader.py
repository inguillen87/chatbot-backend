import json
import os
import logging

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "municipios")

_config_cache = {}
_mtime_cache = {}


def cargar_configuracion_municipio(municipio_id: str, archivo: str) -> dict:
    """Carga un archivo de configuración JSON para el municipio indicado.

    La información se recarga automáticamente si el archivo es modificado.
    """
    clave = ("default", archivo)
    ruta = os.path.join(BASE_CONFIG_PATH, "default", archivo)
    try:
        mtime = os.path.getmtime(ruta)
    except OSError as e:
        logger.error(f"[CONFIG] No se pudo acceder a {ruta}: {e}")
        _config_cache[clave] = {}
        _mtime_cache[clave] = None
        return {}

    if clave in _config_cache and _mtime_cache.get(clave) == mtime:
        return _config_cache[clave]

    try:
        with open(ruta, "r", encoding="utf-8") as f:
            datos = json.load(f)
        _config_cache[clave] = datos
        _mtime_cache[clave] = mtime
        return datos
    except Exception as e:
        logger.error(f"[CONFIG] No se pudo cargar {ruta}: {e}")
        _config_cache[clave] = {}
        _mtime_cache[clave] = mtime
        return {}
