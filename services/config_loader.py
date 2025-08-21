import json
import os
import logging

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "municipios")

_config_cache = {}
_mtime_cache = {}


def cargar_configuracion_municipio(municipio_id: str, archivo: str) -> dict:
    """
    Carga un archivo de configuración JSON para un municipio.
    Si el archivo específico del municipio no existe, carga el de 'default'.
    La información se recarga automáticamente si el archivo es modificado.
    """
    if not municipio_id:
        municipio_id = 'default'

    # 1. Intentar la ruta específica del municipio
    ruta_especifica = os.path.join(BASE_CONFIG_PATH, str(municipio_id), archivo)

    # 2. Determinar la ruta final (específica o default)
    if os.path.exists(ruta_especifica):
        ruta_final = ruta_especifica
        clave_cache = (str(municipio_id), archivo)
    else:
        ruta_final = os.path.join(BASE_CONFIG_PATH, "default", archivo)
        clave_cache = ("default", archivo)
        if not os.path.exists(ruta_final):
             logger.warning(f"[CONFIG] No se encontró ni el archivo específico '{ruta_especifica}' ni el default '{ruta_final}'.")
             return {}


    # 3. Comprobar caché
    try:
        mtime = os.path.getmtime(ruta_final)
    except OSError as e:
        logger.error(f"[CONFIG] No se pudo acceder a {ruta_final}: {e}")
        _config_cache[clave_cache] = {}
        _mtime_cache[clave_cache] = None
        return {}

    if clave_cache in _config_cache and _mtime_cache.get(clave_cache) == mtime:
        return _config_cache[clave_cache]

    # 4. Cargar archivo si no está en caché o está modificado
    try:
        with open(ruta_final, "r", encoding="utf-8") as f:
            datos = json.load(f)
        _config_cache[clave_cache] = datos
        _mtime_cache[clave_cache] = mtime
        logger.info(f"[CONFIG] Configuración '{archivo}' cargada desde '{ruta_final}'.")
        return datos
    except Exception as e:
        logger.error(f"[CONFIG] No se pudo cargar {ruta_final}: {e}")
        _config_cache[clave_cache] = {}
        _mtime_cache[clave_cache] = mtime
        return {}
