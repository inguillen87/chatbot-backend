# services/utils_placeholders.py
import re
import os
import json
import logging

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

# La ruta correcta a /data/sugerencias.json (dos niveles arriba desde /services)
BASE_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

SUGERENCIAS_PATH = os.path.join(BASE_DATA_PATH, "sugerencias.json")
RESPUESTAS_MUNICIPIO_PATH = os.path.join(BASE_DATA_PATH, "respuestas_municipio.json")

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
    """Devuelve la respuesta o lista de la clave indicada desde respuestas_municipio.json."""
    global _cache_respuestas_municipio
    if _cache_respuestas_municipio is None:
        try:
            with open(RESPUESTAS_MUNICIPIO_PATH, "r", encoding="utf-8") as f:
                _cache_respuestas_municipio = json.load(f)
        except Exception as e:
            logger.error(f"[RESPUESTAS] No se pudo cargar {RESPUESTAS_MUNICIPIO_PATH}: {e}")
            _cache_respuestas_municipio = {}
    return _cache_respuestas_municipio.get(clave, "")
