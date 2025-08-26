import json
import os
import logging

logger = logging.getLogger(__name__)

# Determine the base directory for municipal data. On platforms like Render the
# `/data` directory persists across deployments, so prefer it when available.
# Allow overriding via the `DATA_DIR` environment variable for flexibility.
_default_data_path = os.environ.get("DATA_DIR")
if not _default_data_path:
    persistent_path = "/data"
    repo_data_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

    # Prefer the persistent volume only if it actually contains municipal data.
    persistent_municipios = os.path.join(persistent_path, "municipios")
    if os.path.exists(os.path.join(persistent_municipios, "default")):
        _default_data_path = persistent_path
    else:
        _default_data_path = repo_data_path

BASE_DATA_PATH = _default_data_path
BASE_CONFIG_PATH = os.path.join(BASE_DATA_PATH, "municipios")

_config_cache = {}
_mtime_cache = {}


def cargar_configuracion_municipio(municipio_id: str, archivo: str) -> dict:
    """Carga un archivo de configuración JSON para el municipio indicado.

    La información se recarga automáticamente si el archivo es modificado.
    """
    clave = (municipio_id, archivo)
    ruta = os.path.join(BASE_CONFIG_PATH, municipio_id, archivo)
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
