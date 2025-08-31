import json
import os
import logging

logger = logging.getLogger(__name__)

# Determine the base directory for municipal data. Deployments on services like
# Render provide a persistent volume mounted at `/data`. We want to always store
# mutable configuration (like `agenda_cultural.json`) there so it survives
# redeploys. Allow overriding via the `DATA_DIR` environment variable, and fall
# back to the repository's `data` directory only if writing to `/data` is not
# possible (e.g. during local development without permissions).

_default_data_path = os.environ.get("DATA_DIR", "/data")

try:
    # Ensure the directory exists so subsequent code can rely on it.
    os.makedirs(os.path.join(_default_data_path, "municipios"), exist_ok=True)
except OSError:
    # Fallback to repo `data` directory if `/data` cannot be created/written.
    _default_data_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(os.path.join(_default_data_path, "municipios"), exist_ok=True)

BASE_DATA_PATH = _default_data_path
BASE_CONFIG_PATH = os.path.join(BASE_DATA_PATH, "municipios")

_config_cache = {}
_mtime_cache = {}


def cargar_configuracion_municipio(municipio_id: str, archivo: str) -> dict:
    """Carga un archivo de configuración JSON para el municipio indicado.

    `municipio_id` puede recibirse como ``int`` o ``str``. Para evitar errores
    al construir la ruta del archivo, se fuerza su conversión a cadena.

    La información se recarga automáticamente si el archivo es modificado.
    """
    municipio_id = str(municipio_id)
    clave = (municipio_id, archivo)
    ruta = os.path.join(BASE_CONFIG_PATH, municipio_id, archivo)

    if not os.path.exists(ruta) and municipio_id != "default":
        # Fallback to the shared "default" configuration when a municipality
        # specific file is missing. This prevents noisy errors in logs and
        # keeps behaviour consistent for municipalities that have not yet
        # provided their own overrides.
        return cargar_configuracion_municipio("default", archivo)

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
