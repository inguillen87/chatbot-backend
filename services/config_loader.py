import json
import os
import logging

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "municipios")

_config_cache = {}


def cargar_configuracion_municipio(municipio_id: str, archivo: str) -> dict:
    """Carga un archivo de configuración JSON para el municipio indicado."""
    clave = (municipio_id, archivo)
    if clave in _config_cache:
        return _config_cache[clave]
    ruta = os.path.join(BASE_CONFIG_PATH, municipio_id, archivo)
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            datos = json.load(f)
            _config_cache[clave] = datos
            return datos
    except Exception as e:
        logger.error(f"[CONFIG] No se pudo cargar {ruta}: {e}")
        _config_cache[clave] = {}
        return {}
