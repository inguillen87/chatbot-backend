# services/utils_placeholders.py
import re
import os
import json
import logging
from services.config_loader import cargar_configuracion_municipio

logger = logging.getLogger(__name__)

def reemplazar_placeholders(texto, datos):
    if not isinstance(texto, str) or not texto:
        return texto

    def get_valor(clave):
        if isinstance(datos, dict):
            return datos.get(clave, "")
        try:
            return getattr(datos, clave, "")
        except Exception:
            return ""

    encontrados = re.findall(r"\[([a-zA-Z0-9_]+)\]", texto)
    for clave in set(encontrados):
        valor = get_valor(clave)
        texto = texto.replace(f"[{clave}]", str(valor) if valor is not None else "")
    return texto

# Ruta a datos generales
BASE_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")

SUGERENCIAS_PATH = os.path.join(BASE_DATA_PATH, "sugerencias.json")

_cache_sugerencias = None
_cache_respuestas_municipio = None

def sugerencias_por_rubro(rubro_nombre):
    global _cache_sugerencias
    if _cache_sugerencias is None:
        try:
            with open(SUGERENCIAS_PATH, "r", encoding="utf-8") as f:
                _cache_sugerencias = json.load(f)
        except Exception as e:
            logger.error(f"[SUGERENCIAS] No se pudo cargar sugerencias.json en {SUGERENCIAS_PATH}: {e}")
            _cache_sugerencias = {}
    rubro_key = str(rubro_nombre).strip().lower()
    return _cache_sugerencias.get(rubro_key, [])


def obtener_respuesta_municipio(clave: str) -> str | list:
    """Devuelve la respuesta o lista de la clave indicada desde la configuración del municipio."""
    global _cache_respuestas_municipio
    if _cache_respuestas_municipio is None:
        _cache_respuestas_municipio = cargar_configuracion_municipio(
            MUNICIPIO_ID, "respuestas_municipio.json"
        )
    return _cache_respuestas_municipio.get(clave, "")
